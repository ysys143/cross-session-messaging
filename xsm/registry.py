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
           permission_mode: str | None = None, name: str | None = None,
           mcp_pid: int | None = None) -> dict:
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
    # A session that registers again is alive again: `claude --resume` comes
    # back with the same session id, so an earlier goodbye no longer applies.
    record.pop("ended_at", None)
    record.pop("end_reason", None)
    record.pop("adopted", None)       # the session's own hook has now spoken for it
    record.pop("unprompted", None)
    if permission_mode:
        record["permission_mode"] = permission_mode
    if name:
        record["name"] = name
    if runtime == "codex":
        # The xsm MCP server serving this thread, or none known (no beacon:
        # the MCP server is not installed in that home, or this is adoption).
        if mcp_pid:
            record["mcp_pid"] = int(mcp_pid)
            record["mcp_lstart"] = identity.lstart(mcp_pid)
        else:
            record.pop("mcp_pid", None)
            record.pop("mcp_lstart", None)
    paths.write_json(_record_path(runtime, session_id), record)
    if not any(h.get("path") == home for h in config.homes()):
        config.add_home(home, runtime)
    return record


def default_codex_name(thread_id: str) -> str:
    """For a thread nobody named. Codex thread ids are UUIDv7, time first:
    two threads opened minutes apart shared their first eight characters
    and so their name (2026-09-23). The random tail tells them apart."""
    return "codex-%s" % thread_id.replace("-", "")[-6:]


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
        out["native_session_id"] = native["native_session_id"]
    else:
        from . import workers           # lazy: workers imports this module
        worker = workers.for_session(record.get("session_id"))
        out["name"] = (_codex_thread_name(record.get("home", ""), str(record.get("session_id") or ""))
                       or (worker or {}).get("name") or record.get("name")
                       or default_codex_name(str(record.get("session_id") or "")))
    out["state"], why = identity.state_reason(out)
    if why == "thread_replaced":
        # The TUI is alive but opened another thread (Codex /new, resume);
        # this one is closed inside it and takes nothing from its queue.
        out["end_reason"] = out.get("end_reason") or "thread_replaced"
    if out["state"] == "live" and record.get("runtime") == "claude" and \
            out.get("native_session_id") and out["native_session_id"] != record.get("session_id"):
        # The process is alive but now runs another session: /clear and
        # --resume change the id in place (measured: three ids on one pid).
        out["state"] = "ended"
        out["end_reason"] = out.get("end_reason") or "superseded"
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


