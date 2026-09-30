"""Start a queued message in a Codex thread its TUI holds in the shared daemon.

Codex's queue extension skips a thread whose status is Interrupted, so an item
queued after the person pressed Esc waits until they type again (user report,
2026-09-28; codex-rs/ext/queue/src/service.rs in 0.158). Since 0.157 a Codex
TUI is by default a client of one app-server daemon per CODEX_HOME, and that
daemon starts a queued item on request: `thread/queue/start`. Measured
2026-09-28 on 0.158.0: after Esc a queued xsm message sat 25 s and more; this
call returned a turn in progress, the TUI showed the message and ran it, and
the xsm gate recorded the receipt.

Besides that we only read which threads it has loaded (loaded_threads), the
liveness of a thread it hosts. Never `thread/resume` (a second writer on the
thread) and never `turn/start` (it steers into a turn already running). The
API is experimental; anything unexpected is "unavailable" and the item stays
queued, which is where it would have been anyway.

Protocol: WebSocket over the control socket, JSON-RPC in text frames. The
socket is 0600 and takes no token. Stdlib only, never raises.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time

CONTROL_SOCKET = os.path.join("app-server-control", "app-server-control.sock")

STARTED, BUSY, NOT_LOADED, UNAVAILABLE = "started", "busy", "not-loaded", "unavailable"

# The daemon's own words for the two refusals we act on (0.158.0, measured).
BUSY_SIGN = "already has an active or pending turn"
NOT_LOADED_SIGN = "resume the thread before starting a queued message"


class _Fail(Exception):
    pass


DELETED, ABSENT = "deleted", "absent"


def socket_path(codex_home: str) -> str:
    return os.path.join(os.path.expanduser(codex_home), CONTROL_SOCKET)


def start_queued(codex_home: str, thread_id: str, queued_id: str, timeout: float = 3.0,
                 client_version: str = "") -> tuple:
    """(outcome, detail): started, busy, not-loaded or unavailable."""
    path = socket_path(codex_home)
    if not os.path.exists(path):
        return UNAVAILABLE, "no daemon control socket at %s" % path
    deadline = time.monotonic() + timeout
    conn = None
    try:
        conn = _Conn(os.path.realpath(path), deadline)
        init = conn.call("initialize", "initialize", {
            "clientInfo": {"name": "xsm", "version": client_version or _version()},
            "capabilities": {"experimentalApi": True}})
        if "error" in init:
            return UNAVAILABLE, "initialize: %s" % _message(init)
        conn.send({"method": "initialized"})
        reply = conn.call(2, "thread/queue/start", {"threadId": thread_id,
                                                     "queuedSubmissionId": queued_id})
    except (OSError, _Fail, ValueError) as err:
        return UNAVAILABLE, str(err) or err.__class__.__name__
    finally:
        if conn is not None:
            conn.close()
    if "error" in reply:
        text = _message(reply)
        if BUSY_SIGN in text:
            return BUSY, text
        if NOT_LOADED_SIGN in text:
            return NOT_LOADED, text
        return UNAVAILABLE, text
    turn = (reply.get("result") or {}).get("turn") if isinstance(reply.get("result"), dict) else None
    if isinstance(turn, dict):
        return STARTED, str(turn.get("id") or turn.get("status") or "")
    return UNAVAILABLE, "unexpected reply: %s" % json.dumps(reply)[:200]


def delete_queued(codex_home: str, thread_id: str, queued_id: str, timeout: float = 1.0) -> str:
    """Take a queued item back out of a thread's queue: deleted, absent (it
    already started, or was never there) or unavailable. `xsm inbox` hands a
    message over mid-turn; the queue's own copy then arrived after the turn,
    the gate refused it as a duplicate, and Codex showed a "Blocked by hook"
    card for every message (issue #7). thread/queue/delete takes the id
    `codex queue` printed and answers {"deleted": bool} (measured with Codex
    0.159.2, 2026-09-30)."""
    path = socket_path(codex_home)
    if not (thread_id and queued_id) or not os.path.exists(path):
        return UNAVAILABLE
    deadline = time.monotonic() + timeout
    conn = None
    try:
        conn = _Conn(os.path.realpath(path), deadline)
        init = conn.call("initialize", "initialize", {
            "clientInfo": {"name": "xsm", "version": _version()},
            "capabilities": {"experimentalApi": True}})
        if "error" in init:
            return UNAVAILABLE
        conn.send({"method": "initialized"})
        reply = conn.call(2, "thread/queue/delete", {"threadId": thread_id,
                                                      "queuedSubmissionId": queued_id})
    except (OSError, _Fail, ValueError):
        return UNAVAILABLE
    finally:
        if conn is not None:
            conn.close()
    result = reply.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("deleted"), bool):
        return UNAVAILABLE
    return DELETED if result["deleted"] else ABSENT


_LOADED_CACHE = {}          # realpath(home) -> (monotonic time answered, set | None)
_LOADED_TTL = 2.0
# A daemon that did not answer is asked again only after this long, so a home
# whose socket hangs costs one timeout per window, not one per record.
_LOADED_FAIL_TTL = 30.0
# How long one query may take when the caller names no timeout. The receive
# hook lowers it (use_hook_budget): it runs on every prompt of every Codex
# session under a 10 s limit, and several unresponsive homes add up.
DEFAULT_TIMEOUT = 1.0
HOOK_TIMEOUT = 0.2


def use_hook_budget() -> None:
    global DEFAULT_TIMEOUT
    DEFAULT_TIMEOUT = HOOK_TIMEOUT


def loaded_threads(codex_home: str, timeout: float | None = None):
    """The thread ids the home's daemon has loaded, or None if it cannot say.

    A thread hosted by the daemon runs its hooks under the daemon, so every
    thread of a home records the same pid and outlives its window on it (issue
    #5). The daemon itself knows: `thread/loaded/list` drops a thread about 60 s
    after the last TUI holding it quits, the moment its MCP servers stop, and
    lists it again when a TUI reconnects after a daemon restart (measured
    2026-09-29, 0.158/0.159). None — no socket, a sandbox refusal, a timeout,
    an unexpected reply — says nothing about the thread. Cached briefly: a list
    of sessions asks once per home, not once per record. The cache is stamped
    after the query, so a slow answer is not born expired, and a failure is
    kept longer than an answer (review, 2026-09-29)."""
    key = os.path.realpath(os.path.expanduser(codex_home or ""))
    hit = _LOADED_CACHE.get(key)
    if hit:
        ttl = _LOADED_TTL if hit[1] is not None else _LOADED_FAIL_TTL
        if time.monotonic() - hit[0] < ttl:
            return hit[1]
    path = socket_path(key)
    if not key or not os.path.exists(path):
        return None                             # nothing to ask, nothing to wait for
    result = _ask_loaded(key, timeout if timeout is not None else DEFAULT_TIMEOUT)
    _LOADED_CACHE[key] = (time.monotonic(), result)
    return result


def _ask_loaded(codex_home: str, timeout: float):
    path = socket_path(codex_home)
    deadline = time.monotonic() + timeout
    conn = None
    try:
        conn = _Conn(os.path.realpath(path), deadline)
        init = conn.call("initialize", "initialize", {
            "clientInfo": {"name": "xsm", "version": _version()},
            "capabilities": {"experimentalApi": True}})
        if "error" in init:
            return None
        conn.send({"method": "initialized"})
        found, cursor = set(), None
        for page in range(50):
            reply = conn.call(page + 2, "thread/loaded/list",
                              {"cursor": cursor} if cursor else {})
            result = reply.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                return None
            found.update(str(t) for t in result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                return found
        return None
    except (OSError, _Fail, ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except ImportError:
        return "0"


def _message(reply: dict) -> str:
    err = reply.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


class _Conn:
    """Just enough of a WebSocket client for a few JSON-RPC calls."""

    def __init__(self, path: str, deadline: float):
        self.deadline = deadline
        self.buf = b""
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._open(path)
        except BaseException:
            self.close()
            raise

    def _open(self, path: str):
        self._arm()
        self.s.connect(path)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        while b"\r\n\r\n" not in self.buf:
            self._read()
            if len(self.buf) > 65536:
                raise _Fail("no end of handshake")
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status:
            raise _Fail("handshake refused: %s" % status)

    def _arm(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _Fail("timed out")
        self.s.settimeout(left)

    def _read(self):
        self._arm()
        try:
            chunk = self.s.recv(65536)
        except socket.timeout:
            raise _Fail("timed out")
        if not chunk:
            raise _Fail("daemon closed the connection")
        self.buf += chunk

    def _need(self, n: int):
        while len(self.buf) < n:
            self._read()

    def _frame(self, opcode: int, data: bytes):
        head = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)                      # a client must mask (RFC 6455 5.3)
        self._arm()
        self.s.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, obj: dict):
        self._frame(0x1, json.dumps(obj).encode())

    def recv(self) -> dict:
        message = b""
        while True:
            self._need(2)
            b0, b1 = self.buf[0], self.buf[1]
            n, off = b1 & 0x7F, 2
            if n == 126:
                self._need(4)
                n, off = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                self._need(10)
                n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
            if n > 16 * 1024 * 1024:
                raise _Fail("frame too large")
            if b1 & 0x80:                         # a server never masks; skip the key if it did
                off += 4
            self._need(off + n)
            payload = self.buf[off:off + n]
            if b1 & 0x80:
                key = self.buf[off - 4:off]
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            self.buf = self.buf[off + n:]
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise _Fail("daemon closed the connection")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b0 & 0x80:
                value = json.loads(message.decode("utf-8"))
                if not isinstance(value, dict):
                    raise _Fail("not a JSON-RPC object")
                return value

    def call(self, rid, method: str, params: dict) -> dict:
        """The reply to this request; notifications in between are passed over."""
        self.send({"id": rid, "method": method, "params": params})
        while True:
            m = self.recv()
            if m.get("id") == rid and ("result" in m or "error" in m):
                return m

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass
