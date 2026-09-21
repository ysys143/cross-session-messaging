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

from . import config, envelope, identity, ledger, paths, registry

CLAUDE_FIELDS = ("scratchpad_dir", "session_title", "prompt_id")
CODEX_FIELDS = ("turn_id",)


def detect_runtime(data: dict) -> str:
    if any(f in data for f in CLAUDE_FIELDS) or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if any(f in data for f in CODEX_FIELDS) or os.environ.get("CODEX_HOME"):
        return "codex"
    # SessionStart on Claude carries neither marker; its transcript sits under
    # <CONFIG_DIR>/projects/, while Codex writes <CODEX_HOME>/sessions/.
    return "codex" if "/sessions/" in (data.get("transcript_path") or "") else "claude"


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
    return registry.upsert(runtime, home, session_id, pid, data.get("cwd") or os.getcwd(),
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
            "from": parsed.header.get("from") or parsed.attrs.get("from-name"),
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
    runtime = detect_runtime(data)
    me = register(data, runtime)
    if data.get("hook_event_name") != "UserPromptSubmit":
        return None
    parsed = envelope.parse(data.get("prompt") or "")
    if not parsed.peer:
        return None                      # ordinary human input: say nothing
    cfg = config.load()
    msg_id = parsed.header.get("id")
    decision, reason = "pass", ""

    if not parsed.header:
        decision, reason = ("block", "peer message without an xsm header") \
            if cfg.get("strict_peers", True) else ("pass", "unheadered peer message allowed")
    elif not me:
        # Without knowing which session we are, scope cannot be checked at all.
        # Passing here would turn an unidentifiable session into an open door.
        decision, reason = "block", "cannot identify this session, so scope was not checked"
    else:
        sender = _sender_record(parsed)
        if sender is None:
            decision, reason = "block", "sender %r is not registered" % parsed.header.get("from")
        else:
            scope, why = config.scope_for(sender, me, cfg)
            if not scope:
                decision, reason = "block", "out of scope: %s" % why
            elif scope != parsed.header.get("scope"):
                decision, reason = "block", "scope changed since the message was sent"

    if decision == "block":
        stored = hold(runtime, reason, data, me, parsed)
        paths.append_jsonl("decisions.jsonl", {
            "event": "UserPromptSubmit", "runtime": runtime, "decision": "block",
            "reason": reason, "held": stored, "id": msg_id,
            "receiver": me and me.get("name"), "from": parsed.header.get("from")})
        if msg_id:
            ledger.receipt(msg_id, "held" if stored else "blocked", me, reason)
        if not stored:
            # Refusing without a copy would destroy the message; warn instead.
            return allow_with_context(runtime, envelope.sender_context(parsed) +
                                      "\n[xsm] This message failed a check (%s) but could not "
                                      "be stored, so it was delivered with this warning." % reason)
        return block(runtime, reason + " (kept: xsm held list)")

    paths.append_jsonl("decisions.jsonl", {
        "event": "UserPromptSubmit", "runtime": runtime, "decision": "pass", "reason": reason,
        "id": msg_id, "receiver": me and me.get("name"), "from": parsed.header.get("from")})
    if msg_id:
        ledger.receipt(msg_id, "delivered", me)
    return allow_with_context(runtime, envelope.sender_context(parsed))


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
        if looks_like_peer:
            print(json.dumps({
                "decision": "block",
                "reason": "xsm: internal error while checking this peer message; "
                          "it was not delivered. Run `xsm doctor`.",
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "suppressOriginalPrompt": True}}))
        return 0
