"""The receive side: one hook entry point for both runtimes.

Two rules shape this file.

1. A hook that dies stops gating. Claude reports a crashed hook as a
   non-blocking error and runs the prompt anyway, so a broken gate is an open
   gate (S8-g2). Everything here runs under a catch-all. Until 2026-10-01 its
   fallback refused what looked like a peer message; a person pasting an xsm
   log was refused with it, and a conversation they wanted was lost to our
   bug. User decision, 2026-10-01: a gate that breaks steps aside. The prompt
   goes through with a note that it was not checked (policy `fail_open`;
   false brings the refusal back), and nothing here ever blocks a person's own
   prompt. A write that fails (a state folder that cannot be written) decides
   nothing either. With `fail_open` false all of that is the way it was: a session
   that cannot be identified, a sender nobody registered and a failed write each
   block again (2026-10-02: the switch had restored only the catch-all).
2. The runtime is decided from the hook input's own fields, not from path
   strings (ADR-0001 draft A'), because CODEX_HOME and CLAUDE_CONFIG_DIR can
   live anywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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


def stated_home(runtime: str, data: dict) -> str | None:
    """home_of, but only when the hook says it: the runtime's home variable or
    its transcript's location. None where home_of would fall back to the
    default folder, which is a guess and must not overrule a pointer's home."""
    env = "CLAUDE_CONFIG_DIR" if runtime == "claude" else "CODEX_HOME"
    marker = "/projects/" if runtime == "claude" else "/sessions/"
    if os.environ.get(env) or marker in (data.get("transcript_path") or ""):
        return home_of(runtime, data)
    return None


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
    try:
        return registry.upsert(runtime, home, session_id, pid, session_folder(runtime, data),
                               permission_mode=data.get("permission_mode"),
                               name=data.get("session_title"),
                               mcp_pid=registry.beacon_for(pid) if runtime == "codex" else None,
                               socket=os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
                               if runtime == "claude" else None,
                               inside=os.environ.get("CODEX_THREAD_ID" if runtime == "claude"
                                                     else "CLAUDE_CODE_SESSION_ID") or None)
    except OSError as err:
        # A state folder that cannot be written must not make a session
        # unknown to the gate, which then could not check anything (2026-10-01).
        if _fail_closed():
            raise
        _write_failed("registry.upsert", err)
        return registry.by_session(runtime, session_id)


def _write_failed(what: str, err: OSError) -> None:
    paths.append_jsonl("decisions.jsonl", {"event": "write-failed", "decision": "pass",
                                           "reason": "%s: %s" % (what, type(err).__name__)})


def _safely(fn, *args, **kwargs):
    """Run a write the verdict does not depend on. The check has decided; a
    ledger or inbox that cannot be written must not turn that into a block, or
    into an error that blocks (2026-10-01). With fail_open false it is the error
    again, which the catch-all in main() turns into a refusal, as it was."""
    try:
        return fn(*args, **kwargs)
    except OSError as err:
        if _fail_closed():
            raise
        _write_failed(getattr(fn, "__name__", "write"), err)
        return None


def _emit(runtime: str, payload: dict | None) -> None:
    if payload:
        print(json.dumps(payload, ensure_ascii=False))


def _outcome(parsed) -> str | None:
    """How the sender says a task ended, if it said so in a way this version
    knows. An unknown value folds to nothing: the message is still delivered,
    it simply carries no outcome here (PROTOCOL 7)."""
    value = parsed.header.get("outcome")
    return value if value in envelope.OUTCOMES else None


def _close_task(parsed, outcome: str | None) -> None:
    """Record on the task's own ledger entry how it ended. The task was sent
    from here, so its entry is in this machine's ledger; the worker that
    answered may be stopped and forgotten a moment later."""
    task_id = parsed.header.get("reply-to")
    if task_id and outcome:
        ledger.close(task_id, outcome, {"name": parsed.header.get("from"),
                                        "ref": parsed.header.get("ref")})