def _codex_recent_threads(home: str, within: float = 86400.0) -> list:
    """Threads touched recently in a Codex home. A Codex session only runs its
    hook at the first prompt, so a thread that exists but never prompted — say,
    only /rename'd — is invisible to the registry. Listing it lets the sender be
    told why instead of "no such session"."""
    db = os.path.join(home, "state_5.sqlite")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "select id, name, cwd, rollout_path, created_at, updated_at from threads "
            "where archived = 0 and updated_at >= ?", (int(time.time() - within),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return rows


def _running_codex() -> list:
    """Codex TUIs running right now: (cwd, start_epoch, resumed_thread_id).

    Without a hook there is no pid in any record, so the process table is the
    only way to tell a thread someone has open from one touched earlier today.
    """
    import subprocess
    try:
        out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, args = parts
        argv = args.split()
        if not argv or os.path.basename(argv[0]) != "codex":
            continue
        if len(argv) > 1 and argv[1] in ("app-server", "mcp", "mcp-server", "exec", "queue"):
            continue
        # "?" is `codex resume` from the picker: some older thread, unknown which.
        resumed = (argv[2] if len(argv) > 2 else "?") if len(argv) > 1 and argv[1] == "resume" \
            else None
        try:
            res = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        cwd = next((row[1:] for row in res.splitlines() if row.startswith("n")), None)
        if not cwd:
            continue
        started = identity.lstart(int(pid))
        try:
            epoch = time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y")) if started else 0
        except ValueError:
            epoch = 0
        found.append((os.path.realpath(cwd), epoch, resumed, int(pid)))
    return found


def _open_codex_threads(home: str) -> list:
    """The one thread each running Codex TUI has open: the one it resumed, or
    the most recently touched thread in its folder since it started."""
    threads = _codex_recent_threads(home, within=7 * 86400)
    chosen = []
    for cwd, epoch, resumed, pid in _running_codex():
        if resumed and resumed != "?":
            chosen += [t + (pid,) for t in threads if t[0] == resumed]
            continue
        here = [t for t in threads if os.path.realpath(t[2] or "") == cwd]
        # A fresh TUI has open only a thread created after it started. Falling
        # back to an older thread in the same folder adopted the wrong one when
        # the new thread was not written yet (measured with a worker whose folder
        # held an earlier worker's thread).
        # A thread opened with /resume inside that TUI was created earlier but is
        # touched after the TUI started; it counts only when no newer one exists.
        since = here if resumed == "?" else (
            [t for t in here if t[4] >= epoch - 5] or [t for t in here if t[5] >= epoch + 2])
        if since:
            chosen.append(max(since, key=lambda t: t[5]) + (pid,))
    return chosen


def adopt_open_codex() -> list:
    """Register open Codex threads that have not run their hook yet.

    Codex runs hooks only from a thread's first prompt, so a freshly started
    (or only /rename'd) session is invisible and cannot be messaged — which is
    exactly when someone wants to hand it its first task. Registration is
    consent (ADR-0001), and here the consent was already given: xsm is
    installed in that Codex home and its hooks are trusted. So the CLI records
    the pointer itself. Homes without trusted hooks are left alone; the sender
    is told why instead.

    The first message delivered becomes the thread's first prompt, at which
    point Codex's own hook re-registers it properly and gates the message.
    Not called from hooks: reading the process table costs a few hundred ms.
    """
    adopted = []
    known = {r.get("session_id") for r in records()}
    for home in config.homes():
        if home.get("runtime") != "codex":
            continue
        from . import install
        trust = install.codex_trust(home["path"])
        if not trust or not all(trust.values()):
            continue
        for row in _open_codex_threads(home["path"]):
            thread_id, name, cwd, _rollout, _created, _updated, pid = row
            if thread_id in known:
                continue
            rec = upsert("codex", home["path"], thread_id, pid, cwd or "", name=name,
                         mcp_pid=beacon_for(pid))
            rec["adopted"] = True
            paths.write_json(_record_path("codex", thread_id), rec)
            adopted.append(rec)
            known.add(thread_id)
    # A thread with no prompt yet is in no Codex table; its beacon and Codex's
    # log name it. Its MCP server's pid keeps its liveness honest.
    trusted = set()
    for home in config.homes():
        if home.get("runtime") == "codex":
            from . import install
            trust = install.codex_trust(home["path"])
            if trust and all(trust.values()):
                trusted.add(home["path"])
    for row in fresh_codex_threads():
        if not row.get("session_id") or row["session_id"] in known or row["home"] not in trusted:
            continue
        rec = upsert("codex", row["home"], row["session_id"], row["pid"], row.get("cwd") or "",
                     mcp_pid=row["mcp_pid"])
        rec["adopted"] = True
        rec["unprompted"] = True
        paths.write_json(_record_path("codex", row["session_id"]), rec)
        adopted.append(rec)
        known.add(row["session_id"])
    return adopted


def claude_home_here() -> str:
    """The Claude home of the session running this command."""
    return os.path.realpath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def self_consent(home: str) -> str | None:
    """Why the calling Claude session cannot be adopted, or None if it can.

    The consent test is the one adopt_open_codex applies to Codex: xsm is
    installed in that home. Only declared homes count, so an XSM_HOME that
    never installed anything (a test's, a spike's) adopts nothing.
    """
    if not any(h.get("runtime") == "claude" and os.path.realpath(h.get("path", "")) == home
               for h in config.homes()):
        return "xsm is not installed in %s; run: xsm install --claude-home %s" % (home, home)
    from . import install                          # lazy: keeps hook imports small
    plan = install.plan(home, "claude")
    ours = [a for a in plan.get("actions", []) if a["event"] == "UserPromptSubmit"]
    if plan.get("error") or not ours or ours[0]["action"] == "add":
        return "the xsm hooks are missing from %s; run: xsm install --claude-home %s" % (
            plan.get("file", home), home)
    return None


def adopt_self() -> dict | None:
    """Register the Claude session running this command when its hook has not
    run yet.

    Installing xsm into a home whose sessions are already open leaves each one
    unregistered until its next prompt, and a display command like /xsm-who
    runs its shell before that prompt's hook fires — so the first thing a
    person tried after installing reported "not registered" (2026-09-22).
    Registration is consent (ADR-0001), and here it was already given: the
    xsm hook is in this session's own settings (see self_consent). The next
    prompt's hook re-registers the session properly and clears "adopted".
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    home = claude_home_here()
    if not sid or self_consent(home):
        return None
    native = next((d for d in (paths.read_json(p, {}) or {}
                               for p in glob.glob(os.path.join(home, "sessions", "*.json")))
                   if str(d.get("sessionId")) == sid), None)
    if not native or not identity.pid_alive(native.get("pid")):
        return None
    rec = upsert("claude", home, sid, native["pid"], native.get("cwd") or os.getcwd())
    rec["adopted"] = True
    paths.write_json(_record_path("claude", sid), rec)
    return by_session("claude", sid) or rec


def mcp_beacons() -> list:
    """Running xsm MCP servers, from the beacons they wrote; a beacon whose
    process is gone is removed here."""
    out = []
    for p in glob.glob(paths.path(paths.MCP, "*.json")):
        rec = paths.read_json(p) or {}
        if rec.get("pid") and identity.pid_alive(rec["pid"]):
            out.append(rec)
        else:
            try:
                os.unlink(p)
            except OSError:
                pass
    return out


def beacon_for(codex_pid) -> int | None:
    """The newest xsm MCP server under this Codex process: the one serving
    the thread the TUI has open now."""
    mine = [b for b in mcp_beacons() if b.get("ppid") == int(codex_pid)]
    return max(mine, key=lambda b: b.get("started", 0))["pid"] if mine else None


def fresh_codex_threads() -> list:
    """Threads a Codex TUI has open that its hook has not registered: it has
    an xsm MCP server (so the thread is open) but no record points at that
    server. Codex writes no thread row, rollout or hook call before the first
    prompt; its log database does name the thread within seconds, and that is
    enough to address it (adapters.thread_of_process). A row with a
    session_id can be adopted; one without is shown as waiting.
    Read once per command, not in hooks: it runs `ps` and reads Codex's logs."""
    from . import adapters
    recs = records()
    served = {r.get("mcp_pid") for r in recs if r.get("runtime") == "codex"}
    claude_pids = {r.get("pid") for r in recs if r.get("runtime") == "claude"}
    codex_homes = [h["path"] for h in config.homes() if h.get("runtime") == "codex"]
    out = []
    for b in mcp_beacons():
        ppid = b.get("ppid")
        if not ppid or b["pid"] in served or ppid in claude_pids or not identity.pid_alive(ppid):
            continue
        if not any(r.get("pid") == ppid for r in recs) and identity.comm(ppid) != "codex":
            continue
        thread, home = None, None
        for h in codex_homes:
            thread = adapters.thread_of_process(h, ppid, b.get("started") or time.time())
            if thread:
                home = h
                break
        age = int(time.time() - b.get("started", time.time()))
        row = {"runtime": "codex", "home": home or "", "alias": config.alias_of(home) if home
               else "codex", "session_id": thread, "pid": ppid, "mcp_pid": b["pid"],
               "name": "codex-%d" % ppid, "cwd": b.get("cwd"), "registered": False,
               "state": "unknown",
               "ref": identity.ref_of("codex", home, thread) if thread and home else None,
               "fresh": True}
        # The wording is the whole message here: "no prompt yet" was read as
        # "send it a prompt" by a model answering from it (eval, 2026-09-23),
        # which is the one thing that cannot be done.
        row["why"] = ("a Codex window nobody has typed in yet (%dm%02ds)%s" % (
            age // 60, age % 60, "" if thread else
            "; it takes an address once its own user types there or runs /rename, and until "
            "then it cannot be messaged"))
        out.append(row)
    return out


def unregistered() -> list:
    """Sessions visible in a declared home that never ran the hook. Shown for
    diagnosis only — they have no pointer, so they are not addressable."""
    known = {(r["runtime"], str(r.get("session_id"))) for r in records()}
    known_pids = {(r["runtime"], str(r.get("pid"))) for r in records()}
    out = []
    for home in config.homes():
        if home.get("runtime") == "codex":
            from . import install                         # lazy: keeps hook imports small
            trust = install.codex_trust(home["path"])
            for thread_id, name, cwd, rollout, _created, _updated, _pid in _open_codex_threads(home["path"]):
                if ("codex", str(thread_id)) in known:
                    continue
                if trust and not all(trust.values()):
                    why = "the xsm hooks are not trusted in %s; start codex and choose " \
                          "'Trust all and continue'" % home["path"]
                elif not rollout or not os.path.exists(rollout):
                    why = "no prompt yet; Codex registers a session at its first prompt"
                else:
                    why = "its hook has not run since it started"
                out.append({"runtime": "codex", "home": home["path"], "alias": home.get("alias"),
                            "session_id": thread_id, "name": name or default_codex_name(thread_id),
                            "cwd": cwd, "registered": False, "state": "unknown", "why": why,
                            "ref": identity.ref_of("codex", home["path"], thread_id)})
            continue
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
    out += fresh_codex_threads()
    return out


def mark_ended(runtime: str, session_id: str, reason: str | None) -> dict | None:
    """Record a clean exit. Only the SessionEnd hook calls this; a crash never
    does, which is exactly how `ended` and `stale` come apart."""
    p = _record_path(runtime, session_id)
    rec = paths.read_json(p)
    if not rec:
        return None
    rec["ended_at"] = time.time()
    rec["end_reason"] = reason or "unknown"
    paths.write_json(p, rec)
    return rec


def cheap_records() -> list:
    """Pointers whose process is still alive, without the expensive checks.

    `records()` runs `ps` per session and probes each socket, which is right for
    addressing but far too slow for something drawn on every keystroke. Here a
    dead pid is enough to drop a row; a recycled pid may survive one render.
    """
    out = []
    for p in glob.glob(paths.path(paths.SESSIONS, "*.json")):
        rec = paths.read_json(p)
        if rec and rec.get("pid") and identity.pid_alive(rec["pid"]):
            out.append(rec)
    return out


def by_session(runtime: str, session_id: str) -> dict | None:
    rec = paths.read_json(_record_path(runtime, session_id))
    return _enrich(rec) if rec else None


def me(session_id: str | None = None, cwd: str | None = None):
    """The record for the session running the command.

    Environment first: two sessions can share a working directory, and only the
    session id (or its inbox socket) tells them apart. cwd is the fallback for
    a shell that inherited neither — never for one that knows its session id,
    where a cwd match would be some other session's identity.
    """
    rec, how = _me(session_id, cwd)
    try:
        from . import telemetry
        telemetry.annotate("xsm.identity.via", how)
    except ImportError:
        pass
    return rec


def _me(session_id: str | None, cwd: str | None) -> tuple:
    """(record or None, which rule found it) — the rule is what a span of a
    refused command needs, since "cannot tell who is posting" has one cause
    per rule that could have matched and did not."""
    rows = records()
    own_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    session_id = session_id or own_session
    if session_id:
        for rec in rows:
            if rec.get("session_id") == session_id:
                return rec, "session_id"
    # Codex puts its thread id in every shell it runs, and the thread id is
    # what a Codex record is keyed by. It has to come before the process walk:
    # the Codex sandbox refuses to run `ps` ("operation not permitted",
    # measured 2026-09-22), so inside it the walk always fails, and two
    # sandboxed sessions sharing a folder could not tell which one they were.
    thread = os.environ.get("CODEX_THREAD_ID")
    if thread:
        for rec in rows:
            if rec.get("runtime") == "codex" and rec.get("session_id") == thread:
                return rec, "codex_thread_id"
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if sock:
        pid = identity.pid_from_socket(sock)
        for rec in rows:
            if rec.get("pid") == pid:
                return rec, "socket"
    # The CLI runs as a descendant of the session process, so walking up to the
    # agent gives an exact answer even when two sessions share a directory.
    own = identity.ancestor_pid({"claude", "codex"})
    if own:
        for rec in rows:
            if rec.get("pid") == own:
                return rec, "ancestor"
    if own_session and session_id == own_session:
        adopted = adopt_self()
        return adopted, "adopted" if adopted else "none:unregistered-session"
    cwd = os.path.realpath(cwd or os.getcwd())
    live = [r for r in rows if r.get("cwd") == cwd and r.get("state") == "live"]
    if len(live) == 1:
        return live[0], "cwd"
    return None, "none:cwd-%d-live" % len(live)


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
