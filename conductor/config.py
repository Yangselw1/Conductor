"""Configuration loading + scaffolding for Conductor.

A project is described by a single `conductor.yaml`. Everything else (state, worktrees, logs)
lives under `.conductor/` next to it. Repo paths may be absolute or relative to the config file.
"""

import os
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ---- glob helpers: lock conflicts (path-overlap) + scope enforcement ------------------------- #
def _prefix(glob):
    """Literal path prefix of a glob, up to the first wildcard."""
    for j, ch in enumerate(glob):
        if ch in "*?[":
            return glob[:j]
    return glob


def _globs_overlap(g1, g2):
    """Conservative: two globs can match a common path if either's literal prefix contains the
    other's. Over-serializes in ambiguous cases (safe), never under-serializes overlapping paths."""
    p1, p2 = _prefix(g1), _prefix(g2)
    return p1.startswith(p2) or p2.startswith(p1)


def _glob_re(glob):
    out, i = "^", 0
    while i < len(glob):
        if glob[i:i + 2] == "**":
            out += ".*"
            i += 2
            if glob[i:i + 1] == "/":
                i += 1
        elif glob[i] == "*":
            out += "[^/]*"; i += 1
        elif glob[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(glob[i]); i += 1
    return re.compile(out + "$")


def path_in_globs(path, globs):
    """True if a repo-relative path matches any of the lock's path globs."""
    return any(_glob_re(g).match(path) for g in globs)


def allowed_globs(cfg, lock_names, repo):
    """The globs a job is allowed to edit in a given repo. None means 'anything' (global lock '*')."""
    if "*" in lock_names:
        return None
    globs = []
    for ln in lock_names:
        lk = cfg["locks"].get(ln, {})
        if repo in lk.get("_repos", []):
            globs += lk.get("paths", ["**"])
    return globs

DEFAULTS = {
    "default_branch": "main",
    "max_parallel": 4,
    "tick_seconds": 4,
    "worktree_root": None,            # default: <config_dir>/.conductor/trees
    "worker": {
        # {brief} is substituted with the task brief. Each worker is a FULL Claude Code session.
        "command": ["claude", "-p", "{brief}", "--permission-mode", "bypassPermissions"],
        "add_dir_flag": "--add-dir",  # how to grant a worker access to its extra-repo worktrees
        "timeout_sec": 3600,
        "verify_timeout_sec": 900,
    },
    "scope": {
        "scout": False,               # when true, an LLM infers a request's locks if not given
        "scout_model": "claude-haiku-4-5",
        "env_file": None,             # optional path to a .env holding ANTHROPIC_API_KEY
    },
    "repos": {},                       # name -> path
    "locks": {},                       # name -> {repo|repos, paths:[globs], desc}
    "verify": {},                      # repo-name -> shell cmd ; "global" -> cross-repo cmd
}


def find_config(start=None):
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        f = d / "conductor.yaml"
        if f.exists():
            return f
    return None


def load(path=None):
    path = Path(path) if path else find_config()
    if not path or not path.exists():
        raise SystemExit("no conductor.yaml found — run `conductor init` here first.")
    if yaml is None:
        raise SystemExit("Conductor needs pyyaml:  pip install pyyaml")
    cfg = _deep_merge(DEFAULTS, yaml.safe_load(path.read_text()) or {})
    cfg["_path"], cfg["_dir"] = str(path), str(path.parent)
    if not cfg["worktree_root"]:
        cfg["worktree_root"] = str(path.parent / ".conductor" / "trees")
    for k, v in list(cfg["repos"].items()):
        cfg["repos"][k] = v if os.path.isabs(v) else str((path.parent / v).resolve())
    _validate(cfg)
    return cfg


def _validate(cfg):
    for name, lk in cfg["locks"].items():
        repos = lk.get("repos") or ([lk["repo"]] if lk.get("repo") else [])
        for r in repos:
            if r not in cfg["repos"]:
                raise SystemExit(f"lock {name!r} references unknown repo {r!r}")
        lk["_repos"] = repos
    for r in cfg["repos"]:
        if not (Path(cfg["repos"][r]) / ".git").exists():
            raise SystemExit(f"repo {r!r} at {cfg['repos'][r]} is not a git checkout")
    cfg["_lock_conflicts"] = _conflict_graph(cfg)


def _conflict_graph(cfg):
    """Precompute which lock names conflict: same repo + overlapping path globs. A lock always
    conflicts with itself; a lock with no `paths` defaults to ['**'] (whole repo = coarse)."""
    names = list(cfg["locks"])
    graph = {n: {n} for n in names}
    for i, a in enumerate(names):
        la = cfg["locks"][a]
        for b in names[i + 1:]:
            lb = cfg["locks"][b]
            if not (set(la["_repos"]) & set(lb["_repos"])):
                continue
            if any(_globs_overlap(g1, g2)
                   for g1 in la.get("paths", ["**"]) for g2 in lb.get("paths", ["**"])):
                graph[a].add(b)
                graph[b].add(a)
    return graph


def _deep_merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def repos_for_locks(cfg, lock_names):
    """The set of repos a job touches = union of the repos its locks live in (FIFO-ordered)."""
    seen = []
    for ln in lock_names:
        if ln == "*":
            for r in cfg["repos"]:
                if r not in seen:
                    seen.append(r)
            continue
        for r in cfg["locks"].get(ln, {}).get("_repos", []):
            if r not in seen:
                seen.append(r)
    return seen or list(cfg["repos"])[:1]


def init(dirpath="."):
    f = Path(dirpath) / "conductor.yaml"
    if f.exists():
        raise SystemExit(f"{f} already exists.")
    f.write_text(EXAMPLE)
    print(f"wrote {f}\nedit the repos / locks / verify sections, then:  conductor up")


EXAMPLE = """# conductor.yaml — describe your system once; Conductor schedules agents against it.
version: 1
default_branch: main
max_parallel: 4

# 1) The repos Conductor coordinates (name -> path, absolute or relative to this file).
repos:
  cortex-engines: ../cortex-engines
  cortex:         ../cortex

# 2) LOCKS — carve the codebase into ownable units. A request acquires one or more.
#    Narrow locks parallelize; coarse/seam locks deliberately serialize the dangerous shared areas.
locks:
  mosaic-quant:  { repo: cortex-engines, paths: ["services/equity/**", "lib/equity/**"], desc: "Mosaic equity engine" }
  prism:         { repo: cortex-engines, paths: ["api/generate-prism*", "services/prism-*"], desc: "PRISM engine" }
  macro:         { repo: cortex-engines, paths: ["api/generate-macro*", "services/regime-model.ts", "services/network-contagion.ts"], desc: "MacroCollision" }
  thesis-val:    { repo: cortex-engines, paths: ["api/generate-thesis-*", "components/Thesis*"], desc: "Thesis Validator" }
  mc-shared:     { repo: cortex-engines, paths: ["services/gemini.ts", "api/_lib/**", "services/atlas-export.ts"], desc: "shared MC core — serializes anything touching these files; semantic breaks are caught by verify" }
  cockpit:       { repo: cortex, paths: ["app/**"], desc: "Cortex cockpit UI/server" }
  engines:       { repo: cortex, paths: ["engines/**"], desc: "Cortex engines" }
  vault-scripts: { repo: cortex, paths: ["vault/.atlas/**"], desc: "Atlas vault scripts/lib" }
  atlas-seam:    { repos: [cortex-engines, cortex], paths: ["services/*atlas*.ts", "vault/.atlas/scripts/drain_inbox.py"], desc: "the cross-repo export contract — one owner, both sides" }

# 3) VERIFY — run after a worker merges, before its locks release. Per-repo + an optional cross-repo gate.
verify:
  cortex-engines: "npx tsc --noEmit"
  cortex:         "python3 index/build.py >/dev/null && python3 vault/.atlas/scripts/validate.py"
  # global: "your end-to-end round-trip / contract test"

# 4) Each worker is a FULL headless Claude Code session (full tools, MCP, skills, model).
worker:
  command: ["claude", "-p", "{brief}", "--permission-mode", "bypassPermissions"]
  timeout_sec: 3600

# 5) Optional: let an LLM infer a request's locks when you don't pass --locks.
scope:
  scout: false
  scout_model: claude-haiku-4-5
  # env_file: ../cortex-engines/.env   # where ANTHROPIC_API_KEY lives
"""