def allow_with_context(runtime: str, context: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": context}}


def hold(runtime: str, reason: str, data: dict, me: dict | None, parsed) -> str | None:
    """Keep the body before refusing it. A Codex hook block consumes the queue
    item and leaves no trace in the transcript (S6), so if we do not store it
    the message is simply gone. If storing fails we do not block at all."""
    name = "%d" % int(time.time() * 1000)
    try:
        paths.write_json(paths.path(paths.HELD, "%s.json" % name), {
            "t": time.time(), "reason": reason, "runtime": runtime,
            "receiver": me and me.get("name"), "id": parsed.header.get("id"),
            # Whatever the sender revealed, in order of usefulness: our own
            # header, the envelope's display name, then the raw reply address.
            # An injected message has none of the first two (S8-g2), and the
            # socket path is then the only trace of where it came from.
            "from": (parsed.header.get("from") or parsed.attrs.get("from-name")
                     or parsed.attrs.get("from") or "unknown"),
            "scope": parsed.header.get("scope"), "body": parsed.body[:4000],
            "truncated": len(parsed.body) > 4000,
            # What `xsm held deliver` needs to show it as it would have been:
            # who it was for, and the header and envelope it came with.
            "receiver_ref": me and me.get("ref"), "header": parsed.header,
            "attrs": parsed.attrs})
        return name
    except OSError:
        return None


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


def _link_cli() -> None:
    """Keep ~/.local/bin/xsm on the Codex plugin that runs this hook.

    Codex, unlike Claude, puts no plugin bin/ on PATH, and the skill runs `xsm`
    by name (measured with Codex 0.158, 2026-09-29). A plugin has no install
    step of its own, so session start is where the link is made, and where it
    follows the plugin to a new version. Only from a plugin copy: a checkout's
    hook leaves PATH to the person, and install_cli() itself moves nothing but
    a link into another plugin version."""
    from . import install
    if not re.search(install.PLUGIN_CACHE, install.REPO + "/"):
        return
    try:
        install.install_cli()
    except OSError:
        pass                            # a read-only ~/.local/bin must not stop the session


def _handle(data: dict) -> dict | None:
    runtime = detect_runtime(data)
    if data.get("hook_event_name") == "PermissionRequest":
        return workers.permission_request(data, runtime)
    if data.get("hook_event_name") == "SessionEnd":
        # Do not re-register on the way out; just note the goodbye.
        if data.get("session_id"):
            ended = registry.mark_ended(runtime, data["session_id"], data.get("reason"),
                                        home=stated_home(runtime, data))
            workers.reap_detached(ended)    # its workers have nobody to report to now
            if ended and ended.get("ref"):
                # A reach lasts until its session ends. Waiting for the hourly
                # prune left it standing for a resume under a new pid (2026-09-28).
                config.drop_reach(ended["ref"], session=ended)
        return None
    if data.get("hook_event_name") == "UserPromptExpansion":
        # Claude: the person typed a slash command. Its only use here is a
        # typed `/xsm link <folder>` as their consent; the expansion itself
        # goes on untouched, so nothing is printed.
        if data.get("session_id"):
            _record_consent(registry.by_session(runtime, data["session_id"]), data)
        return None
    if data.get("hook_event_name") == "PostToolUse":
        # Claude: only AskUserQuestion is installed (its matcher). The person's
        # answer comes back as the tool's result, which no prompt hook sees, so
        # it is kept here as the reply to an ask (consent.py). Nothing printed.
        if data.get("tool_name") == "AskUserQuestion" and data.get("session_id"):
            _record_consent(registry.by_session(runtime, data["session_id"]), data)
        return None
    me = register(data, runtime)
    if me:
        # The pointer as written lacks what the runtime keeps elsewhere — a Codex
        # thread's name lives in its state DB — so read it back the way every
        # other lookup does, or receipts and decisions record no receiver name.
        me = registry.by_session(runtime, me["session_id"]) or me
    if data.get("hook_event_name") == "SessionStart":
        housekeeping.maybe_prune()
        if runtime == "codex":
            _link_cli()
        return None
    if data.get("hook_event_name") != "UserPromptSubmit":
        return None
    parsed = envelope.parse(data.get("prompt") or "")
    if not parsed.peer:
        # Ordinary human input: say nothing. A Codex `$xsm link <folder>` (and
        # join, leave, reach) is kept as the person's consent (consent.py).
        _record_consent(me, data)
        return _with_bounces(runtime, me, None)
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
        return _with_bounces(runtime, me, out)


def _with_bounces(runtime: str, me: dict | None, out: dict | None) -> dict | None:
    """Add, once, the notes left for this session about its held SendMessages
    (issue #8). A refused prompt cannot carry context, so they wait for the
    next one that goes through."""
    if not me or (out is not None and "decision" in out):
        return out
    try:
        from . import bounce
        notes = bounce.notice(me.get("session_id"))
    except Exception:                   # noqa: BLE001 - a note must not cost the prompt
        return out
    if not notes:
        return out
    if out is None:
        return allow_with_context(runtime, notes)
    spec = out.setdefault("hookSpecificOutput", {"hookEventName": "UserPromptSubmit"})
    spec["additionalContext"] = ((spec.get("additionalContext") or "") + "\n\n" + notes).strip()
    return out


def _record_consent(me: dict | None, data: dict) -> None:
    """Keep a typed xsm command as the person's consent. A failure must not
    touch the prompt, so it is only noted."""
    try:
        from . import consent
        consent.record(me, data)
    except Exception as err:            # noqa: BLE001 - never lock the person out
        paths.append_jsonl("decisions.jsonl", {
            "event": data.get("hook_event_name"), "decision": "pass",
            "reason": "consent not recorded: %s" % type(err).__name__})


# A second hook run for the same message, within this long, is a second
# registration of the one hook (a plugin and a direct install in one home), not
# a second copy of the message (2026-10-01).
REPEAT_WINDOW = 60.0
# What `xsm inbox` writes as the reason of its receipt; the hook's are empty or
# say "unchecked".
VIA_INBOX = "read with xsm inbox"


def _context(decision: str, reason: str, parsed, runtime: str, **kw) -> str:
    """What the agent reads above a message that passed: the sender's, and
    for one xsm could not check the line that says so first."""
    if decision == "unchecked":
        return envelope.unchecked_context(reason, parsed, runtime, **kw)
    return envelope.sender_context(parsed, runtime, **kw)


def _body_sha(parsed) -> str:
    return hashlib.sha256((parsed.body or "").encode("utf-8", "replace")).hexdigest()[:16]


def _repeat(runtime: str, me: dict | None, parsed, msg_id: str) -> dict | None:
    """The same context again, for the second run of a hook this message already
    passed through (2026-10-01). Refusing it as "already received" refused the
    whole prompt, and the person's session got nothing. Only the same receiver
    within REPEAT_WINDOW, and only a delivery the hook itself recorded: the
    queue copy that arrives after `xsm inbox` read the message stays refused (it
    is what drops it from Codex's queue). And only the same message: the receipt
    keeps the sender's ref and a hash of the body, because a header that reuses a
    just-delivered id is otherwise a free pass for any text (2026-10-02). A
    receipt without them (an older hook's) is not a repeat."""
    rec = ledger.receipt_of(msg_id)
    if not rec or rec.get("decision") != "delivered" or \
            (rec.get("reason") or "").startswith(VIA_INBOX):
        return None
    if rec.get("from_ref") != parsed.header.get("ref") or rec.get("body_sha") != _body_sha(parsed):
        return None
    # A session xsm could not identify has no ref, on either run: the same hook
    # twice for it is still the same hook twice.
    if (rec.get("receiver") or {}).get("ref") != (me or {}).get("ref") or \
            time.time() - float(rec.get("t") or 0) > REPEAT_WINDOW:
        return None
    reason = rec.get("reason") or ""
    unchecked = reason.startswith("unchecked: ")
    worker = workers.load(os.environ["XSM_WORKER"]) if os.environ.get("XSM_WORKER") else None
    return allow_with_context(runtime, _context(
        "unchecked" if unchecked else "pass", reason[len("unchecked: "):] if unchecked else "",
        parsed, runtime, worker=bool(worker), cwd=(me or {}).get("cwd")))


def _gate(data: dict, runtime: str, me: dict | None, parsed, span=None) -> dict | None:
    msg_id = parsed.header.get("id")
    if msg_id and ledger.received(msg_id):
        # Already handed over, almost always by `xsm inbox` while the Codex
        # turn was still running; this is the queue's own copy arriving late.
        # Not held: the session has it. Refusing is what drops it from Codex.
        inbox.drop((me or {}).get("session_id"), msg_id)
        again = _repeat(runtime, me, parsed, msg_id)
        if again is not None:
            if span is not None:
                span.set_attribute("xsm.receive.decision", "repeat")
            paths.append_jsonl("decisions.jsonl", {
                "event": "UserPromptSubmit", "runtime": runtime, "decision": "pass",
                "reason": "the same hook ran twice for this message", "id": msg_id,
                "receiver": me and me.get("name")})
            return again
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
            "receiver": me and me.get("name"), "from": _claimed_sender(parsed)})
        if msg_id:
            _safely(ledger.receipt, msg_id, "held" if stored else "blocked", me, reason)
        if stored:
            _tell_held(parsed, me, reason, stored)
        if not stored:
            # Refusing without a copy would destroy the message; warn instead.
            return allow_with_context(runtime, envelope.sender_context(parsed, runtime) +
                                      "\n[xsm] This message failed a check (%s) but could not "
                                      "be stored, so it was delivered with this warning." % reason)
        return block(runtime, "%s (kept as %s; your agent delivers it on your yes: "
                              "xsm held deliver %s)" % (reason, stored, stored))

    if span is not None:
        span.set_attribute("xsm.receive.decision", decision)
    paths.append_jsonl("decisions.jsonl", {
        "event": "UserPromptSubmit", "runtime": runtime, "decision": "pass", "reason": reason,
        "unchecked": decision == "unchecked", "id": msg_id, "receiver": me and me.get("name"),
        "from": _claimed_sender(parsed)})
    if not parsed.header and decision != "unchecked":
        return None                     # Claude's own framing stands (ADR-0013)
    outcome = _outcome(parsed)
    if msg_id:
        _safely(ledger.receipt, msg_id, "delivered", me,
                "unchecked: %s" % reason if decision == "unchecked" else "", outcome=outcome,
                from_ref=parsed.header.get("ref"), body_sha=_body_sha(parsed))
    if parsed.header.get("kind") == "reply":
        _safely(_close_task, parsed, outcome)
        _safely(workers.on_reply, parsed.header.get("ref"), parsed.header.get("reply-to"), me,
                outcome)
    worker = workers.load(os.environ.get("XSM_WORKER") or "") if os.environ.get("XSM_WORKER") else None
    return allow_with_context(runtime, _context(decision, reason, parsed, runtime,
                                                worker=bool(worker), cwd=(me or {}).get("cwd")))


