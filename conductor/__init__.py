"""Conductor — a lock-aware orchestrator for parallel AI coding agents across many repos.

Fire a continuous stream of dev requests. Conductor runs non-conflicting work in parallel as
*full* headless Claude Code sessions in isolated git worktrees, queues conflicting work behind
the lock holder, then merges + verifies each result automatically. Generic by design: point it
at any set of repos through conductor.yaml — nothing here is specific to one project.

The model is classic concurrency control applied to AI agents:
  - a request acquires one or more named LOCKS (carved from your codebase in conductor.yaml),
  - disjoint locks  -> run in parallel,
  - overlapping locks -> the later one waits for the holder,
  - each finished worker is merged into its repo's default branch and verified before its locks
    are released and queued work behind it is drained.
"""

__version__ = "1.0.0"
