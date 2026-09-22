"""Liveness probes and stable references for a session.

A record left behind by a killed session looks exactly like a live one, so
liveness is decided at lookup time, never by a shutdown hook: `kill -9` leaves
no SessionEnd (S3). pid alone is not enough either — pids get reused — so the
process start time from `ps` is part of the check, the same rule cc-peer's own
roster uses.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess


def lstart(pid: int) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def comm(pid) -> str | None:
    """The process's command name, or None if `ps` cannot say."""
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.basename(out.stdout.strip()) or None


def pid_alive(pid) -> bool:
    """Whether a process with this pid exists. EPERM says it does — we are
    just not allowed to signal it — and inside the Codex sandbox that is the
    answer for every other session's pid (measured through xsm's own spans,
    2026-09-22: every peer read as dead from inside, live from the hooks)."""
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        try:
            from . import telemetry
            telemetry.bump("xsm.state.kill_eperm")
        except ImportError:
            pass
        return True
    except OSError as err:
        try:
            from . import telemetry
            telemetry.bump("xsm.state.kill_errno_%s" % err.errno)
        except ImportError:
            pass
        return False
    except (TypeError, ValueError):
        return False


def socket_live(sock_path: str, timeout: float = 0.3) -> bool | None:
    """True if the inbox socket answers, False if it is gone, None if we were
    not allowed to ask. The Codex sandbox refuses the connect with EPERM, and
    reading that as dead showed every Claude peer as `stale` to a Codex worker
    (S10 collab run 4) — the same mistake pid_alive once made with kill."""
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock_path)
        return True
    except PermissionError:
        return None
    except OSError:
        return False


def ref_of(runtime: str, home: str, session_id: str) -> str:
    """Short, stable handle that disambiguates two sessions sharing a name.
    Claude prints its own [ref] the same way: sha256 of an identifying string."""
    raw = "%s:%s:%s" % (runtime, os.path.realpath(home), session_id)
    return hashlib.sha256(raw.encode()).hexdigest()[:6]


def normalize(name: str) -> str:
    """Names are matched case-insensitively and space/dash/underscore blind,
    because the two runtimes accept different spellings (S7)."""
    return re.sub(r"[\s_-]+", "", (name or "").strip().lower())


def state_of(record: dict) -> str:
    """live | ended | stale | unknown.

    `ended` means the session said goodbye (its SessionEnd hook ran) and its
    process is gone; `stale` means the process is gone without a goodbye — a
    crash, a kill -9, a closed terminal. Both can come back: `claude --resume`
    reuses the session id, so the same pointer turns live again. `unknown` is
    deliberate for a Codex session whose pid we could not confirm: unknown must
    not read as dead (ADR-0001).
    """
    return state_reason(record)[0]


def state_reason(record: dict) -> tuple:
    """(state, reason): state_of with the cause, for a record's end_reason."""
    verdict, why = _state_of(record)
    try:
        from . import telemetry
        telemetry.bump("xsm.state.%s" % why)
    except ImportError:
        pass
    return verdict, why


def _state_of(record: dict) -> tuple:
    """(state, reason). The reason is counted on the command's span: a list
    that shows no live peers has one cause per check below, and from inside a
    sandbox they are not the ones they look like from outside."""
    pid = record.get("pid")
    if not pid:
        return "unknown", "no_pid"
    gone = "ended" if record.get("ended_at") else "stale"
    if not pid_alive(pid):
        return gone, "pid_dead"
    recorded = record.get("lstart")
    unverified = False
    if recorded:
        now = lstart(pid)
        if now is not None and now != recorded:
            return gone, "lstart_changed"   # pid reused by a different process
        # `ps` refused (the Codex sandbox) is not evidence of reuse; the pid is
        # alive and we could not look further.
        unverified = now is None
    if record.get("runtime") == "codex" and record.get("mcp_pid"):
        # A Codex TUI starts a fresh set of MCP servers for every thread it
        # opens and kills the old set about a minute after leaving that thread
        # (measured 2026-09-23, Codex 0.155). So xsm's own MCP server, whose
        # pid the hook wrote here, lives exactly as long as the thread does —
        # which the process does not: one /new in the TUI left the process
        # alive, the thread shut down, and two messages queued to it forever.
        beacon = record["mcp_pid"]
        if not pid_alive(beacon):
            return "ended", "thread_replaced"
        seen = record.get("mcp_lstart")
        if seen:
            now = lstart(beacon)
            if now is not None and now != seen:
                return "ended", "thread_replaced"
    if record.get("runtime") == "claude":
        answer = socket_live(record.get("socket") or "")
        if answer is None:
            return "live", "socket_blocked"
        if answer:
            return "live", "live_unverified" if unverified else "live"
        return gone, "socket_dead"
    return "live", "live_unverified" if unverified else "live"


def ancestor_pid(names, max_hops: int = 10) -> int | None:
    """Walk up the process tree to the session process. A hook runs under a
    shell, so os.getppid() is the shell, not the agent (agent-comms' own
    drain.sh walks the same way)."""
    pid = os.getppid()
    for _ in range(max_hops):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        parts = out.stdout.strip().split(None, 1)
        if len(parts) != 2:
            return None
        parent, comm = parts
        if os.path.basename(comm.strip()) in names:
            return pid
        try:
            pid = int(parent)
        except ValueError:
            return None
    return None


def pid_from_socket(sock_path: str) -> int | None:
    """Claude names its inbox socket after its own pid: /tmp/cc-socks/<pid>.sock."""
    base = os.path.basename(sock_path or "")
    try:
        return int(base.split(".", 1)[0])
    except (ValueError, IndexError):
        return None
