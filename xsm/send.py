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
from contextlib import nullcontext

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
         reply_to: str | None = None, priority: str = "next", wait: float = 0.0,
         msg_id: str | None = None) -> SendResult:
    """One span around the whole attempt, refusals included.

    A refusal is as worth timing as a delivery: "out of scope" and "target has
    no inbox socket" are the two things a caller actually hits, and neither
    shows up in a log that only records what succeeded.
    """
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    span_cm = telemetry.span("xsm.send", {"xsm.msg.kind": kind},
                             kind="PRODUCER") if telemetry else nullcontext()
    with span_cm as span:
        result = _send(target_spec, body, sender=sender, kind=kind, reply_to=reply_to,
                       priority=priority, wait=wait, msg_id=msg_id, span=span)
        if span is not None:
            span.set_attribute("xsm.result.status", result.status)
            if result.msg_id:
                span.set_attribute("xsm.msg.id", result.msg_id)
            if result.target:
                span.set_attribute("xsm.target.ref", result.target.get("ref"))
                span.set_attribute("xsm.target.runtime", result.target.get("runtime"))
            if result.status in ("refused", "error"):
                span.set_status("ERROR", result.reason)
        if telemetry:
            telemetry.counter("xsm.send.count", 1, {"xsm.result.status": result.status})
        return result


def _send(target_spec: str, body: str, *, sender: dict | None = None, kind: str = "note",
          reply_to: str | None = None, priority: str = "next", wait: float = 0.0,
          msg_id: str | None = None, span=None) -> SendResult:
    sender = sender or registry.me()
    if not sender:
        return SendResult("refused", "this session is not registered; run `xsm doctor`")

    from . import remote
    local_spec, peer = remote.split_target(target_spec)
    if peer:
        if sender.get("ref") in config.blocked():
            return SendResult("refused", "this session is blocked (xsm block)")
        reply = remote.send(sender, local_spec, peer, body, kind, reply_to, wait, msg_id,
                            traceparent=span.traceparent() if span is not None else None)
        status = reply.get("status") or ("queued" if reply.get("ok") else "error")
        status = {"queued": "sent-unconfirmed"}.get(status, status)
        return SendResult(status, reply.get("error") or ("on %s" % peer), reply.get("id"),
                          reply.get("target"))

    registry.adopt_open_codex()             # an unprompted Codex thread can still be addressed
    found = resolve.resolve(target_spec)
    if not found.ok:
        return SendResult("refused", found.reason, candidates=found.candidates)
    target = found.record or {}

    if not target.get("registered"):
        return SendResult("refused", "target has no hook record, so it cannot be addressed")
    if target.get("ref") == sender.get("ref"):
        return SendResult("refused", "refusing to send to yourself")
    blocked = config.blocked()
    if sender.get("ref") in blocked or target.get("ref") in blocked:
        return SendResult("refused", "%s is blocked (xsm block); only a person can lift it" % (
            "this session" if sender.get("ref") in blocked else "the target"), target=target)

    scope, reason = config.scope_for(sender, target)
    if not scope:
        return SendResult("refused", "out of scope: %s" % reason, target=target)

    forecast, why = native_forecast(sender, target)
    if forecast in ("refuse",):
        return SendResult("refused", "the receiver would drop this: %s" % why, target=target)

    msg_id = msg_id or envelope.new_id()
    content = envelope.build(body, msg_id=msg_id, sender=sender, scope=scope, kind=kind,
                             reply_to=reply_to,
                             traceparent=span.traceparent() if span is not None else None)
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
        ledger.failed(msg_id, "%s: %s" % (err.reason, err.detail))
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