def _tell_held(parsed, me: dict | None, reason: str, held: str) -> None:
    """Both sides learn of a hold (issue #8; 2026-10-01 for xsm's own messages
    and for the receiver's agent): the sender in its next prompt or command, the
    receiver's agent so it can offer the person `xsm held deliver`. A note must
    not cost the gate its answer, so a failure is only logged."""
    try:
        _bounce_sender(parsed, me, reason, held)
        from . import bounce
        bounce.record_held_here(me, _claimed_sender(parsed) or "an unknown sender", reason, held)
    except Exception as err:            # noqa: BLE001 - the hold already happened
        paths.append_jsonl("decisions.jsonl", {"event": "bounce", "decision": "pass",
                                               "reason": "note not left: %s" % type(err).__name__})


def check(parsed, me: dict | None, cfg: dict | None = None) -> tuple:
    """(decision, reason) for a peer message: the same checks whether it came
    in through the hook or was taken with `xsm inbox`. The decision is "pass",
    "block", or "unchecked": passed, but xsm could not tell who it is from or
    whether the scope holds, and the agent is told so (2026-10-01)."""
    cfg = cfg if cfg is not None else config.load()
    decision, reason = "pass", ""
    if not parsed.header:
        decision, reason = _check_native(parsed, me, cfg)
    elif not me:
        decision, reason = _unidentified(cfg)
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
        sender, why_not = _sender_record(parsed)
        mismatch = _socket_mismatch(parsed, sender) if sender is not None else None
        if sender is None:
            decision, reason = _unknown_sender(parsed, why_not, me, cfg)
        elif mismatch:
            decision, reason = "block", mismatch
        else:
            decision, reason, scope = _check_sender(sender, me, cfg, parsed)
            if decision != "block" and scope != parsed.header.get("scope"):
                decision, reason = "block", "scope changed since the message was sent"
    return decision, reason


