# Conductor

**A lock-aware orchestrator for parallel AI coding agents across many repos.**

Fire a continuous stream of development requests. Conductor runs the non-conflicting ones in
parallel — each as a *full* headless Claude Code session in its own isolated git worktree — queues
the ones that would collide behind whoever holds the lock, and merges + verifies every result
automatically before releasing its locks. You keep typing requests; it keeps the work coherent.

It is deliberately a small idea borrowed from old, solid engineering: **classic concurrency
control (locks + a scheduler + a merge gate), with AI agents as the workers and a verify command
as the consistency check.** Nothing in the core is specific to any one project — you describe your
system once in `conductor.yaml` and point it at any set of repos.

---

## Why

Running several agents at once across coupled apps has two distinct failure modes:

| Failure | What stops it |
|---|---|
| Two agents overwrite the **same file** | **Worktrees** — each worker edits a private checkout; git surfaces any overlap at merge, loudly |
| Two agents change **two halves of one contract** and they no longer fit | **Locks** — seam regions get a single coarse lock, so that work is owned by one worker, both sides at once |

Conductor turns both into bookkeeping so you don't have to track them by hand.

## The model

```
  you ──fire requests──▶  intake queue ──▶ scheduler ──▶ lock table (derived from running jobs)
                                                │
                                  locks free? ──┴── locks busy?
                                       │                │
                                  acquire +         leave queued
                                  spawn worker      (waits for the holder)
                                       │
                          full `claude -p` session in its own worktree(s)
                                       │ exits
                          MERGE GATE: commit → merge all-or-nothing → verify
                                       │ pass → release locks, drain queue
                                       │ fail → roll back, park for you with the log
```

- **Disjoint locks → parallel**, up to `max_parallel`.
- **Overlapping locks → the later job waits**, and only for the specific holder.
- **Each worker is a real Claude Code session** (full tools, MCP, skills, model) — not a degraded
  subagent. Multi-repo (seam) jobs get a worktree per repo, wired together with `--add-dir`.
- **The merge gate enforces scope and is all-or-nothing**: it first checks the worker only edited
  files inside its lock's globs (out-of-scope edits are rejected), then merges each repo in sequence
  and verifies. If *anything* fails — out-of-scope edit, merge conflict, or a failed verify — every
  merge it made is rolled back (`git reset --hard`) and the job is parked with its branch + log kept.
- **Single scheduler**: `conductor up`/`tick` hold an exclusive daemon lock, so the lock table and
  merges are never raced by a second instance.

## The lock map is the whole design

In `conductor.yaml` you carve each repo into **named locks**. Narrow locks parallelize; coarse
"seam" or "shared-core" locks deliberately serialize the dangerous shared areas:

```yaml
locks:
  mosaic-quant: { repo: cortex-engines, paths: ["services/equity/**"] }   # narrow → runs alongside others
  prism:        { repo: cortex-engines, paths: ["api/generate-prism*"] }  # narrow
  mc-shared:    { repo: cortex-engines, paths: ["services/gemini.ts", "api/_lib/**"] }   # coarse → blocks engine work
  atlas-seam:   { repos: [cortex-engines, cortex], paths: [...] }          # cross-repo contract → one owner
```

A request acquires one or more locks (you pass `--locks`, or let the optional LLM **scout** infer
them). Two requests with disjoint lock sets run at once; overlapping sets serialize.

## Usage

```bash
conductor init                 # scaffold conductor.yaml, then edit repos / locks / verify
conductor up                   # run the scheduler loop (leave it running)

# from anywhere, fire requests — they queue and flow:
conductor add "tighten Mosaic's reverse-DCF terminal-value handling" --locks mosaic-quant
conductor add "rewrite PRISM's adversary prompt"                     --locks prism
conductor add "add a sector field to the equity packet + Atlas reader" --locks atlas-seam

conductor status               # held locks + what's running / queued
conductor jobs                 # recent history
conductor logs job_ab12cd -f   # tail a worker
conductor cancel job_ab12cd
```

`add` is fire-and-forget. The first two above run **in parallel** (disjoint); a second
`mosaic-quant` request would **wait** for the first to merge. The seam job takes a worktree in
both repos and owns both sides of the contract.

## Configuration (`conductor.yaml`)

- `repos:` name → path (absolute or relative to the config file).
- `locks:` name → `{ repo | repos, paths:[globs], desc }`. **This is where you encode what may run
  in parallel.** Break monolith files into separate locks to unlock more parallelism.
- `verify:` per-repo shell command run at the merge gate (e.g. `tsc --noEmit`, `validate.py`), plus
  an optional `global:` cross-repo round-trip / contract test. A job is only "done" if verify passes.
- `worker.command:` the worker process; `{brief}` is substituted. Default is a full Claude Code
  session. Swap it for any agent CLI — Conductor only needs it to edit the worktree and exit.
- `scope.scout:` when true, an LLM infers a request's locks if you don't pass `--locks`
  (needs `ANTHROPIC_API_KEY`, via env or `scope.env_file`).

## Honest limits

- A genuinely overlapping follow-up **waits** — that's the point, not a bug.
- Scope estimates are conservative; a worker that strays outside its lock surfaces at the merge
  gate (conflict) rather than silently. Unscoped requests take the global lock and serialize.
- A failed verify/merge **parks** the job (branch + log kept) rather than guessing — that's the one
  place a human decision is the right call.

## Layout

```
conductor/
  conductor/      config.py · store.py · scope.py · engine.py · cli.py
  bin/conductor   launcher (no install needed)
  conductor.example.yaml
```

Requires Python 3.9+, git, and `pyyaml`. Workers require whatever your `worker.command` is
(by default, the `claude` CLI on PATH).
