"""Durable job + lock state (stdlib sqlite3, WAL so the CLI and daemon can share it).

The lock table is *derived*, not stored: the locks currently held = the union of the `locks` of
every job in a `running` or `merging` state. That keeps locks and job state impossible to desync.
"""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id           TEXT PRIMARY KEY,
  request      TEXT NOT NULL,
  locks        TEXT NOT NULL,     -- json list of lock names
  repos        TEXT NOT NULL,     -- json list of repo keys (FIFO order)
  status       TEXT NOT NULL,     -- queued|running|merging|done|failed|parked|canceled
  branch       TEXT,
  worktrees    TEXT,              -- json {repo_key: path}
  pid          INTEGER,
  log          TEXT,
  scope_reason TEXT,
  note         TEXT,
  created      REAL,
  started      REAL,
  finished     REAL
);
"""

ACTIVE = ("running", "merging")


class Store:
    def __init__(self, cfg):
        d = Path(cfg["_dir"]) / ".conductor"
        d.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(d / "state.db"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add(self, jid, request, locks, repos, scope_reason):
        self.db.execute(
            "INSERT INTO jobs (id,request,locks,repos,status,scope_reason,created) "
            "VALUES (?,?,?,?,'queued',?,?)",
            (jid, request, json.dumps(locks), json.dumps(repos), scope_reason, time.time()),
        )
        self.db.commit()

    def update(self, jid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))
        self.db.commit()

    def get(self, jid):
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def by_status(self, *statuses):
        q = ",".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY created ASC", statuses
        ).fetchall()

    def all(self):
        return self.db.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()

    def held_locks(self):
        """Set of lock names currently held by running/merging jobs."""
        held = set()
        for row in self.by_status(*ACTIVE):
            held |= set(json.loads(row["locks"]))
        return held