def _unidentified(cfg: dict) -> tuple:
    """(decision, reason) for a session xsm cannot identify. Without knowing which
    session we are, scope cannot be checked at all. That is our own gap, and a
    conversation the person wants is not held for it: the message goes through and
    says it was not checked. fail_open = false is the old refusal."""
    if config.policy("fail_open", cfg=cfg):
        return "unchecked", "this session could not be identified, so scope was not checked"
    return "block", "cannot identify this session, so scope was not checked"


def _unknown_sender(parsed, why_not: str, me: dict, cfg: dict) -> tuple:
    """(decision, reason) for a header whose ref no registered session has. A person
    who pastes an xsm log into their own prompt writes exactly that, and was refused
    for it (2026-10-02). It passes, unchecked, unless this machine's ledger shows
    that ref sent this very id (then it only lost its registry record, and it passes
    as checked); a session a person blocked stays blocked. A ref that is ambiguous,
    and fail_open = false, are the refusal as it was."""
    claimed, ref = parsed.header.get("from"), parsed.header.get("ref")
    if "is not registered" not in why_not or not config.policy("fail_open", cfg=cfg):
        return "block", why_not
    if ref in config.blocked() or me.get("ref") in config.blocked():
        return "block", "a blocked session is on this message"
    if ledger.sent_by(parsed.header.get("id"), ref):
        return "pass", "sender %r is no longer registered; this machine's ledger shows it sent " \
                       "this message" % claimed
    return "unchecked", "sender %r is not a session this machine knows, and it has no record of " \
                        "that message being sent%s" % (
                            claimed, "" if parsed.attrs else " (it has an xsm header but no Claude "
                                                             "envelope: it may be text your user "
                                                             "pasted)")


