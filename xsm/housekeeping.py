"""What happens to sessions that stopped, and to the records they left behind.

There is no daemon to sweep on a schedule (ADR-0003), so cleanup is
opportunistic: a session starting, or someone running the CLI, gets to prune —
at most once an hour, so it never slows a hook down in a loop.

The retention windows follow from what we measured:

- A stopped session's pointer is kept for `retention_days` (default 7), because
  `claude --resume <id>` comes back with the *same* session id, so the same ref
  and address turn live again. Deleting on exit would break that.
- Ledger entries and held bodies are kept for `ledger_retention_days` (default
  30): long enough to answer "did that ever arrive?", short enough not to grow
  without bound.
"""
from __future__ import annotations

import glob
import json
import os
import time

from . import config, identity, inbox, paths

STAMP = "last-prune"
INTERVAL = 3600.0
DEFAULTS = {"retention_days": 7, "ledger_retention_days": 30, "telemetry_retention_days": 7}


def _setting(name: str) -> float:
    try:
        return float(config.load().get(name, DEFAULTS[name]))
    except (TypeError, ValueError):
        return float(DEFAULTS[name])


def due(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        return now - os.path.getmtime(paths.path(STAMP)) >= INTERVAL
    except OSError:
        return True


def prune(now: float | None = None, dry_run: bool = False) -> dict:
    """Remove what has outlived its window. Returns what was (or would be) removed."""
    now = time.time() if now is None else now
    pointer_cutoff = now - _setting("retention_days") * 86400
    record_cutoff = now - _setting("ledger_retention_days") * 86400
    removed = {"sessions": [], "ledger": [], "held": [], "inbox": [], "telemetry": {}}

    for p in glob.glob(paths.path(paths.SESSIONS, "*.json")):
        rec = paths.read_json(p)
        if not rec:
            continue
        if identity.state_of(rec) == "live":
            continue
        # A goodbye is the moment it stopped; otherwise the last sign of life.
        last_seen = rec.get("ended_at") or rec.get("updated") or 0
        if last_seen < pointer_cutoff:
            removed["sessions"].append(rec.get("name") or os.path.basename(p))
            if not dry_run:
                _unlink(p)

    for p in glob.glob(paths.path(paths.LEDGER, "*.json")):
        entry = paths.read_json(p) or {}
        if (entry.get("t") or os.path.getmtime(p)) < record_cutoff:
            removed["ledger"].append(os.path.basename(p))
            if not dry_run:
                _unlink(p)

    for p in glob.glob(paths.path(paths.HELD, "*.json")):
        entry = paths.read_json(p) or {}
        if (entry.get("t") or os.path.getmtime(p)) < record_cutoff:
            removed["held"].append(os.path.basename(p))
            if not dry_run:
                _unlink(p)

    removed["telemetry"] = prune_telemetry(now, dry_run)

    # A copy for a Codex session that never read it: the queue item it
    # duplicates is gone with the session, so it goes with the pointer window.
    for p in glob.glob(paths.path(inbox.INBOX, "*", "*.json")):
        entry = paths.read_json(p) or {}
        if (entry.get("t") or os.path.getmtime(p)) < pointer_cutoff:
            removed["inbox"].append(os.path.basename(p))
            if not dry_run:
                _unlink(p)

    if not dry_run:
        from . import registry
        registry.mcp_beacons()          # drops beacons of MCP servers that are gone
        _touch(paths.path(STAMP))
    return removed


def maybe_prune() -> dict | None:
    """Called from hot paths. Cheap when not due; never raises."""
    try:
        if not due():
            return None
        from . import workers
        workers.reap_detached()        # workers left by sessions that are over
        return prune()
    except Exception:                  # housekeeping must never break a hook
        return None


def prune_telemetry(now: float, dry_run: bool = False) -> dict:
    """Drop old spans and metric points, but never one still waiting to be
    exported: `xsm otlp-export` remembers how far it has read as a line
    number, so a line dropped from the head would silently skip an unsent one
    (ADR-0011). Lines are dropped from the head only, and the cursor moves
    down by as many.

    Measured for the window's default (2026-09-23): a heavy day of workers and
    pilots wrote 836 spans, 340 kB — a week of that is a few megabytes.
    """
    from . import otlp_export, telemetry
    days = _setting("telemetry_retention_days")
    if days <= 0:
        return {}
    cutoff = now - days * 86400
    cursor = paths.read_json(paths.path(otlp_export.CURSOR), {}) or {}
    dropped = {}
    for name, key in ((telemetry.SPANS, "spans"), (telemetry.METRICS, "points")):
        rows = paths.read_jsonl(name)
        if not rows:
            continue
        exported = int(cursor.get(key, 0))
        cut = 0
        for row in rows[:exported]:
            when = row.get("start") or row.get("t") or 0
            if when >= cutoff:
                break
            cut += 1
        if not cut:
            continue
        dropped[name] = cut
        if dry_run:
            continue
        keep = rows[cut:]
        tmp = paths.path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in keep:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, paths.path(name))
        cursor[key] = max(0, exported - cut)
    if dropped and not dry_run:
        paths.write_json(paths.path(otlp_export.CURSOR), cursor)
    return dropped


def _unlink(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


def _touch(p: str) -> None:
    try:
        paths.ensure_home()
        with open(p, "a"):
            pass
        os.utime(p, None)
    except OSError:
        pass
