"""The receive side: one hook entry point for both runtimes.

Two rules shape this file.

1. A hook that dies stops gating. Claude reports a crashed hook as a
   non-blocking error and runs the prompt anyway, so a broken gate is an open
   gate (S8-g2). Everything here runs under a catch-all, and the fallback
   decision depends on what arrived: a message carrying a peer envelope or an
   xsm header is refused, anything that looks like the user typing is passed.
   A person must never be locked out of their own session by our bug.
2. The runtime is decided from the hook input's own fields, not from path
   strings (ADR-0001 draft A'), because CODEX_HOME and CLAUDE_CONFIG_DIR can
   live anywhere.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import nullcontext

from . import config, envelope, housekeeping, identity, inbox, ledger, paths, registry, workers

CLAUDE_FIELDS = ("scratchpad_dir", "session_title", "prompt_id")
CODEX_FIELDS = ("turn_id",)


def detect_runtime(data: dict) -> str:
    """The hook input says who is calling; the environment only guesses.

    Environment variables are inherited: a Codex started from a Claude
    session's terminal carries CLAUDE_CODE_SESSION_ID, so trusting env first
    would register that Codex session as Claude. Order: input fields, then the
    transcript's location, then the environment as a last resort.
    """
    if any(f in data for f in CLAUDE_FIELDS):
        return "claude"
    if any(f in data for f in CODEX_FIELDS):
        return "codex"
    # Claude writes <CONFIG_DIR>/projects/…, Codex writes <CODEX_HOME>/sessions/….
    tp = data.get("transcript_path") or ""
    if "/projects/" in tp:
        return "claude"
    if "/sessions/" in tp:
        return "codex"
    if os.environ.get("CODEX_HOME") and not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "codex"
    return "claude"


def home_of(runtime: str, data: dict) -> str | None:
    if runtime == "claude":
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        if env:
            return env
        tp = data.get("transcript_path") or ""
        marker = "/projects/"
        return tp[:tp.index(marker)] if marker in tp else os.path.expanduser("~/.claude")
    env = os.environ.get("CODEX_HOME")
    if env:
        return env
    tp = data.get("transcript_path") or ""
    marker = "/sessions/"
    return tp[:tp.index(marker)] if marker in tp else os.path.expanduser("~/.codex")


def pid_of(runtime: str) -> int | None:
    if runtime == "claude":
        pid = identity.pid_from_socket(os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", ""))
        if pid:
            return pid
        return identity.ancestor_pid({"claude"})
    return identity.ancestor_pid({"codex"}) or os.getppid()


def session_folder(runtime: str, data: dict) -> str:
    """The folder a session belongs to, which decides its scope. For Claude
    that is where it started: the hook input's `cwd` follows every `cd` its
    Bash tool makes, so one `cd` into a subfolder moved the session there in
    `xsm who` and in every scope check (2026-09-23). Claude Code gives hooks
    the start folder as CLAUDE_PROJECT_DIR. Codex's shell does not keep a
    `cd`, so its `cwd` already is the start folder."""
    if runtime == "claude" and os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.environ["CLAUDE_PROJECT_DIR"]
    return data.get("cwd") or os.getcwd()


def register(data: dict, runtime: str) -> dict | None:
    session_id = data.get("session_id")
    home = home_of(runtime, data)
    pid = pid_of(runtime)
    if session_id and not pid:
        # The session env vars are missing (an older runtime, an odd launcher).
        # If this session registered before, its own pointer still knows the pid.
        known = registry.by_session(runtime, session_id)
        pid = known and known.get("pid")
    if not (session_id and home and pid):
        paths.append_jsonl("decisions.jsonl", {
            "event": data.get("hook_event_name"), "runtime": runtime,
            "decision": "register-skipped",
            "reason": "missing %s" % ", ".join(
                n for n, v in (("session_id", session_id), ("home", home), ("pid", pid)) if not v)})
        return None
    return registry.upsert(runtime, home, session_id, pid, session_folder(runtime, data),
                           permission_mode=data.get("permission_mode"),
                           name=data.get("session_title"))


def _emit(runtime: str, payload: dict | None) -> None:
    if payload:
        print(json.dumps(payload, ensure_ascii=False))


def allow_with_context(runtime: str, context: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": context}}


def hold(runtime: str, reason: str, data: dict, me: dict | None, parsed) -> bool:
    """Keep the body before refusing it. A Codex hook block consumes the queue
    item and leaves no trace in the transcript (S6), so if we do not store it
    the message is simply gone. If storing fails we do not block at all."""
    try:
        paths.write_json(paths.path(paths.HELD, "%d.json" % int(time.time() * 1000)), {
            "t": time.time(), "reason": reason, "runtime": runtime,
            "receiver": me and me.get("name"), "id": parsed.header.get("id"),
            # Whatever the sender revealed, in order of usefulness: our own
            # header, the envelope's display name, then the raw reply address.
            # An injected message has none of the first two (S8-g2), and the
            # socket path is then the only trace of where it came from.
            "from": (parsed.header.get("from") or parsed.attrs.get("from-name")
                     or parsed.attrs.get("from") or "unknown"),
            "scope": parsed.header.get("scope"), "body": parsed.body[:4000]})
        return True
    except OSError:
        return False


def block(runtime: str, reason: str) -> dict:
    out = {"decision": "block", "reason": "xsm: %s" % reason}
    if runtime == "claude":
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit",
                                     "suppressOriginalPrompt": True}
    return out


def handle(data: dict) -> dict | None:
    """One span per hook call. A session's first sign of life is its
    SessionStart hook; without a span there, a session that never came up
    and one that came up and did nothing look the same from outside."""
    try:
        from . import telemetry
    except ImportError:
        return _handle(data)
    event = data.get("hook_event_name") or "unknown"
    with telemetry.span("xsm.hook.%s" % event, {"xsm.hook.event": event,
                                                "xsm.hook.runtime": detect_runtime(data)}):
        return _handle(data)


def _handle(data: dict) -> dict | None:
    runtime = detect_runtime(data)
    if data.get("hook_event_name") == "PermissionRequest":
        return workers.permission_request(data, runtime)
    if data.get("hook_event_name") == "SessionEnd":
        # Do not re-register on the way out; just note the goodbye.
        if data.get("session_id"):
            ended = registry.mark_ended(runtime, data["session_id"], data.get("reason"))
            workers.reap_detached(ended)    # its workers have nobody to report to now
        return None
    me = register(data, runtime)
    if me:
        # The pointer as written lacks what the runtime keeps elsewhere — a Codex
        # thread's name lives in its state DB — so read it back the way every
        # other lookup does, or receipts and decisions record no receiver name.
        me = registry.by_session(runtime, me["session_id"]) or me
    if data.get("hook_event_name") == "SessionStart":
        housekeeping.maybe_prune()
        return None
    if data.get("hook_event_name") != "UserPromptSubmit":
        return None
    parsed = envelope.parse(data.get("prompt") or "")
    if not parsed.peer:
        return None                      # ordinary human input: say nothing
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    # Opened here, not in main(): the traceparent is only known once the
    # envelope is parsed, and main()'s catch-all must stay the outermost thing
    # in this file.
    span_cm = telemetry.span("xsm.receive.gate", {"xsm.msg.id": parsed.header.get("id")},
                             kind="CONSUMER",
                             traceparent=parsed.header.get("traceparent")) \
        if telemetry else nullcontext()
    with span_cm as span:
        out = _gate(data, runtime, me, parsed, span)
        if telemetry and span is not None:
            telemetry.counter("xsm.receive.count", 1,
                              {"xsm.receive.decision": span.attributes.get("xsm.receive.decision")})
        return out


def _gate(data: dict, runtime: str, me: dict | None, parsed, span=None) -> dict | None:
    msg_id = parsed.header.get("id")
    if msg_id and ledger.received(msg_id):
        # Already handed over, almost always by `xsm inbox` while the Codex
        # turn was still running; this is the queue's own copy arriving late.
        # Not held: the session has it. Refusing is what drops it from Codex.
        inbox.drop((me or {}).get("session_id"), msg_id)
        if span is not None:
            span.set_attribute("xsm.receive.decision", "duplicate")
        paths.append_jsonl("decisions.jsonl", {
            "event": "UserPromptSubmit", "runtime": runtime, "decision": "block",
            "reason": "already received", "id": msg_id, "receiver": me and me.get("name")})
        return block(runtime, "message %s was already received (xsm inbox)" % msg_id)
    decision, reason = check(parsed, me)
    if msg_id:
        inbox.drop((me or {}).get("session_id"), msg_id)

    if decision == "block":
        stored = hold(runtime, reason, data, me, parsed)
        if span is not None:
            span.set_attribute("xsm.receive.decision", "held" if stored else "blocked")
            span.set_status("ERROR", reason)
        paths.append_jsonl("decisions.jsonl", {
            "event": "UserPromptSubmit", "runtime": runtime, "decision": "block",
            "reason": reason, "held": stored, "id": msg_id,
            "receiver": me and me.get("name"), "from": parsed.header.get("from")})
        if msg_id:
            ledger.receipt(msg_id, "held" if stored else "blocked", me, reason)
        if not stored:
            # Refusing without a copy would destroy the message; warn instead.
            return allow_with_context(runtime, envelope.sender_context(parsed, runtime) +
                                      "\n[xsm] This message failed a check (%s) but could not "
                                      "be stored, so it was delivered with this warning." % reason)
        return block(runtime, reason + " (kept: xsm held list)")

    if span is not None:
        span.set_attribute("xsm.receive.decision", "pass")
    paths.append_jsonl("decisions.jsonl", {
        "event": "UserPromptSubmit", "runtime": runtime, "decision": "pass", "reason": reason,
        "id": msg_id, "receiver": me and me.get("name"), "from": parsed.header.get("from")})
    if msg_id:
        ledger.receipt(msg_id, "delivered", me)
    if parsed.header.get("kind") == "reply":
        workers.on_reply(parsed.header.get("ref"), parsed.header.get("reply-to"), me)
    worker = workers.load(os.environ.get("XSM_WORKER") or "") if os.environ.get("XSM_WORKER") else None
    return allow_with_context(runtime, envelope.sender_context(
        parsed, runtime, worker=bool(worker), cwd=(me or {}).get("cwd")))


def check(parsed, me: dict | None, cfg: dict | None = None) -> tuple:
    """(decision, reason) for a peer message: the same checks whether it came
    in through the hook or was taken with `xsm inbox`."""
    cfg = cfg if cfg is not None else config.load()
    decision, reason = "pass", ""
    if not parsed.header:
        decision, reason = ("block", "peer message without an xsm header") \
            if cfg.get("strict_peers", True) else ("pass", "unheadered peer message allowed")
    elif not me:
        # Without knowing which session we are, scope cannot be checked at all.
        # Passing here would turn an unidentifiable session into an open door.
        decision, reason = "block", "cannot identify this session, so scope was not checked"
    elif parsed.header.get("origin"):
        # From a paired machine (ADR-0007): trusted only if this machine's own
        # receiver recorded the id for that peer, and only into its project.
        from . import remote
        peer = parsed.header["origin"]
        pairing = remote.pairing_for(peer)
        msg = parsed.header.get("id") or ""
        if not pairing or not remote.recorded_inbound(msg, peer):
            decision, reason = "block", "remote message from %s was not received by this " \
                                        "machine's xsm" % peer
        elif parsed.header.get("scope") != "remote:%s" % peer:
            decision, reason = "block", "remote message carries the wrong scope"
        elif not remote._project_members(pairing["local_project"], me.get("cwd") or "/"):
            decision, reason = "block", "this session is not in %s, the project paired with %s" % (
                pairing["local_project"], peer)
        elif me.get("ref") in config.blocked():
            decision, reason = "block", "a blocked session is on this message"
    else:
        sender = _sender_record(parsed)
        if sender is None:
            decision, reason = "block", "sender %r is not registered" % parsed.header.get("from")
        elif sender.get("state") not in ("live", "unknown"):
            # A stopped session's pointer stays for days; its name must not
            # carry a message now (S8-e, ADR-0009).
            decision, reason = "block", "sender %r is not running (%s)" % (
                parsed.header.get("from"), sender.get("state"))
        elif sender.get("ref") in config.blocked() or (me.get("ref") in config.blocked()):
            decision, reason = "block", "a blocked session is on this message"
        else:
            scope, why = config.scope_for(sender, me, cfg)
            if not scope:
                decision, reason = "block", "out of scope: %s" % why
            elif scope != parsed.header.get("scope"):
                decision, reason = "block", "scope changed since the message was sent"
    return decision, reason


def take_inbox(me: dict) -> list:
    """Messages waiting for this Codex session, handed over now instead of at
    the end of its turn. Each goes through check() exactly as the hook would
    run it, gets the same receipt, and comes back as the text the hook would
    have shown: sender context, then the body. A refused one is kept in the
    held list and comes back as a one-line notice."""
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    runtime = me.get("runtime") or "codex"
    worker = bool(os.environ.get("XSM_WORKER") and workers.load(os.environ["XSM_WORKER"]))
    out = []
    for item in inbox.take(str(me.get("session_id"))):
        parsed = envelope.parse(item["content"])
        msg_id = parsed.header.get("id") or item.get("id")
        if msg_id and ledger.received(msg_id):
            continue                     # the hook got there first
        span_cm = telemetry.span("xsm.receive.inbox", {"xsm.msg.id": msg_id}, kind="CONSUMER",
                                 traceparent=parsed.header.get("traceparent")) \
            if telemetry else nullcontext()
        with span_cm as span:
            decision, reason = check(parsed, me)
            if decision == "block":
                stored = hold(runtime, reason, {}, me, parsed)
                ledger.receipt(msg_id, "held" if stored else "blocked", me, reason)
                out.append("[xsm] Message %s from %s was refused (%s)%s." % (
                    msg_id, parsed.header.get("from") or "unknown", reason,
                    "; kept in the held list" if stored else ""))
            else:
                ledger.receipt(msg_id, "delivered", me, "read with xsm inbox")
                if parsed.header.get("kind") == "reply":
                    workers.on_reply(parsed.header.get("ref"), parsed.header.get("reply-to"), me)
                out.append(envelope.sender_context(parsed, runtime, worker=worker,
                                                   cwd=me.get("cwd")) + "\n\n" + parsed.body)
            if span is not None:
                span.set_attribute("xsm.receive.decision",
                                   "pass" if decision != "block" else "held")
            paths.append_jsonl("decisions.jsonl", {
                "event": "inbox", "runtime": runtime, "decision": decision, "reason": reason,
                "id": msg_id, "receiver": me.get("name"), "from": parsed.header.get("from")})
    return out


def _sender_record(parsed) -> dict | None:
    """The sender as the registry knows it, matched on the ref in the header —
    names change while a session runs, refs do not (S7)."""
    ref = parsed.header.get("ref")
    for rec in registry.records():
        if ref and rec.get("ref") == ref:
            return rec
    return None


def main(argv=None) -> int:
    raw = sys.stdin.read()
    data = {}
    try:
        data = json.loads(raw or "{}")
        if os.environ.get("XSM_FORCE_ERROR"):          # `xsm selftest` exercises the fallback
            raise RuntimeError("forced by XSM_FORCE_ERROR")
        _emit(detect_runtime(data), handle(data))
        return 0
    except BaseException as err:                       # noqa: BLE001 - see module docstring
        prompt = (data.get("prompt") or "") if isinstance(data, dict) else ""
        # The raw text matters too: if json parsing is what failed, the prompt
        # field was never extracted, and a peer message would look like a human's.
        looks_like_peer = envelope.looks_like_peer(prompt) or envelope.looks_like_peer(raw)
        paths.append_jsonl("decisions.jsonl", {
            "event": (data or {}).get("hook_event_name"), "decision":
                "block" if looks_like_peer else "pass",
            "reason": "xsm internal error: %s" % type(err).__name__,
            "detail": str(err)[:300], "peer_like": looks_like_peer})
        if (data or {}).get("hook_event_name") == "PermissionRequest":
            # No answer means the runtime's own default, which for a background
            # worker is to refuse. Never print a prompt decision here.
            return 0
        if looks_like_peer:
            print(json.dumps({
                "decision": "block",
                "reason": "xsm: internal error while checking this peer message; "
                          "it was not delivered. Run `xsm doctor`.",
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "suppressOriginalPrompt": True}}))
        return 0