def _socket_mismatch(parsed, sender: dict) -> str | None:
    """Why the envelope's own sender does not fit the header's, or None.

    The header is text the sender writes; the envelope's from="uds:<socket>"
    on a Claude message is filled in by Claude. A model talked into writing
    `[xsm v1 … ref=<someone else's ref>]` at the top of a SendMessage would
    otherwise be taken for that session (review of ADR-0013, 2026-09-29).
    xsm's own sends write the sender's socket into the envelope, and a Codex
    sender has none, so neither is affected. Where either side has no socket
    there is nothing to compare."""
    where = parsed.attrs.get("from") or ""
    if not where.startswith("uds:"):
        return None
    if sender.get("runtime") != "claude":
        return ("the message came through a Claude session's socket (%s) but claims %r, "
                "which is not a Claude session" % (where, parsed.header.get("from")))
    if sender.get("socket") and sender["socket"] != where[len("uds:"):]:
        return ("the message came from %s, which is not the socket of %r"
                % (where, parsed.header.get("from")))
    return None


def _claimed_sender(parsed) -> str | None:
    """Who the message says it is from, for the decision log: the xsm header's
    name, else Claude's envelope name and socket (a native message has no header)."""
    if parsed.header.get("from"):
        return parsed.header["from"]
    name, where = parsed.attrs.get("from-name"), parsed.attrs.get("from")
    return " ".join(x for x in (name, where and "(%s)" % where) if x) or None


