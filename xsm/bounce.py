"""Telling a sender its Claude SendMessage was held by the receiver's gate.

A native message (Claude's own SendMessage, no xsm header) to a session out
of scope is held by that session's hook (ADR-0013). The sender's tool result
had already said "queued", Claude's own delivery notice covers only Claude's
gate, and a native message has no xsm id, so no receipt reaches the sender:
the sender reported the message delivered while it sat in the other
machine-local held list (issue #8, 2026-10-01).

So the receiving hook leaves a note for the sender, here, keyed by the
sender's session id; the sender's next hook run (and any xsm command or MCP
tool it calls) shows it once. It is the sender's own state file, not a
message: it passes no gate and cannot bounce again. A sender the hook could
not name (no local socket, a socket no single running session owns) gets no
note — telling the wrong session is worse than telling none.
"""
from __future__ import annotations

import os
import time

from . import paths

BOUNCES = "bounces"


def _dir(session_id: str) -> str:
    return paths.path(BOUNCES, os.path.basename(str(session_id)))


def record(sender: dict, receiver: dict | None, reason: str, held: str | None,
           body: str, connect_dir: str | None) -> None:
    """Leave a note for `sender` that its message to `receiver` was held. When
    the receiver was out of scope the text is kept as a send of its own and the
    connection is asked for in the sender's session (connect.hold_native), so the
    note can lead to one yes that connects and sends it (2026-10-01); a failure
    there leaves the note as it was."""
    sid = str(sender.get("session_id") or "")
    if not sid:
        return
    receiver = receiver or {}
    offer = None
    if connect_dir:
        try:
            from . import connect
            offer = connect.hold_native(sender, receiver, body)
        except Exception:               # noqa: BLE001 - the plain note still says it
            offer = None
    paths.write_json(os.path.join(_dir(sid), "%d.json" % int(time.time() * 1000)), {
        "t": time.time(), "reason": reason, "held": held,
        "to": {k: receiver.get(k) for k in ("name", "alias", "ref", "runtime", "cwd")},
        "connect_dir": connect_dir, "preview": (body or "")[:200], "offer": offer})


def record_held_here(receiver: dict | None, who: str, reason: str, held: str) -> None:
    """Leave the receiving agent a note that its own gate kept a message for
    its user to decide on. Refusing the prompt shows the person a line, but
    the agent sees nothing of a prompt that was refused, so it would not know
    to offer `xsm held deliver` (2026-10-01). Shown once, with the next prompt
    that goes through, like a sender's note. It carries none of the message:
    what the gate held is for the person to see first."""
    sid = str((receiver or {}).get("session_id") or "")
    if not sid:
        return
    # The sender's own words (a header's from) end up in the agent's context:
    # one short line, never a paragraph of someone else's.
    paths.write_json(os.path.join(_dir(sid), "%d.json" % int(time.time() * 1000)), {
        "t": time.time(), "here": True, "from": " ".join(str(who).split())[:60],
        "reason": " ".join(str(reason).split())[:200], "held": held})


def take(session_id: str | None) -> list:
    """Claim this session's notes, oldest first; each is shown once."""
    if not session_id:
        return []
    d = _dir(session_id)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for name in names:
        p = os.path.join(d, name)
        claimed = p[:-len(".json")] + ".taking"
        try:
            os.rename(p, claimed)           # two readers at once show it once
        except OSError:
            continue
        note = paths.read_json(claimed)
        try:
            os.unlink(claimed)
        except OSError:
            pass
        if isinstance(note, dict):
            out.append(note)
    return out


def text(notes: list) -> str:
    """What the sending agent reads. Connecting the folders is its user's
    decision, but asking is the agent's job (user decision, 2026-10-01: ask for
    approval rather than have the user type commands). When the message was
    kept (`offer`), one yes connects the folders and sends it: the agent asks,
    runs `xsm send --held <id>` to be shown the reply, and once more on a yes."""
    lines = []
    for note in notes:
        if note.get("here"):
            lines.append(
                "[xsm] A message from %s was held by this session's gate (%s) and is kept as %s. "
                "It is not delivered. If your user wants it, ask them, in plain words, whether "
                "to deliver it, and after they answer run `xsm held deliver %s`: that run shows "
                "you their reply without acting, and on a yes, run it once more to receive the "
                "message." % (note.get("from") or "an unknown sender",
                              (note.get("reason") or "").split(";")[0], note.get("held"),
                              note.get("held")))
            continue
        to = note.get("to") or {}
        who = "%s@%s" % (to.get("name") or "?", to.get("alias") or "?")
        if to.get("ref"):
            who += " [ref:%s]" % to["ref"]
        lines.append("[xsm] Your message to %s was NOT delivered: its gate held it (%s)."
                     % (who, (note.get("reason") or "").split(";")[0]))
        if note.get("preview"):
            lines.append("  It began: %s" % note["preview"].replace("\n", " ")[:120])
        if note.get("offer"):
            kept = note["offer"]
            lines.append(
                "  It is kept as %s. It needs your user's yes to %s: ask them once, in plain "
                "words, whether to go ahead, and wait for their answer. After they answer, run `xsm "
                "send --held %s` (or the xsm_send tool with held=%s): xsm keeps their answer as "
                "the verdict and that run shows it without acting. On a yes, run it once more: "
                "xsm connects and sends the message, so there is nothing more to ask. On a no "
                "it stays unsent. Do not report the message as delivered." % (
                    kept.get("id"), kept.get("what"), kept.get("id"), kept.get("id")))
        elif note.get("connect_dir"):
            lines.append(
                "  To reach it, ask your user whether to connect this folder with %s, and do it "
                "for them: run `xsm link %s` (their reply is kept as the verdict) or call the "
                "xsm_link MCP tool (an approval form). Then send the message again. Do not "
                "report the message as delivered." % (note["connect_dir"], note["connect_dir"]))
        else:
            lines.append("  Tell your user it was not delivered.")
    return "\n".join(lines)


def notice(session_id: str | None) -> str:
    """The notes for this session as text, used up; "" when there are none."""
    return text(take(session_id))
