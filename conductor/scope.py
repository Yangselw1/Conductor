"""Scope estimation: turn a free-text request into the set of named locks it will touch.

Precedence:  explicit `--locks`  >  optional LLM scout  >  conservative global lock ("*").
The "*" lock conflicts with everything, so an unscoped request serializes safely rather than
racing blind. The scout is best-effort and never blocks the loop.
"""

import json
import os
import urllib.request


def conflict(a, b, cfg):
    """Do two lock sets conflict? Two locks conflict if they share a repo and their path globs
    overlap (precomputed in cfg['_lock_conflicts']); the global lock '*' conflicts with anything."""
    a, b = set(a), set(b)
    if "*" in a or "*" in b:
        return True
    graph = cfg.get("_lock_conflicts", {})
    return any(graph.get(la, {la}) & b for la in a)


def estimate(request, explicit, cfg):
    valid = set(cfg["locks"])
    if explicit:
        bad = [l for l in explicit if l not in valid and l != "*"]
        if bad:
            raise SystemExit(f"unknown lock(s): {', '.join(bad)}  (defined: {', '.join(sorted(valid))})")
        return list(dict.fromkeys(explicit)), "explicit"
    if cfg["scope"].get("scout"):
        try:
            locks = _scout(request, cfg)
            if locks:
                return locks, "scout"
        except Exception as e:  # noqa: BLE001 — never let scoping break the loop
            return ["*"], f"scout failed ({e}); global lock"
    return ["*"], "no --locks and scout off; global lock (serialized)"


def _api_key(cfg):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    ef = cfg["scope"].get("env_file")
    if ef and os.path.exists(ef):
        for line in open(ef):
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("no ANTHROPIC_API_KEY (set env or scope.env_file)")


def _scout(request, cfg):
    """Ask a fast model which named locks a request touches. Returns a list of valid lock names."""
    catalog = "\n".join(
        f"- {n}: {d.get('desc', '')} [{', '.join(d['_repos'])}: {', '.join(d.get('paths', []))}]"
        for n, d in cfg["locks"].items()
    )
    prompt = (
        "You are a code-scope router. Given a development request and a catalog of named locks "
        "(each = a region of one or more repos), return ONLY a JSON array of the lock names the "
        "request will need to edit. Be conservative: include a lock if the work plausibly touches "
        "its paths. If a request spans a contract between repos, include the seam lock.\n\n"
        f"LOCKS:\n{catalog}\n\nREQUEST:\n{request}\n\nReturn JSON array only, e.g. [\"mosaic-quant\"]."
    )
    body = json.dumps({
        "model": cfg["scope"]["scout_model"],
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                 "x-api-key": _api_key(cfg)},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        text = json.loads(r.read())["content"][0]["text"]
    start, end = text.find("["), text.rfind("]")
    names = json.loads(text[start:end + 1]) if start >= 0 else []
    valid = set(cfg["locks"])
    return [n for n in names if n in valid] or ["*"]