def _check_sender(sender: dict, me: dict, cfg: dict, parsed) -> tuple:
    """(decision, reason, scope) for a sender the registry knows: not blocked,
    and in a scope with this session; running too, unless it exited after
    sending. An xsm message only (Claude's own never gets here).

    A stopped session's pointer stays for days, and until 2026-10-01 its name
    could carry nothing (S8-e, ADR-0009), which also lost every message a
    session sent just before it exited. Now a sender that has exited passes when
    this machine's ledger shows that it queued this very message (provenance:
    `xsm send` wrote that entry after its own scope check), and passes noted as
    unchecked when there is no such record. `stale_sender` = hold brings the
    old refusal back.

    The scope is judged from the receiver's side, so a refusal tells this
    session's agent what to ask of its own user, not the sender's."""
    claimed = parsed.header.get("from")
    exited = sender.get("state") not in ("live", "unknown")
    if exited and config.policy("stale_sender", cfg=cfg) == "hold":
        return "block", "sender %r is not running (%s)" % (claimed, sender.get("state")), None
    if sender.get("ref") in config.blocked() or (me.get("ref") in config.blocked()):
        return "block", "a blocked session is on this message", None
    scope, why = config.scope_for(me, sender, cfg)
    if not exited:
        return ("pass", "", scope) if scope else ("block", "out of scope: %s" % why, None)
    proven = ledger.sent_by(parsed.header.get("id"), sender.get("ref"))
    if not scope and proven and (parsed.header.get("scope") or "").startswith("reach:"):
        # A reach ends with the session that holds it, by design; the send was
        # allowed while it stood.
        scope = parsed.header["scope"]
    if not scope:
        return "block", "out of scope: %s" % why, None
    if proven:
        return "pass", "sender %r had exited (%s); this machine's ledger shows it sent this " \
                       "message" % (claimed, sender.get("state")), scope
    return "unchecked", "sender %r is not running (%s) and this machine has no record of it " \
                        "sending this message" % (claimed, sender.get("state")), scope


def _native_owners(parsed) -> list:
    """The running Claude sessions that own the envelope's uds: socket. /clear
    and --resume keep the process and its socket under a new session id; the
    old record reads as ended (superseded), and must not make the socket look
    shared (measured 2026-09-29)."""
    where = parsed.attrs.get("from") or ""
    socket = where[len("uds:"):] if where.startswith("uds:") else ""
    if not socket:
        return []
    return [rec for rec in registry.records()
            if rec.get("runtime") == "claude" and rec.get("socket") == socket
            and rec.get("state") in ("live", "unknown")]


def _bounce_sender(parsed, me: dict | None, reason: str, held: str) -> None:
    """Leave the sender of a held message a note it will see (issue #8).

    A native message's sender is the one running session that owns its socket.
    An xsm message's is the one its header's ref names, and only when this
    machine's ledger shows that session queued this id: a header is text the
    sender writes, and a forged one must not put a note in someone else's
    session. A message from another machine has no sender here. Never ourselves."""
    if parsed.header:
        if parsed.header.get("origin"):
            return
        sender, _ = _sender_record(parsed)
        owners = [sender] if sender and ledger.sent_by(parsed.header.get("id"),
                                                       sender.get("ref")) else []
    else:
        owners = _native_owners(parsed)
    if len(owners) != 1 or (me and owners[0].get("session_id") == me.get("session_id")):
        return
    from . import bounce
    connect = config.project_root(me["cwd"]) \
        if not parsed.header and me and me.get("cwd") and reason.startswith("out of scope") \
        else None
    bounce.record(owners[0], me, reason, held, parsed.body, connect)


