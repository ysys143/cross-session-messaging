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
import json
import os
import sqlite3
import time

from . import config, identity, paths


def _record_path(runtime: str, session_id: str) -> str:
    safe = session_id.replace("/", "_")
    return paths.path(paths.SESSIONS, "%s-%s.json" % (runtime, safe))


def upsert(runtime: str, home: str, session_id: str, pid: int, cwd: str,
           permission_mode: str | None = None, name: str | None = None,
           mcp_pid: int | None = None, socket: str | None = None,
           inside: str | None | bool = False) -> dict:
    home = os.path.realpath(os.path.expanduser(home))
    record = paths.read_json(_record_path(runtime, session_id), {}) or {}
    previous_pid = record.get("pid")
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
    if runtime == "claude":
        # The inbox socket as Claude Code exports it to hooks
        # (CLAUDE_CODE_MESSAGING_SOCKET, documented), so sending does not have
        # to find it in Claude's own undocumented sessions/<pid>.json. The file
        # is named after the pid, so a path for another pid is not this one's.
        if socket and identity.pid_from_socket(socket) == int(pid):
            record["socket"] = socket
        elif previous_pid != int(pid) or socket:
            record.pop("socket", None)
    if runtime == "codex":
        # The xsm MCP server serving this thread, or none known (no beacon:
        # the MCP server is not installed in that home, or this is adoption).
        if mcp_pid:
            record["mcp_pid"] = int(mcp_pid)
            record["mcp_lstart"] = identity.lstart(mcp_pid)
        else:
            record.pop("mcp_pid", None)
            record.pop("mcp_lstart", None)
        # Whether the pid is the home's app-server daemon rather than a TUI:
        # then the pid is every hosted thread's, and liveness asks the daemon.
        # Written both ways: a record with no key is a legacy one, and only
        # those cost a process-table probe when liveness is judged.
        record["app_server"] = identity.is_app_server(pid)
    if inside is not False:
        # The other runtime's id this session was started with (its own hook
        # inherits it): the session runs inside that one's shell. Only the
        # session's own hook knows it; adoption leaves it as it was.
        if inside:
            record["inside"] = str(inside)
        else:
            record.pop("inside", None)
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


def codex_rollout(home: str, thread_id: str) -> str | None:
    """The rollout file of a Codex thread: its `threads` row names it, and the
    file name ends in the thread id when the row cannot be read."""
    db = os.path.join(home, "state_5.sqlite")
    if os.path.exists(db):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
            try:
                row = con.execute("select rollout_path from threads where id = ?",
                                  (thread_id,)).fetchone()
            finally:
                con.close()
            if row and row[0] and os.path.exists(row[0]):
                return row[0]
        except sqlite3.Error:
            pass
    found = glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % thread_id))
    return max(found) if found else None


ROLLOUT_TAIL = 256 * 1024


def codex_interrupted(home: str, thread_id: str) -> bool:
    """Whether the thread's last turn ended with Esc and none has started since.

    Codex's queue does not start anything in such a thread until its user
    types (0.158; ADR-0002 appendix), so `xsm list` says so. The rollout's
    last turn event decides: `turn_aborted` with reason `interrupted`, not
    followed by `task_started` or `task_complete` (measured 2026-09-28).
    Reads only the file's tail; a turn longer than that reads as not
    interrupted, which only loses the note."""
    path = codex_rollout(home, thread_id) if home and thread_id else None
    if not path:
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - ROLLOUT_TAIL))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in reversed(tail.splitlines()):
        if '"turn_aborted"' not in line and '"task_started"' not in line and \
                '"task_complete"' not in line:
            continue
        try:
            payload = (json.loads(line).get("payload") or {})
        except (ValueError, AttributeError):
            continue
        kind = payload.get("type") if isinstance(payload, dict) else None
        if kind == "turn_aborted":
            return payload.get("reason") == "interrupted"
        if kind in ("task_started", "task_complete"):
            return False
    return False


def _enrich(record: dict) -> dict:
    out = dict(record)
    if record.get("runtime") == "claude":
        native = _claude_native(record.get("home", ""), record.get("pid"))
        out["name"] = native["name"] or record.get("name") or "claude-%s" % record.get("pid")
        out["socket"] = record.get("socket") or native["socket"]
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
    elif why == "thread_unloaded":
        # No TUI holds the thread any more; the daemon that hosted it let it go.
        out["end_reason"] = out.get("end_reason") or "thread_unloaded"
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
        resumed = _resumed_thread(argv)
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


