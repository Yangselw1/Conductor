"""Conductor CLI.

  conductor init                     scaffold a conductor.yaml here
  conductor up                       run the scheduler loop (Ctrl-C to stop)
  conductor add "<request>" [--locks a,b] [--repos x,y]
  conductor status                   queue + running + held locks
  conductor jobs [--all]             recent jobs
  conductor logs <job_id> [-f]       a worker's output
  conductor cancel <job_id>
  conductor tick                     run ONE reap+dispatch pass (for testing / cron)
"""

import argparse
import json
import secrets
import sys
import time

from . import config, engine
from .scope import estimate
from .store import Store

DOT = {"queued": "•", "running": "▶", "merging": "⇄", "done": "✓",
       "failed": "✗", "parked": "‖", "canceled": "⊘"}


def _id():
    return "job_" + secrets.token_hex(3)


def _load():
    cfg = config.load()
    return cfg, Store(cfg)


def cmd_init(args):
    config.init(args.dir)


def cmd_add(args):
    cfg, store = _load()
    explicit = [s.strip() for s in args.locks.split(",") if s.strip()] if args.locks else None
    locks, reason = estimate(args.request, explicit, cfg)
    repos = [s.strip() for s in args.repos.split(",")] if args.repos else config.repos_for_locks(cfg, locks)
    jid = _id()
    store.add(jid, args.request, locks, repos, reason)
    print(f"queued {jid}  locks=[{', '.join(locks)}] repos=[{', '.join(repos)}]  ({reason})")
    print("start the loop with `conductor up` if it isn't already running.")


def cmd_up(args):
    cfg, store = _load()
    _lock = engine.daemon_lock(cfg)  # exclusive: refuse to run two schedulers here (keep ref alive)
    print(f"conductor up — {len(cfg['repos'])} repos, {len(cfg['locks'])} locks, "
          f"max_parallel={cfg['max_parallel']}, tick={cfg['tick_seconds']}s. Ctrl-C to stop.")
    try:
        engine.run_forever(cfg, store)
    except KeyboardInterrupt:
        print("\nstopped (running workers keep going; their merges happen on next `up`/`tick`).")


def cmd_tick(args):
    cfg, store = _load()
    _lock = engine.daemon_lock(cfg)  # never tick while a daemon (or another tick) holds the lock
    engine.run_forever(cfg, store, once=True)
    cmd_status(args)


def cmd_status(args):
    cfg, store = _load()
    held = store.held_locks()
    active = store.by_status("running", "merging", "queued")
    print(f"held locks: {', '.join(sorted(held)) or '(none)'}")
    if not active:
        print("(nothing active)")
        return
    for j in active:
        age = int(time.time() - (j["started"] or j["created"]))
        print(f"  {DOT.get(j['status'],'?')} {j['id']}  {j['status']:<8} "
              f"[{', '.join(json.loads(j['locks']))}]  {age}s  {j['request'][:60]}")


def cmd_jobs(args):
    cfg, store = _load()
    rows = store.all() if args.all else store.all()[:20]
    for j in rows:
        note = (j["note"] or "")[:80]
        print(f"  {DOT.get(j['status'],'?')} {j['id']}  {j['status']:<8} {j['request'][:48]}"
              + (f"   — {note}" if note else ""))


def cmd_logs(args):
    cfg, store = _load()
    j = store.get(args.job_id)
    if not j or not j["log"]:
        raise SystemExit("no log for that job yet")
    if args.follow:
        import subprocess
        subprocess.run(["tail", "-f", j["log"]])
    else:
        sys.stdout.write(open(j["log"]).read())


def cmd_cancel(args):
    cfg, store = _load()
    engine.cancel(cfg, store, args.job_id)
    print(f"canceled {args.job_id}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="conductor", description="Lock-aware orchestrator for parallel AI coding agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("dir", nargs="?", default="."); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("up"); s.set_defaults(fn=cmd_up)
    s = sub.add_parser("tick"); s.set_defaults(fn=cmd_tick)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("jobs"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_jobs)
    s = sub.add_parser("add"); s.add_argument("request"); s.add_argument("--locks"); s.add_argument("--repos"); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("logs"); s.add_argument("job_id"); s.add_argument("-f", "--follow", action="store_true"); s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("cancel"); s.add_argument("job_id"); s.set_defaults(fn=cmd_cancel)

    args = p.parse_args(argv)
    args.fn(args)