def _check_native(parsed, me: dict | None, cfg: dict) -> tuple:
    """A peer message with Claude's envelope and no xsm header: Claude's own
    SendMessage. Claude's gate has already let it in (it decides before this
    hook runs, S1). From a socket on this machine it passes, whoever sent it
    and whatever the scope: the sender is the same user on the same machine,
    who could write the socket directly anyway, so holding it guarded nothing
    and only broke connections Claude itself makes — and it held exactly the
    sessions xsm knew while letting unknown ones through (user decision,
    2026-10-01, amending ADR-0013 again). Scope still governs xsm's own
    messages, which `xsm send` refuses before sending. A session a person
    blocked with `xsm block` stays blocked.

    A sender that is not a socket on this machine (Remote Control, a cloud
    session, another machine) was held, as past the uid boundary (2026-09-30).
    It passes now, with a note naming where it came from (user decision,
    2026-10-01, amending ADR-0013: a person who connected their own phone or
    cloud session to talk to this one is not to be refused by a rule they
    cannot see). `remote_native` = hold brings the hold back.

    Claude's envelope carries from="uds:<socket>", from-name and from-mode,
    and no session id (measured with Claude 2.1.284, 2026-09-29). The socket
    is the sender's own claim, as the ref in an xsm header is; it names the
    sender only when exactly one running session owns it."""
    if cfg.get("strict_peers", False):
        return "block", "peer message without an xsm header (strict_peers)"
    if not me:
        return _unidentified(cfg)
    if me.get("runtime") != "claude":
        # Claude sessions are the only ones Claude's messaging reaches; the same
        # text arriving through a Codex queue came some other way.
        return "block", "peer message without an xsm header"
    where = parsed.attrs.get("from") or ""
    socket = where[len("uds:"):] if where.startswith("uds:") else ""
    running = _native_owners(parsed)
    blocked = config.blocked()
    if me.get("ref") in blocked or (len(running) == 1 and running[0].get("ref") in blocked):
        return "block", "a blocked session is on this message"
    if not socket:
        origin = "Claude peer message from %s, which is not a session on this machine" % (
            where or "an unnamed sender")
        if config.policy("remote_native", cfg=cfg) == "hold":
            return "block", origin
        return "unchecked", "it came from %s, which is not a session on this machine (Remote " \
                            "Control, a cloud session or another machine)" % (
                                where or "an unnamed sender")
    if len(running) == 1:
        return "pass", "local Claude peer message from %s@%s" % (
            running[0].get("name"), running[0].get("alias"))
    return "pass", "local Claude peer message from %s (%s)" % (
        parsed.attrs.get("from-name") or "unnamed", where)


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
        if item.get("queued_id"):
            # Handed over here, so its queue copy must not arrive after the
            # turn as a duplicate the gate refuses — Codex shows each refusal
            # as a "Blocked by hook" card (issue #7). If this cannot reach the
            # daemon (a sandboxed shell), the gate still drops the copy.
            from . import codex_daemon
            codex_daemon.delete_queued(item.get("codex_home") or me.get("home") or "",
                                       str(me.get("session_id")), item["queued_id"])
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
                _safely(ledger.receipt, msg_id, "held" if stored else "blocked", me, reason)
                if stored:
                    _tell_held(parsed, me, reason, stored)
                out.append("[xsm] Message %s from %s was refused (%s)%s." % (
                    msg_id, parsed.header.get("from") or "unknown", reason,
                    "; kept as %s: if your user wants it, ask them, and after they answer run "
                    "`xsm held deliver %s`" % (stored, stored) if stored else ""))
            else:
                outcome = _outcome(parsed)
                _safely(ledger.receipt, msg_id, "delivered", me,
                        "%s; unchecked: %s" % (VIA_INBOX, reason) if decision == "unchecked"
                        else VIA_INBOX, outcome=outcome)
                if parsed.header.get("kind") == "reply":
                    _safely(_close_task, parsed, outcome)
                    _safely(workers.on_reply, parsed.header.get("ref"),
                            parsed.header.get("reply-to"), me, outcome)
                out.append(_context(decision, reason, parsed, runtime, worker=worker,
                                    cwd=me.get("cwd")) + "\n\n" + parsed.body)
            if span is not None:
                span.set_attribute("xsm.receive.decision",
                                   decision if decision != "block" else "held")
            paths.append_jsonl("decisions.jsonl", {
                "event": "inbox", "runtime": runtime, "decision": decision, "reason": reason,
                "id": msg_id, "receiver": me.get("name"), "from": parsed.header.get("from")})
    return out


