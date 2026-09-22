"""The two native delivery paths, and nothing else.

Claude: one JSON frame per line into the session's own inbox socket. Codex:
`codex queue --thread <uuid>`, which a running TUI picks up within about ten
seconds when the thread is loaded and idle (S2). Both are the runtimes' own
mechanisms — we add no transport of our own, so there is nothing of ours to
keep running (ADR-0003).

A sandboxed Codex cannot use either path from its shell: the socket connect
returns EPERM and the queue database is read-only (S4). We surface that as a
named failure instead of a traceback, because the fix is a different launch
mode, not a retry.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager


class DeliveryError(Exception):
    """Carries a machine-readable reason plus the runtime's own words."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__("%s: %s" % (reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


def codex_bin() -> str | None:
    return shutil.which("codex") or next(
        (p for p in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex") if os.path.exists(p)), None)


@contextmanager
def _timed(transport: str):
    """One span and one duration per delivery attempt, tagged with how it went.

    How often a send is refused by a sandbox rather than delivered is the
    question this exists to answer, so the reason is recorded and the error
    re-raised untouched.
    """
    try:
        from . import telemetry
    except ImportError:
        yield
        return
    start, outcome = time.time(), "ok"
    try:
        with telemetry.span("xsm.deliver", {"xsm.transport": transport}):
            yield
    except DeliveryError as err:
        outcome = err.reason
        raise
    finally:
        telemetry.histogram("xsm.deliver.duration", time.time() - start,
                            {"xsm.transport": transport, "xsm.delivery.outcome": outcome})


def to_claude(socket_path: str, content: str, msg_id: str, priority: str = "next",
              reply_address: str | None = None) -> None:
    with _timed("uds"):
        _to_claude(socket_path, content, msg_id, priority, reply_address)


def _to_claude(socket_path: str, content: str, msg_id: str, priority: str,
               reply_address: str | None) -> None:
    frame = {"type": "user", "msg_id": msg_id, "priority": priority,
             "message": {"role": "user", "content": content}}
    if reply_address:
        frame["from"] = reply_address
    line = json.dumps(frame, ensure_ascii=False) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(socket_path)
            s.sendall(line.encode())
    except PermissionError as err:
        raise DeliveryError("sandbox-blocked", "%s (a sandboxed session cannot open the "
                                               "inbox socket; run the sender outside the "
                                               "sandbox or through a trusted hook)" % err)
    except FileNotFoundError:
        raise DeliveryError("no-inbox", "%s does not exist; the session is gone" % socket_path)
    except OSError as err:
        raise DeliveryError("socket-error", str(err))


def to_codex(codex_home: str, thread_id: str, content: str) -> str:
    """Always addressed by thread UUID: name lookup fails outright once a home
    holds more than a hundred threads (S7)."""
    with _timed("codex-queue"):
        return _to_codex(codex_home, thread_id, content)


def _to_codex(codex_home: str, thread_id: str, content: str) -> str:
    binary = codex_bin()
    if not binary:
        raise DeliveryError("no-codex-binary", "codex is not on PATH")
    env = dict(os.environ, CODEX_HOME=os.path.expanduser(codex_home))
    try:
        out = subprocess.run([binary, "queue", "--thread", thread_id, "--message", content],
                             capture_output=True, text=True, timeout=30, env=env)
    except subprocess.SubprocessError as err:
        raise DeliveryError("codex-failed", str(err))
    if out.returncode != 0:
        text = (out.stderr or out.stdout or "").strip()
        blocked = "readonly database" in text or "Operation not permitted" in text
        reason = "sandbox-blocked" if blocked else "codex-failed"
        raise DeliveryError(reason, text[:400])
    return (out.stdout or "").strip()
