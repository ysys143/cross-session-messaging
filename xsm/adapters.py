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

import glob
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager


class DeliveryError(Exception):
    """Carries a machine-readable reason plus the runtime's own words."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__("%s: %s" % (reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


# An install that cannot run says so in its own words; measured 2026-09-23,
# when an npm @openai/codex without its platform binary sat first on PATH and
# every delivery to a Codex session would have failed with this.
BROKEN_CODEX = ("Missing optional dependency", "command not found", "cannot execute binary")


def codex_bins() -> list:
    """Every codex worth trying, best first: an explicit one, then PATH, then
    where the installers put it. A machine can have more than one, and the
    first is not always the one that runs."""
    found = [os.environ.get("XSM_CODEX") or "", shutil.which("codex") or "",
             "/opt/homebrew/bin/codex", "/usr/local/bin/codex"]
    out = []
    for path in found:
        if path and os.path.exists(path) and path not in out:
            out.append(path)
    return out


def codex_bin() -> str | None:
    return codex_bins()[0] if codex_bins() else None


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
    binaries = codex_bins()
    if not binaries:
        raise DeliveryError("no-codex-binary", "codex is not on PATH")
    env = dict(os.environ, CODEX_HOME=os.path.expanduser(codex_home))
    out = None
    for binary in binaries:
        try:
            out = subprocess.run([binary, "queue", "--thread", thread_id, "--message", content],
                                 capture_output=True, text=True, timeout=30, env=env)
        except subprocess.SubprocessError as err:
            raise DeliveryError("codex-failed", str(err))
        text = (out.stderr or out.stdout or "")
        if out.returncode == 0 or not any(sign in text for sign in BROKEN_CODEX):
            break
        # That install cannot run at all; another one on this machine may.
    if out is None:
        raise DeliveryError("no-codex-binary", "codex is not on PATH")
    if out.returncode != 0:
        text = (out.stderr or out.stdout or "").strip()
        if "no rollout found" in text:
            # A thread its TUI has open but that has had no prompt yet: Codex
            # writes no rollout before the first prompt, and `codex queue`
            # checks for one. The TUI itself reads its queue all the same
            # (measured 2026-09-23: a row written here was taken in 14 s).
            return queue_direct(codex_home, thread_id, content)
        blocked = "readonly database" in text or "Operation not permitted" in text
        reason = "sandbox-blocked" if blocked else "codex-failed"
        raise DeliveryError(reason, text[:400])
    return (out.stdout or "").strip()


# --- Codex internals, for a thread with no prompt yet -------------------------
#
# Neither of these is a public interface. Each checks the shape it expects and
# fails with `codex-internal-changed` otherwise, so a Codex update turns a
# fresh thread back into "not addressable yet" rather than a lost message
# (user decision, 2026-09-23; ADR-0002 appendix).

QUEUE_COLUMNS = ["id", "thread_id", "payload_json", "queue_order", "created_at_ms", "updated_at_ms"]


def _newest(home: str, stem: str) -> str | None:
    """Codex names its databases <stem>_<schema version>.sqlite."""
    found = glob.glob(os.path.join(os.path.expanduser(home), "%s_*.sqlite" % stem))
    return max(found, key=lambda p: int(os.path.basename(p)[len(stem) + 1:-7] or 0)
               if os.path.basename(p)[len(stem) + 1:-7].isdigit() else -1) if found else None


def _uuid() -> str:
    return str(getattr(uuid, "uuid7", uuid.uuid4)())      # Codex's own ids are v7


def queue_direct(codex_home: str, thread_id: str, content: str) -> str:
    """The row `codex queue` would have written, written here."""
    db = _newest(codex_home, "queue")
    if not db:
        raise DeliveryError("codex-internal-changed", "no queue database in %s" % codex_home)
    try:
        con = sqlite3.connect(db, timeout=5)
    except sqlite3.Error as err:
        raise DeliveryError("codex-failed", str(err))
    try:
        cols = [row[1] for row in con.execute("pragma table_info(queued_items)")]
        if cols != QUEUE_COLUMNS:
            raise DeliveryError("codex-internal-changed",
                                "queued_items has columns %s, expected %s" % (cols, QUEUE_COLUMNS))
        now = int(time.time() * 1000)
        payload = {"UserInput": {"content": [{"type": "text", "text": content,
                                              "text_elements": []}],
                                 "client_id": _uuid()}}
        with con:
            order = con.execute("select coalesce(max(queue_order), 0) + 1 from queued_items "
                                "where thread_id = ?", (thread_id,)).fetchone()[0]
            con.execute("insert into queued_items (id, thread_id, payload_json, queue_order, "
                        "created_at_ms, updated_at_ms) values (?, ?, ?, ?, ?, ?)",
                        (_uuid(), thread_id, json.dumps(payload, ensure_ascii=False), order,
                         now, now))
    except sqlite3.OperationalError as err:
        blocked = "readonly" in str(err) or "not permitted" in str(err)
        raise DeliveryError("sandbox-blocked" if blocked else "codex-failed", str(err))
    except sqlite3.Error as err:
        raise DeliveryError("codex-failed", str(err))
    finally:
        con.close()
    return "queued directly (the thread has had no prompt yet)"


def thread_of_process(codex_home: str, pid: int, since: float) -> str | None:
    """The thread a Codex process opened at about `since` (when its xsm MCP
    server started), from Codex's own log database: every log line carries
    `pid:<pid>:…` and, once a thread exists, its id — seconds after the TUI
    opens it, long before any prompt. The thread first seen closest to
    `since` is the one; a side thread the TUI opens later is further off."""
    db = _newest(codex_home, "logs")
    if not db:
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "select thread_id, min(ts) from logs where process_uuid like ? and thread_id != '' "
            "and thread_id is not null and ts >= ? group by thread_id",
            ("pid:%d:%%" % int(pid), int(since) - 30)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    near = [(abs(first - since), tid) for tid, first in rows if abs(first - since) <= 30]
    return min(near)[1] if near else None