def _resumed_thread(argv: list) -> str | None:
    """The thread a `codex ... resume <id>` command line opened; "?" for
    `codex resume` from the picker (some older thread, unknown which); None
    when it resumed nothing. Options may come first: `codex --no-alt-screen
    resume <id>` was read as a fresh TUI and handed a newer thread in its
    folder — a stopped worker's (issue #5)."""
    if "resume" not in argv[1:]:
        return None
    rest = argv[argv.index("resume", 1) + 1:]
    return rest[0] if rest and not rest[0].startswith("-") else "?"


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


def _hooks_will_register(home: str) -> bool:
    """Whether Codex will run both xsm hooks in this home. SessionStart alone
    is not enough: UserPromptSubmit is the gate every message passes, and a
    home trusting only SessionStart was adopted into a session that could
    never take a message (adversarial review, 2026-09-28). workers.spawn
    requires the same two."""
    from . import install                          # lazy: keeps hook imports small
    trust = install.codex_trust(home)
    return all(trust.get(k) for k in ("SessionStart", "UserPromptSubmit"))


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
    trusted = {h["path"] for h in config.homes()
               if h.get("runtime") == "codex" and _hooks_will_register(h["path"])}
    for home in config.homes():
        if home["path"] not in trusted:
            continue
        from . import codex_daemon               # lazy: keeps hook imports small
        for row in _open_codex_threads(home["path"]):
            thread_id, name, cwd, _rollout, _created, _updated, pid = row
            if thread_id in known:
                continue
            loaded = codex_daemon.loaded_threads(home["path"])     # cached per home
            if loaded is not None and thread_id not in loaded:
                # The TUI-and-folder match is a guess, and it handed a closed
                # thread to whichever TUI was open in that folder (issue #5).
                # Where the home's daemon answers, a thread it has not loaded
                # is not open in any of its TUIs; one open in an embedded TUI
                # is registered by its own hook at its first prompt instead.
                continue
            rec = upsert("codex", home["path"], thread_id, pid, cwd or "", name=name,
                         mcp_pid=beacon_for(pid))
            rec["adopted"] = True
            paths.write_json(_record_path("codex", thread_id), rec)
            adopted.append(rec)
            known.add(thread_id)
    # A thread with no prompt yet is in no Codex table; its beacon and Codex's
    # log name it. Its MCP server's pid keeps its liveness honest.
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
    unregistered until its next prompt, and a display command like /xsm who
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
    rec = upsert("claude", home, sid, native["pid"], native.get("cwd") or os.getcwd(),
                 socket=os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))
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
            trusted = not trust or all(trust.get(k) for k in ("SessionStart", "UserPromptSubmit"))
            for thread_id, name, cwd, rollout, _created, _updated, _pid in _open_codex_threads(home["path"]):
                if ("codex", str(thread_id)) in known:
                    continue
                if not trusted:
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


def mark_ended(runtime: str, session_id: str, reason: str | None,
               home: str | None = None) -> dict | None:
    """Record a clean exit. The SessionEnd hook calls this, and so does
    stopping a worker (reason `worker-stopped`); a crash never does, which is
    exactly how `ended` and `stale` come apart.

    `home` is where the ending session lives. Pointers are keyed on runtime
    and session id alone, so a thread id that exists in two CODEX_HOMEs (a
    copied home) names one pointer; a goodbye from the other home used to mark
    it ended and drop its reaches while it ran on (2026-09-28). A goodbye whose
    home is not the pointer's leaves the pointer alone."""
    p = _record_path(runtime, session_id)
    rec = paths.read_json(p)
    if not rec:
        return None
    if home and os.path.realpath(os.path.expanduser(home)) != \
            os.path.realpath(rec.get("home") or ""):
        paths.append_jsonl("decisions.jsonl", {
            "event": "SessionEnd", "runtime": runtime, "decision": "end-ignored",
            "reason": "the goodbye came from %s, the pointer is for %s" % (home, rec.get("home")),
            "session_id": session_id})
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


