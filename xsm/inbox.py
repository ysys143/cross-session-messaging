"""A copy of each message sent to a Codex session, for it to read mid-turn.

Codex takes its queue only between turns. A session that keeps working —
`sleep; ./phase; ./xsm channel` in a loop, which is what a Codex worker told
to check often does — never ends a turn and never receives anything: in S10
collab run 4 all six messages to the Codex worker (three phase announcements,
a peer's reply) sat in its queue for the whole fifteen minutes.

So the send side also keeps the envelope here, under the target's session id,
and `xsm inbox` (or the xsm_inbox MCP tool) hands it over through the same
checks the hook runs. Whichever reads it first writes the receipt; the other
path then sees the receipt and drops its copy — the hook refuses the queued
duplicate, which in Codex consumes it without a trace (S6).
"""
from __future__ import annotations

import os
import time

from . import paths

INBOX = "inbox"
MAX_WAIT = 600          # a `--wait` longer than a person's approval window is a daemon
MCP_MAX_WAIT = 60       # the MCP server reads one request at a time; a long block looks dead
KEEPALIVE = 15.0        # a silent process is one an agent kills


def _dir(session_id: str) -> str:
    return paths.path(INBOX, str(session_id))


def keep(session_id: str, msg_id: str, content: str) -> None:
    paths.write_json(os.path.join(_dir(session_id), "%s.json" % msg_id),
                     {"id": msg_id, "t": time.time(), "content": content})


def drop(session_id: str | None, msg_id: str | None) -> None:
    if not (session_id and msg_id):
        return
    try:
        os.unlink(os.path.join(_dir(session_id), "%s.json" % msg_id))
    except OSError:
        pass


def count(session_id: str | None) -> int:
    """Cheap: one listdir. Runs after every command a Codex session makes."""
    if not session_id:
        return 0
    try:
        return sum(1 for n in os.listdir(_dir(session_id)) if n.endswith(".json"))
    except OSError:
        return 0


def wait_for(session_id: str | None, seconds: float, interval: float = 1.0,
             keepalive: float = KEEPALIVE, out=None) -> int:
    """Block until a message is waiting, and say how many. 0 when time runs out.

    This is not a daemon: it is one command blocking until it returns, and the
    process ends with it. The cap is what says so in code. It exists because
    the alternative is what a Codex worker actually did — `sleep` in a loop,
    which never ends the turn, which is exactly why its six queued messages
    never arrived (S10 collab run 4).

    It counts; it never takes. `take()` claims by rename, so a waiting loop
    that took would swallow the message it was waiting for.
    """
    deadline = time.time() + max(0.0, min(seconds, MAX_WAIT))
    spoke = time.time()
    while True:
        waiting = count(session_id)
        if waiting:
            return waiting
        left = deadline - time.time()
        if left <= 0:
            if out:
                out.write("[xsm] no message in %ds. Call `xsm inbox --wait` again if you are "
                          "still waiting; do not sleep-poll.\n" % seconds)
                out.flush()
            return 0
        if out and keepalive and time.time() - spoke >= keepalive:
            out.write("[xsm] waiting for messages — %ds left; this returns the moment one "
                      "arrives.\n" % int(left))
            out.flush()
            spoke = time.time()
        time.sleep(min(interval, left))


def take(session_id: str) -> list:
    """Claim every waiting copy, oldest first. The rename is the claim: two
    readers at once (a shell `xsm inbox` and the MCP tool) get each message
    once between them."""
    d = _dir(session_id)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in names:
        claimed = os.path.join(d, name[:-len(".json")] + ".taking")
        try:
            os.rename(os.path.join(d, name), claimed)
        except OSError:
            continue
        item = paths.read_json(claimed)
        try:
            os.unlink(claimed)
        except OSError:
            pass
        if item and item.get("content"):
            out.append(item)
    return sorted(out, key=lambda i: i.get("t", 0))


def notice(session_id: str | None) -> str:
    n = count(session_id)
    if not n:
        return ""
    return ("[xsm] %d message%s from other sessions waiting for you; read with `xsm inbox` "
            "(or the xsm_inbox MCP tool)." % (n, "" if n == 1 else "s"))
