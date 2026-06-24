"""The orchestration engine: dispatch, the full-session worker, and the merge gate.

Scheduler invariant: a queued job starts only when none of its locks *conflict* with a held lock,
where two locks conflict iff they share a repo and their path globs overlap (computed once at config
load). Each worker runs in its own git worktree(s) as a full headless Claude Code session. When it
exits, the merge gate — which can never crash the daemon — commits its worktrees, enforces that it
stayed in scope, merges every repo all-or-nothing, runs verify, and only then releases its locks.

Single-daemon by design: `conductor up`/`tick` hold an exclusive daemon lock, so the lock table and
merges are never raced by a second scheduler.
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config, scope


class _GateError(Exception):
    """A merge-gate failure that should park the job (rolled back, branch kept)."""


# ---- process / git helpers ------------------------------------------------------------------- #
def _run(cmd, cwd=None, timeout=120):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _git(repo, *args, timeout=120):
    return _run(["git", "-C", repo, *args], timeout=timeout)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid):
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        pass


def _harvest_zombies():
    """In the long-running daemon, workers are our children; reap exited ones so a finished
    worker stops looking 'alive' (a zombie still answers os.kill(pid, 0))."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid == 0:
            return


def daemon_lock(cfg):
    """Acquire the exclusive single-scheduler lock. Returns the open handle (keep it alive for the
    process lifetime). Raises SystemExit if another scheduler is already running here."""
    lp = Path(cfg["_dir"]) / ".conductor" / "daemon.lock"
    lp.parent.mkdir(parents=True, exist_ok=True)
    f = open(lp, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another `conductor` scheduler is already running here (daemon.lock held).")
    f.write(str(os.getpid()))
    f.flush()
    return f


# ---- dispatch -------------------------------------------------------------------------------- #
def dispatch(cfg, store):
    """Start as many queued, non-conflicting jobs as the parallelism budget allows."""
    held = store.held_locks()
    running = len(store.by_status("running", "merging"))
    for job in store.by_status("queued"):
        if running >= cfg["max_parallel"]:
            break
        locks = json.loads(job["locks"])
        if scope.conflict(locks, held, cfg):
            continue                                  # a held lock conflicts — wait
        if _start(cfg, store, job):
            held |= set(locks)
            running += 1


def _brief(cfg, job, worktrees):
    repos = json.loads(job["repos"])
    lock_paths = []
    for ln in json.loads(job["locks"]):
        lock_paths += cfg["locks"].get(ln, {}).get("paths", [])
    dirs = "\n".join(f"  - {rk}: {worktrees[rk]}" + ("   (cwd)" if i == 0 else "   (--add-dir)")
                     for i, rk in enumerate(repos))
    scope_line = ", ".join(lock_paths) if lock_paths else "(touch only what the task strictly needs)"
    return (
        f"{job['request']}\n\n"
        "----- Conductor worker context (do not echo this back) -----\n"
        "You are an isolated worker running in dedicated git worktree(s). Make ONLY the changes "
        "this task requires, and ONLY within your scope. Working directories:\n"
        f"{dirs}\n"
        f"In-scope paths (edits OUTSIDE these are rejected at merge): {scope_line}\n"
        "Do NOT run git commit/merge/push — the coordinator commits your worktree and merges your "
        "branch automatically once you finish, AFTER checking you stayed in scope. When done, stop."
    )


def _worker_cmd(cfg, brief, worktrees, repos):
    cmd = [tok.replace("{brief}", brief) if "{brief}" in tok else tok
           for tok in cfg["worker"]["command"]]
    flag = cfg["worker"].get("add_dir_flag")
    if flag and len(repos) > 1:
        for rk in repos[1:]:
            cmd += [flag, worktrees[rk]]
    return cmd


def _start(cfg, store, job):
    jid, repos = job["id"], json.loads(job["repos"])
    branch = f"conductor/{jid}"
    root = Path(cfg["worktree_root"]) / jid
    worktrees, created = {}, []
    for rk in repos:
        wt = root / rk
        r = _git(cfg["repos"][rk], "worktree", "add", "-b", branch, str(wt), cfg["default_branch"])
        if r.returncode != 0:
            _cleanup(cfg, created, branch, worktrees, keep_branch=False)
            store.update(jid, status="failed", finished=time.time(),
                         note=f"worktree add failed in {rk}: {r.stderr[:300]}")
            return False
        worktrees[rk] = str(wt)
        created.append(rk)
    cmd = _worker_cmd(cfg, _brief(cfg, job, worktrees), worktrees, repos)
    logdir = Path(cfg["_dir"]) / ".conductor" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logp = logdir / f"{jid}.log"
    logf = open(logp, "w")
    try:
        p = subprocess.Popen(cmd, cwd=worktrees[repos[0]], stdout=logf,
                             stderr=subprocess.STDOUT, start_new_session=True)
    except (FileNotFoundError, OSError) as e:
        logf.close()
        _cleanup(cfg, created, branch, worktrees, keep_branch=False)
        store.update(jid, status="failed", finished=time.time(), note=f"cannot spawn worker: {e}")
        return False
    logf.close()                                       # the child inherited the fd; don't leak it here
    store.update(jid, status="running", branch=branch, worktrees=json.dumps(worktrees),
                 pid=p.pid, log=str(logp), started=time.time())
    return True


# ---- reap + merge gate ----------------------------------------------------------------------- #
def reap(cfg, store):
    _harvest_zombies()
    for job in store.by_status("running"):
        pid = job["pid"]
        if pid and _alive(pid):
            started = job["started"] or 0
            if cfg["worker"]["timeout_sec"] and started and \
                    time.time() - started > cfg["worker"]["timeout_sec"]:
                _kill(pid)                              # timed out -> park, do NOT merge partial work
                _cleanup(cfg, json.loads(job["repos"]), job["branch"],
                         json.loads(job["worktrees"]), keep_branch=True)
                store.update(job["id"], status="failed", finished=time.time(),
                             note=f"worker timed out (>{cfg['worker']['timeout_sec']}s); branch kept, not merged")
            continue
        _merge_gate(cfg, store, job)


def _merge_gate(cfg, store, job):
    jid = job["id"]
    if store.get(jid)["status"] != "running":          # another reaper already took it
        return
    store.update(jid, status="merging")
    repos = json.loads(job["repos"])
    worktrees = json.loads(job["worktrees"])
    branch, locks = job["branch"], json.loads(job["locks"])
    merged = []
    try:
        # 0) preconditions: each main checkout is on the default branch and clean
        for rk in repos:
            repo = cfg["repos"][rk]
            head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if head != cfg["default_branch"]:
                raise _GateError(f"{rk} is on {head!r}, not {cfg['default_branch']!r}; "
                                 "Conductor merges into the default branch")
            if _git(repo, "status", "--porcelain").stdout.strip():
                raise _GateError(f"{rk} has uncommitted changes in its main checkout; refusing to merge")

        # 1) commit the worker's output on its branch (the coordinator owns the commit)
        changed = False
        for rk in repos:
            _git(worktrees[rk], "add", "-A")
            if _git(worktrees[rk], "commit", "-m", f"conductor[{jid}]: {job['request'][:64]}").returncode == 0:
                changed = True
        if not changed:
            _cleanup(cfg, repos, branch, worktrees, keep_branch=False)
            store.update(jid, status="done", finished=time.time(), note="worker made no changes")
            return

        # 2) per repo: ENFORCE scope, then merge, then verify
        for rk in repos:
            repo = cfg["repos"][rk]
            allowed = config.allowed_globs(cfg, locks, rk)        # None = global lock -> anything
            if allowed is not None:
                files = _git(repo, "diff", "--name-only", f"{cfg['default_branch']}..{branch}").stdout.split()
                stray = [f for f in files if not config.path_in_globs(f, allowed)]
                if stray:
                    raise _GateError(f"worker edited out of scope in {rk}: {', '.join(stray[:6])}")
            m = _git(repo, "merge", "--no-ff", "--no-edit", branch)
            if m.returncode != 0:
                _git(repo, "merge", "--abort")
                raise _GateError(f"merge conflict in {rk}")
            merged.append(rk)
            v = _verify(cfg, rk)
            if v:
                raise _GateError(v)

        # 3) cross-repo / contract verify
        if cfg["verify"].get("global"):
            r = _run(["bash", "-lc", cfg["verify"]["global"]], cwd=cfg["_dir"],
                     timeout=cfg["worker"]["verify_timeout_sec"])
            if r.returncode != 0:
                raise _GateError(f"global verify failed: {(r.stdout + r.stderr)[-300:]}")

    except Exception as e:                              # noqa: BLE001 — the gate must never crash the loop
        for rk in merged:                               # roll back every merge we made
            _git(cfg["repos"][rk], "reset", "--hard", "HEAD~1")
        _cleanup(cfg, repos, branch, worktrees, keep_branch=True)
        store.update(jid, status="failed", finished=time.time(),
                     note=f"{e}  (branch {branch} kept for inspection; log: {job['log']})")
        return

    _cleanup(cfg, repos, branch, worktrees, keep_branch=False)
    store.update(jid, status="done", finished=time.time(),
                 note=f"merged + verified into {cfg['default_branch']}")


def _verify(cfg, repo_key):
    cmd = cfg["verify"].get(repo_key)
    if not cmd:
        return None
    r = _run(["bash", "-lc", cmd], cwd=cfg["repos"][repo_key], timeout=cfg["worker"]["verify_timeout_sec"])
    return None if r.returncode == 0 else f"verify failed in {repo_key}: {(r.stdout + r.stderr)[-300:]}"


def _cleanup(cfg, repo_keys, branch, worktrees, keep_branch):
    for rk in repo_keys:
        wt = worktrees.get(rk)
        if wt and os.path.exists(wt):
            _git(cfg["repos"][rk], "worktree", "remove", "--force", wt)
            if os.path.exists(wt):                      # belt-and-suspenders if remove was refused
                shutil.rmtree(wt, ignore_errors=True)
                _git(cfg["repos"][rk], "worktree", "prune")
        if not keep_branch:
            _git(cfg["repos"][rk], "branch", "-D", branch)


def cancel(cfg, store, jid):
    job = store.get(jid)
    if not job:
        raise SystemExit(f"no such job: {jid}")
    if job["pid"] and _alive(job["pid"]):
        _kill(job["pid"])
    if job["worktrees"]:
        _cleanup(cfg, json.loads(job["repos"]), job["branch"], json.loads(job["worktrees"]),
                 keep_branch=False)
    store.update(jid, status="canceled", finished=time.time(), note="canceled by user")


# ---- the loop -------------------------------------------------------------------------------- #
def run_forever(cfg, store, once=False):
    while True:
        reap(cfg, store)
        dispatch(cfg, store)
        if once:
            return
        time.sleep(cfg["tick_seconds"])