def _runtime_here(rows: list | None = None) -> str | None:
    """Which runtime's shell this is: "claude", "codex", "ambiguous" for a
    shell carrying both ids that nothing attributes, or None for a shell
    that carries neither runtime's id.

    Each runtime sets its own id in the shells it runs, but leaves the other's
    alone: a Codex started from a shell a Claude session made (a tmux server
    first opened there, say) still carries that Claude's id, and took that
    session for itself (2026-09-27). With both set, the nearer agent process
    decides. Where the walk cannot run, that is not proof of Codex: a Claude
    shell in its own OS sandbox is refused `ps` too (adversarial review,
    2026-09-28), and a Claude started from a Codex shell carries that
    thread's id — so _nested_runtime weighs what else there is."""
    own_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    thread = os.environ.get("CODEX_THREAD_ID")
    if not (own_session and thread):
        return "claude" if own_session else "codex" if thread else None
    rows = rows if rows is not None else records()
    own = identity.ancestor_pid({"claude", "codex"})
    if not own:
        return _nested_runtime(rows, own_session, thread)
    rec = next((r for r in rows if r.get("pid") == own), None)
    if rec and rec.get("runtime") in ("claude", "codex"):
        return rec["runtime"]
    return "claude" if identity.comm(own) == "claude" else "codex"


def _started(rec: dict | None) -> float | None:
    try:
        return time.mktime(time.strptime(rec["lstart"], "%a %b %d %H:%M:%S %Y"))
    except (TypeError, KeyError, ValueError):
        return None


