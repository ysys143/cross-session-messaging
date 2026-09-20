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

from . import adapters, config, envelope, ledger, paths, registry, resolve


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
         reply_to: str | None = None, priority: str = "next", wait: float = 0.0) -> SendResult:
    sender = sender or registry.me()
    if not sender:
        return SendResult("refused", "this session is not registered; run `xsm doctor`")

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

    msg_id = envelope.new_id()
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
    return SendResult("sent-unconfirmed", note, msg_id, target)
