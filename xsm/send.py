"""The send side.

Everything the receiver will check is checked here first — scope, liveness,
registration — so a refusal is immediate and local instead of a message that
vanishes on the far side. The S8 prototype skipped these checks and happily
"sent" to a session that had been killed, or after the policy had changed
underneath it.

What this does not do is claim delivery. The result is `sent-unconfirmed`
until the receiving hook writes a receipt.
"""
from __future__ import annotations

import os

from . import adapters, config, envelope, ledger, paths, registry, resolve, workers


class SendResult:
    def __init__(self, status: str, reason: str = "", msg_id: str | None = None,
                 target: dict | None = None, candidates=None):
        self.status = status          # delivered | sent-unconfirmed | held | refused | error
        self.reason = reason
        self.msg_id = msg_id
        self.target = target
        self.candidates = candidates or []

    def as_dict(self) -> dict:
        out = {"status": self.status, "reason": self.reason, "id": self.msg_id}
        if self.target:
            out["to"] = "%s@%s" % (self.target.get("name"), self.target.get("alias"))
        if self.candidates:
            out["candidates"] = ["%s@%s [%s]" % (c.get("name"), c.get("alias"), c.get("ref"))
                                 for c in self.candidates]
        return out


def send(target_spec: str, body: str, *, sender: dict | None = None, kind: str = "note",
         reply_to: str | None = None, priority: str = "next", wait: float = 0.0,
         msg_id: str | None = None) -> SendResult:
    sender = sender or registry.me()
    if not sender:
        return SendResult("refused", "this session is not registered; run `xsm doctor`")

    registry.adopt_open_codex()             # an unprompted Codex thread can still be addressed
    found = resolve.resolve(target_spec)
    if not found.ok:
        return SendResult("refused", found.reason, candidates=found.candidates)
    target = found.record or {}

    if not target.get("registered"):
        return SendResult("refused", "target has no hook record, so it cannot be addressed")
    if target.get("ref") == sender.get("ref"):
        return SendResult("refused", "refusing to send to yourself")

    scope, reason = config.scope_for(sender, target)
    if not scope:
        return SendResult("refused", "out of scope: %s" % reason, target=target)

    forecast, why = native_forecast(sender, target)
    if forecast in ("refuse",):
        return SendResult("refused", "the receiver would drop this: %s" % why, target=target)

    msg_id = msg_id or envelope.new_id()
    content = envelope.build(body, msg_id=msg_id, sender=sender, scope=scope, kind=kind,
                             reply_to=reply_to)
    ledger.queued(msg_id, sender, target, scope, kind, body)

    try:
        if target.get("runtime") == "claude":
            if not target.get("socket"):
                return SendResult("refused", "target has no inbox socket", msg_id, target)
            adapters.to_claude(target["socket"], content, msg_id, priority=priority,
                               reply_address=("uds:%s" % sender["socket"]) if sender.get("socket")
                               else None)
        elif workers.is_headless_codex(workers.for_session(target.get("session_id"))):
            workers.deliver(workers.for_session(target.get("session_id")), content)
        else:
            adapters.to_codex(target.get("home", os.path.expanduser("~/.codex")),
                              str(target.get("session_id")), content)
    except adapters.DeliveryError as err:
        paths.append_jsonl("decisions.jsonl", {"decision": "send-failed", "id": msg_id,
                                               "reason": err.reason, "detail": err.detail})
        return SendResult("error", "%s: %s" % (err.reason, err.detail), msg_id, target)

    if wait > 0:
        state = ledger.wait_for(msg_id, wait)
        if state.get("status") == "delivered":
            return SendResult("delivered", "receiver recorded it", msg_id, target)
        if state.get("status") in ("held", "blocked"):
            return SendResult(state["status"], (state.get("receipt") or {}).get("reason", ""),
                              msg_id, target)
    note = "queued"
    if target.get("runtime") == "codex":
        note = ("queued; a Codex session picks the queue up within about 10 seconds when the "
                "thread is loaded and idle, otherwise at the user's next input")
    elif forecast == "hold":
        note = "queued, but Claude will hold it for its user: %s" % why
    elif forecast == "unknown":
        note = "queued; %s, so the receiver's own gate may hold it" % why
    return SendResult("sent-unconfirmed", note, msg_id, target)


def native_forecast(sender: dict, target: dict) -> tuple:
    """What Claude's own gate will most likely do with this message.

    Returns (verdict, explanation). Only meaningful for a Claude target: Codex
    has no such gate. The order mirrors the receive decision measured in S1 —
    an explicit setting wins, then permission-mode parity.
    """
    if target.get("runtime") != "claude":
        return "n/a", ""
    setting = registry.inbound_setting(target.get("home", ""))
    if setting == "accept":
        return "accept", "receiver's user settings say crossSessionInbound=accept"
    if setting in ("hold", "refuse"):
        return setting, "receiver's user settings say crossSessionInbound=%s" % setting
    mine, theirs = registry.mode_class(sender), registry.mode_class(target)
    if mine is None or theirs is None:
        return "unknown", "one of the two permission modes is not known yet"
    if mine == theirs:
        return "accept", "both sessions run in %s mode" % theirs
    return "hold", ("sender is %s and receiver is %s, so Claude holds the message for its "
                    "user; set crossSessionInbound to \"accept\" on the receiver, or match "
                    "the permission modes" % (mine, theirs))