def _sender_record(parsed) -> tuple:
    """(record, reason): the sender as the registry knows it, matched on the
    ref in the header — names change while a session runs, refs do not (S7).

    A ref is 24 bits and two records can share one (collisions within ~11k
    synthetic tries, 2026-09-28). Taking the first match would check scope and
    liveness against the wrong session. So when several records carry the
    ref, the envelope's from-session, then the header's from name, must pick
    exactly one; otherwise there is no sender and the message is refused.
    Both are the sender's own words, like the ref itself: they narrow the
    choice, they never widen it past the ref."""
    ref = parsed.header.get("ref")
    if not ref:
        return None, "sender %r is not registered" % parsed.header.get("from")
    matches = [rec for rec in registry.records() if rec.get("ref") == ref]
    if not matches:
        return None, "sender %r is not registered" % parsed.header.get("from")
    if len(matches) == 1:
        return matches[0], ""
    sid = parsed.attrs.get("from-session")
    by_id = [rec for rec in matches if sid and str(rec.get("session_id")) == sid]
    if len(by_id) == 1:
        return by_id[0], ""
    name = parsed.header.get("from")
    by_name = [rec for rec in matches if name and "%s@%s" % (rec.get("name"), rec.get("alias")) == name]
    if len(by_name) == 1:
        return by_name[0], ""
    return None, "sender %r is ambiguous: %d sessions share ref %s" % (name, len(matches), ref)


def _fail_closed() -> bool:
    """Whether a broken gate refuses what looks like a peer message (policy
    `fail_open` = false). Asked from the catch-all, so it must not fail: when it
    cannot say, the gate is open."""
    try:
        return not config.policy("fail_open")
    except Exception:                                  # noqa: BLE001 - see module docstring
        return False


def main(argv=None) -> int:
    raw = sys.stdin.read()
    data = {}
    from . import codex_daemon
    codex_daemon.use_hook_budget()      # 10 s hook limit: a hung daemon may cost 0.2 s, not 1 s
    try:
        data = json.loads(raw or "{}")
        if os.environ.get("XSM_FORCE_ERROR"):          # `xsm selftest` exercises the fallback
            raise RuntimeError("forced by XSM_FORCE_ERROR")
        _emit(detect_runtime(data), handle(data))
        return 0
    except BaseException as err:                       # noqa: BLE001 - see module docstring
        if not isinstance(data, dict):
            data = {}
        prompt = data.get("prompt") or ""
        # The raw text matters too: if json parsing is what failed, the prompt
        # field was never extracted, and a peer message would look like a human's.
        looks_like_peer = envelope.looks_like_peer(str(prompt)) or envelope.looks_like_peer(raw)
        event = data.get("hook_event_name")
        # A tool result has already happened and nothing here can hold it back:
        # it never was a block, whatever text it carried (2026-10-01).
        refuse = looks_like_peer and event in ("UserPromptSubmit", None) and _fail_closed()
        paths.append_jsonl("decisions.jsonl", {
            "event": event, "decision": "block" if refuse else "pass",
            "reason": "xsm internal error: %s" % type(err).__name__,
            "detail": str(err)[:300], "peer_like": looks_like_peer})
        if event in ("PermissionRequest", "UserPromptExpansion", "PostToolUse"):
            # No answer means the runtime's own default, which for a background
            # worker is to refuse. Never print a prompt decision here. An
            # expansion is the person's own slash command: nothing to refuse.
            # A tool result has already happened, whatever text it carries.
            return 0
        if refuse:
            print(json.dumps({
                "decision": "block",
                "reason": "xsm: internal error while checking this peer message; "
                          "it was not delivered (fail_open is off). Run `xsm doctor`.",
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "suppressOriginalPrompt": True}}))
        elif looks_like_peer and event in ("UserPromptSubmit", None):
            # The gate broke, and the person's conversation goes on (user
            # decision, 2026-10-01): through, with a note that nothing checked
            # it. A person's own prompt that quotes an xsm log gets the same
            # note, which says only what holds either way.
            print(json.dumps(allow_with_context("claude", envelope.unchecked_context(
                "xsm hit an internal error (%s)" % type(err).__name__))))
        return 0
