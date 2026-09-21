"""The session registry: pointers written by hooks, details read from source.

ADR-0001 draft A': a hook records only what identifies a session (runtime,
home, session id, pid, process start, cwd). Everything that changes while the
session runs — its name, its inbox socket — is read from the runtime's own
files at lookup time, so a cached name can never go stale (S7).

Registration is also consent: a session that never ran the hook is listed as
`unregistered` and cannot be addressed, which keeps a mesh from adopting
sessions behind their back (S9 conclusion 6).
"""
from __future__ import annotations

import glob
import os
import sqlite3
import time

from . import config, identity, paths


def _record_path(runtime: str, session_id: str) -> str:
    safe = session_id.replace("/", "_")
    return paths.path(paths.SESSIONS, "%s-%s.json" % (runtime, safe))


def upsert(runtime: str, home: str, session_id: str, pid: int, cwd: str,
           permission_mode: str | None = None, name: str | None = None) -> dict:
    home = os.path.realpath(os.path.expanduser(home))
    record = paths.read_json(_record_path(runtime, session_id), {}) or {}
    record.update({
        "runtime": runtime,
        "home": home,
        "alias": config.alias_of(home),
        "session_id": session_id,
        "pid": int(pid),
        "lstart": identity.lstart(pid),
        "cwd": os.path.realpath(cwd) if cwd else record.get("cwd"),
        "ref": identity.ref_of(runtime, home, session_id),
        "updated": time.time(),
    })
    if permission_mode:
        record["permission_mode"] = permission_mode
    if name:
        record["name"] = name
    paths.write_json(_record_path(runtime, session_id), record)
    if not any(h.get("path") == home for h in config.homes()):
        config.add_home(home, runtime)
    return record


def _claude_native(home: str, pid) -> dict:
    """Claude's own registry entry: authoritative name and inbox socket.

    nameSource tells us how stable the name is. Observed values: `user` (given
    with --name or /rename), `derived` (built from the folder, e.g.
    graduate-school-path-7b) and `auto` (generated from the conversation). Only
    a user-set name is something a person chose, so anything written down should
    use the ref instead.
    """
    data = paths.read_json(os.path.join(home, "sessions", "%s.json" % pid), {}) or {}
    return {"name": data.get("name"), "socket": data.get("messagingSocketPath"),
            "name_source": data.get("nameSource"),
            "native_session_id": data.get("sessionId")}


def _codex_thread_name(home: str, thread_id: str) -> str | None:
    db = os.path.join(home, "state_5.sqlite")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("select name from threads where id = ?", (thread_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return row[0] if row and row[0] else None


def _enrich(record: dict) -> dict:
    out = dict(record)
    if record.get("runtime") == "claude":
        native = _claude_native(record.get("home", ""), record.get("pid"))
        out["name"] = native["name"] or record.get("name") or "claude-%s" % record.get("pid")
        out["socket"] = native["socket"]
        out["name_source"] = native["name_source"]
    else:
        out["name"] = (_codex_thread_name(record.get("home", ""), str(record.get("session_id") or ""))
                       or record.get("name") or "codex-%s" % str(record.get("session_id"))[:8])
    out["state"] = identity.state_of(out)
    out["registered"] = True
    return out


def records() -> list:
    """Every registered session, newest first."""
    out = []
    for p in glob.glob(paths.path(paths.SESSIONS, "*.json")):
        rec = paths.read_json(p)
        if rec and rec.get("session_id"):
            out.append(_enrich(rec))
    return sorted(out, key=lambda r: r.get("updated", 0), reverse=True)


def unregistered() -> list:
    """Sessions visible in a declared home that never ran the hook. Shown for
    diagnosis only — they have no pointer, so they are not addressable."""
    known = {(r["runtime"], str(r.get("session_id"))) for r in records()}
    known_pids = {(r["runtime"], str(r.get("pid"))) for r in records()}
    out = []
    for home in config.homes():
        if home.get("runtime") != "claude":
            continue
        for p in glob.glob(os.path.join(home["path"], "sessions", "*.json")):
            data = paths.read_json(p, {}) or {}
            sid, pid = str(data.get("sessionId") or ""), str(data.get("pid") or "")
            if not pid or ("claude", sid) in known or ("claude", pid) in known_pids:
                continue
            rec = {"runtime": "claude", "home": home["path"], "alias": home.get("alias"),
                   "session_id": sid, "pid": data.get("pid"), "cwd": data.get("cwd"),
                   "name": data.get("name"), "socket": data.get("messagingSocketPath"),
                   "name_source": data.get("nameSource"),
                   # Claude writes procStart in UTC while `ps lstart` prints local
                   # time, so the two are not comparable; liveness for a session we
                   # did not register rests on pid plus a live socket.
                   "native_start": data.get("procStart"), "registered": False,
                   "ref": identity.ref_of("claude", home["path"], sid)}
            rec["state"] = identity.state_of(rec)
            out.append(rec)
    return out


def by_session(runtime: str, session_id: str) -> dict | None:
    rec = paths.read_json(_record_path(runtime, session_id))
    return _enrich(rec) if rec else None


def prune(max_age_days: float = 14.0) -> int:
    """Drop pointers whose session is gone and whose record is old. Liveness is
    checked at lookup, so pruning is housekeeping, not correctness."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in glob.glob(paths.path(paths.SESSIONS, "*.json")):
        rec = paths.read_json(p)
        if not rec:
            continue
        if identity.state_of(_enrich(rec)) == "stale" and rec.get("updated", 0) < cutoff:
            try:
                os.unlink(p)
                removed += 1
            except OSError:
                pass
    return removed


def me(session_id: str | None = None, cwd: str | None = None):
    """The record for the session running the command.

    Environment first: two sessions can share a working directory, and only the
    session id (or its inbox socket) tells them apart. cwd is the fallback for
    a shell that inherited neither.
    """
    rows = records()
    session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        for rec in rows:
            if rec.get("session_id") == session_id:
                return rec
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if sock:
        pid = identity.pid_from_socket(sock)
        for rec in rows:
            if rec.get("pid") == pid:
                return rec
    # The CLI runs as a descendant of the session process, so walking up to the
    # agent gives an exact answer even when two sessions share a directory.
    own = identity.ancestor_pid({"claude", "codex"})
    if own:
        for rec in rows:
            if rec.get("pid") == own:
                return rec
    cwd = os.path.realpath(cwd or os.getcwd())
    live = [r for r in rows if r.get("cwd") == cwd and r.get("state") == "live"]
    return live[0] if len(live) == 1 else None


def inbound_setting(home: str) -> str | None:
    """The receiver's own crossSessionInbound, read from its user settings.

    Claude decides an explicit setting before it compares permission modes, so
    "accept" is what makes a cross-mode message arrive at all (S1). We can only
    see the user-level file here: a session launched with --settings or with
    project settings may differ, which is why callers treat this as a
    prediction, not a verdict.
    """
    data = paths.read_json(os.path.join(home, "settings.json"), {}) or {}
    value = data.get("crossSessionInbound")
    return value if isinstance(value, str) else None


def mode_class(record: dict) -> str | None:
    """bypass | prompting | None(unknown). Claude compares classes, not modes."""
    mode = record.get("permission_mode")
    if not mode:
        return None
    return "bypass" if mode in ("bypassPermissions", "bypass") else "prompting"