def _nested_runtime(rows: list, own_session: str, thread: str) -> str:
    """The runtime of a shell carrying both ids when the process walk cannot
    run. One of the two sessions was started from the other's shell, so:
    a session whose process is gone is not the one running this; of two
    live ones the one whose hook saw the other's id is the inner one; else
    the later started is, the one whose shell this
    is (both start times were taken by hooks, outside any sandbox); then
    the sandbox markers each side sets (xsm's for a Claude worker, Codex's
    own); then whichever id has a record. Only when none of that points
    either way is the answer "ambiguous" — no record rather than another
    session's."""
    sock_pid = identity.pid_from_socket(os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") or "")
    claude = next((r for r in rows if r.get("runtime") == "claude" and (
        r.get("pid") == sock_pid if sock_pid else r.get("session_id") == own_session)), None)
    codex = next((r for r in rows if r.get("runtime") == "codex"
                  and r.get("session_id") == thread), None)
    claude_pid = sock_pid or (claude or {}).get("pid")
    if claude_pid and not identity.pid_alive(claude_pid):
        return "codex"
    if codex and not identity.pid_alive(codex.get("pid")):
        return "claude"
    # What each session's own hook saw: the other runtime's id in the
    # environment it was started with means it runs inside that one's shell,
    # so it is the inner session, whose shell this is. Start order said the
    # opposite once the outer Codex resumed and its record's start time
    # became the newer one (review, 2026-09-28).
    claude_inside = (claude or {}).get("inside") == thread
    codex_inside = (codex or {}).get("inside") == own_session
    if claude_inside != codex_inside:
        return "claude" if claude_inside else "codex"
    ours, theirs = _started(claude), _started(codex)
    if ours and theirs and ours != theirs:
        return "claude" if ours > theirs else "codex"
    if os.environ.get("XSM_SANDBOXED"):
        return "claude"
    if os.environ.get("CODEX_SANDBOX"):
        return "codex"
    if bool(claude) != bool(codex):
        return "claude" if claude else "codex"
    return "ambiguous"


def _me(session_id: str | None, cwd: str | None) -> tuple:
    """(record or None, which rule found it) — the rule is what a span of a
    refused command needs, since "cannot tell who is posting" has one cause
    per rule that could have matched and did not.

    The shell's own runtime is settled first, and only that runtime's id is
    looked up. Falling through to the other rules from a shell that knows its
    id handed it some other session's identity: a Codex thread not registered
    yet took whichever live session shared its folder (via=cwd), and a Codex
    carrying an inherited Claude id took that Claude session (adversarial
    review, 2026-09-28) — and `xsm send` then signed as them."""
    rows = records()
    if session_id:
        # A hook names its session outright; that is not an inherited guess.
        rec = next((r for r in rows if r.get("session_id") == session_id), None)
        return (rec, "session_id") if rec else (None, "none:unregistered-session")
    runtime = _runtime_here(rows)
    if runtime == "ambiguous":
        return None, "none:ambiguous-runtime"
    if runtime == "codex":
        # Codex puts its thread id in every shell it runs, and the thread id is
        # what a Codex record is keyed by. Only that key counts: the process
        # walk cannot run in the Codex sandbox, and where it can, a pid match
        # may be an older thread of the same TUI after /new.
        thread = os.environ.get("CODEX_THREAD_ID")
        for rec in rows:
            if rec.get("runtime") == "codex" and rec.get("session_id") == thread:
                return rec, "codex_thread_id"
        return None, "none:codex-thread-unregistered"
    own_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if runtime == "claude":
        for rec in rows:
            if rec.get("session_id") == own_session:
                return rec, "session_id"
    # /clear and --resume give the same Claude process a new session id before
    # its hook records it, so the rules that name the process still hold.
    same_runtime = (lambda rec: rec.get("runtime") == "claude") if runtime else (lambda rec: True)
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if sock:
        pid = identity.pid_from_socket(sock)
        for rec in rows:
            if rec.get("pid") == pid and same_runtime(rec):
                return rec, "socket"
    # The CLI runs as a descendant of the session process, so walking up to the
    # agent gives an exact answer even when two sessions share a directory.
    own = identity.ancestor_pid({"claude", "codex"})
    if own:
        for rec in rows:
            if rec.get("pid") == own and same_runtime(rec):
                return rec, "ancestor"
    if runtime == "claude":
        adopted = adopt_self()
        return adopted, "adopted" if adopted else "none:unregistered-session"
    # cwd only for a shell that carries neither id — never for one that knows
    # its session id, where a cwd match would be some other session's identity.
    cwd = os.path.realpath(cwd or os.getcwd())
    live = [r for r in rows if r.get("cwd") == cwd and r.get("state") == "live"]
    if len(live) == 1:
        return live[0], "cwd"
    return None, "none:cwd-%d-live" % len(live)


def unregistered_reason() -> str:
    """What to tell a session that has no record. A Codex thread is registered
    by its hooks at its next prompt, so right after installing or trusting
    them the thread running the command is not known yet — "no hook record for
    this cwd" read as a broken install (a tester's report, 2026-09-28). The
    runtime is decided as `me` decides it: a Codex that inherited a Claude
    session id was told to install xsm into that Claude's home."""
    runtime = _runtime_here()
    if runtime == "ambiguous":
        return ("this shell carries both a Claude Code session id (%s) and a Codex thread id (%s) "
                "- one runtime was started from the other's shell - and nothing here tells which "
                "session runs it, so xsm will not sign as either; run it from the session's own "
                "shell, or run `xsm doctor`" % (os.environ.get("CLAUDE_CODE_SESSION_ID"),
                                                os.environ.get("CODEX_THREAD_ID")))
    if runtime == "codex":
        return ("this Codex thread (%s) is not registered yet: xsm's hooks register a thread at "
                "its next prompt, so if they were just installed or trusted, send any message in "
                "this session and try again; if it persists, run `xsm doctor`"
                % os.environ.get("CODEX_THREAD_ID"))
    why = self_consent(claude_home_here()) if runtime == "claude" else None
    if why:
        return "this session is not registered: %s" % why
    return ("this session is not registered: no hook record for it. If xsm was just installed, "
            "send any message in the session first; otherwise run `xsm doctor`")


INBOUND_LADDER = ("accept", "hold", "refuse")


def inbound_setting(home: str, cwd: str | None = None) -> str | None:
    """The receiver's crossSessionInbound: its user settings, tightened by the
    project's.

    Claude decides an explicit setting before it compares permission modes, so
    "accept" is what makes a cross-mode message arrive at all (S1). A project's
    .claude/settings.json or settings.local.json may only tighten it along
    accept < hold < refuse; Claude 2.1.284 held a message from a repository set
    to "hold" while the user settings said "accept", and xsm had forecast it as
    accepted (2026-09-29). A session launched with --settings or
    --setting-sources may still differ, which is why callers treat this as a
    prediction, not a verdict.
    """
    def read(path):
        value = (paths.read_json(path, {}) or {}).get("crossSessionInbound")
        return value if value in INBOUND_LADDER else None
    value = read(os.path.join(home, "settings.json"))
    for name in ("settings.json", "settings.local.json"):
        stricter = read(os.path.join(cwd, ".claude", name)) if cwd else None
        if stricter and (value is None or
                         INBOUND_LADDER.index(stricter) > INBOUND_LADDER.index(value)):
            value = stricter
    return value


def mode_class(record: dict) -> str | None:
    """bypass | prompting | None(unknown). Claude compares classes, not modes."""
    mode = record.get("permission_mode")
    if not mode:
        return None
    return "bypass" if mode in ("bypassPermissions", "bypass") else "prompting"
