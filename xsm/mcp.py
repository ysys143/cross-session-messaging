"""The xsm MCP server: channel tools for a session, over stdio.

Started by the session (Claude Code or Codex) and ending with it, so it is not
a resident process (ADR-0003). It exists for two reasons (ADR-0005):

- A decision recorded from a session must be the person's. `xsm_decide` asks
  them through MCP elicitation, and their answer comes back from the client to
  this server without passing through the model (measured on both runtimes,
  2026-09-22). The answer and the question are stored with the decision.
- A sandboxed Codex cannot write `~/.xsm` from its shell, but the MCP servers it
  starts run outside the sandbox (measured), so posting and reading go through
  here too.

The session is identified by this process's ancestry: the nearest `claude` or
`codex` ancestor is the session, and its registry record is the author. Codex
threads of one CODEX_HOME share one ancestor (the app-server daemon), so a
Codex call names its thread in `params._meta.threadId` and that decides; with
several live threads and no such id, the call is refused (Server.session).
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time

from . import channel, consent, identity, paths, registry

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "xsm_post",
     "description": ("Post to this project's xsm channel, the record people and sessions share. "
                     "Tags: note, question, proposal, result, hypothesis. A decision cannot be "
                     "posted: ask your user with xsm_decide."),
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"},
         "tag": {"type": "string", "enum": [t for t in channel.TAGS if t != "decision"]},
         "reply_to": {"type": "string", "description": "id of the post this answers"},
         "channel": {"type": "string", "description": "a named project; default: this project"}},
         "required": ["text"]}},
    {"name": "xsm_channel",
     "description": "Read this project's xsm channel: threads, or only posts with one tag.",
     "inputSchema": {"type": "object", "properties": {
         "tag": {"type": "string", "enum": list(channel.TAGS)},
         "limit": {"type": "integer", "default": 30},
         "channel": {"type": "string"}}}},
    {"name": "xsm_grant",
     "description": ("Ask your user for explicit permission to start a worker with dangerous "
                     "options: full_access (no sandbox, no approvals) and/or trust_hooks (Codex: "
                     "run hooks without trust review). Returns a one-time grant id for "
                     "`xsm spawn ... --grant <id>`, valid 10 minutes, for this session, runtime "
                     "and folder only. Say plainly why the worker needs it."),
     "inputSchema": {"type": "object", "properties": {
         "runtime": {"type": "string", "description": "claude or codex for a worker; "
                     "remote:<host> for a remote pairing"},
         "options": {"type": "array", "items": {"type": "string",
                                                 "enum": ["full_access", "trust_hooks",
                                                          "outside_scope", "remote"]}},
         "reason": {"type": "string"},
         "dir": {"type": "string", "description": "the worker's folder; default this session's "
                 "(a remote pairing is always for this session's folder)"}},
         "required": ["runtime", "options", "reason"]}},
    {"name": "xsm_send",
     "description": ("Send a message to another session through xsm, the same as `xsm send`. Use "
                     "it when your shell cannot run xsm (a sandboxed Codex cannot: xsm needs the "
                     "process table, and a remote needs the network). Targets: name, "
                     "name@home, ref:xxxxxx, and …@<paired machine>."),
     "inputSchema": {"type": "object", "properties": {
         "target": {"type": "string"}, "text": {"type": "string"},
         "kind": {"type": "string", "enum": ["note", "task", "reply"], "default": "note"},
         "reply_to": {"type": "string"},
         "outcome": {"type": "string", "enum": ["succeeded", "failed"],
                     "description": "only on a reply that closes a task you were given: how it "
                                    "ended. Say a failure here, not only in the words — the flag "
                                    "is what the sender can act on."},
         "resend": {"type": "string",
                    "description": "the id of an earlier send that ended unknown or error: sends "
                                   "that same message again under its id, which the receiver "
                                   "drops if it already has it. Same target, kind and text only."},
         "held": {"type": "string",
                  "description": "the id of a message a refused send kept (out of scope: it "
                                 "waits for your user's yes to connect, then goes with it): "
                                 "sends it as it was kept, so target and text are not needed."},
         "wait": {"type": "number", "default": 15}}, "required": []}},
    {"name": "xsm_inbox",
     "description": ("Codex sessions: read messages other sessions sent you that are still "
                     "waiting. Codex takes them only between turns; while you are working, call "
                     "this whenever an xsm result says messages are waiting, and before you wait "
                     "on a peer. A Claude session never needs it: its messages arrive on their "
                     "own. `wait` blocks until one arrives instead of sleeping in a loop — a "
                     "loop that never ends your turn is why six messages once went unread."),
     "inputSchema": {"type": "object", "properties": {
         "wait": {"type": "number", "default": 0,
                  "description": "seconds to block until a message arrives (max 60 here; the "
                                 "shell `xsm inbox --wait` allows longer)"}}}},
    {"name": "xsm_link",
     "description": ("Link this session's project folder with another folder, so the sessions "
                     "of both talk both ways until unlinked; one side is enough. The normal way "
                     "to connect another folder. When your user typed `/xsm link <folder>` "
                     "(Codex: `$xsm link <folder>`), that is their consent and no form is shown; "
                     "otherwise this asks them with a form. drop=true unlinks without asking."),
     "inputSchema": {"type": "object", "properties": {
         "dir": {"type": "string", "description": "the other folder, e.g. ~/src/other-repo"},
         "drop": {"type": "boolean", "default": False},
         "reason": {"type": "string"}}, "required": ["dir"]}},
    {"name": "xsm_join",
     "description": ("Ask your user to let this session's folder join (or leave) a named xsm "
                     "project, so sessions in other repositories that also joined it can talk "
                     "with this one. Joining is their decision; this shows them a form."),
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "leave": {"type": "boolean", "default": False},
         "reason": {"type": "string"}}, "required": ["project"]}},
    {"name": "xsm_reach",
     "description": ("Ask your user to let this session talk with the sessions of one other "
                     "folder (both ways, while this session runs), when a send is refused as out "
                     "of scope. Narrower than joining a project: nothing else changes. This "
                     "shows them a form; drop=true takes it back without asking."),
     "inputSchema": {"type": "object", "properties": {
         "dir": {"type": "string", "description": "the other folder, e.g. ~/src/other-repo"},
         "drop": {"type": "boolean", "default": False},
         "reason": {"type": "string"}}, "required": ["dir"]}},
    {"name": "xsm_doc_endorse",
     "description": ("Ask your user to endorse a node of a shared document (xsm doc), making it "
                     "the document's canonical text when rendered. They see the node and decide."),
     "inputSchema": {"type": "object", "properties": {
         "doc": {"type": "string", "description": "path of the document, e.g. docs/x.md"},
         "node": {"type": "string"}}, "required": ["doc", "node"]}},
    {"name": "xsm_approve",
     "description": ("Show your user a permission request from a worker you started, and pass "
                     "on their answer. Call it as soon as you are told a worker is waiting; the "
                     "worker is blocked until it is answered. Without an id, takes the oldest "
                     "waiting request of your workers."),
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "xsm_decide",
     "description": ("Ask your user to decide, and record their answer as a decision in the "
                     "channel. Your user sees the question and picks an option or writes an "
                     "answer; you do not choose for them. Use it when a choice should be on "
                     "record as the user's."),
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"},
                     "description": "choices to offer; omit for a free-text answer"},
         "summary": {"type": "string", "description": "one line on what is being decided"},
         "reply_to": {"type": "string"},
         "channel": {"type": "string"}},
         "required": ["question"]}},
]


# The clientInfo.name Codex sends in initialize (codex-rs/codex-mcp/src/rmcp_client.rs,
# 0.158). Only Codex is known to decline forms without showing them.
CODEX_CLIENT = "codex-mcp-client"
ACTIONS = {"accept": "accepted", "decline": "declined", "cancel": "dismissed"}
# The MCP spec's three results of a form: accept (the person submitted it),
# decline (they explicitly said no) and cancel (they dismissed it without
# choosing). A client that shows its forms reports a no as a decline, so xsm
# reads it as the person's no and the agent does not ask again (2026-10-01).
DECLINED = "declined by your user"


def person_answer(reply, allowed=None, client=None) -> tuple:
    """The answer a person chose in a form, or (None, why not).

    Every form tool acts only on this. It takes what the client reports: an
    accepted form whose `answer` is a non-empty string (one of `allowed`, when
    the form offers choices), unless `_meta.approvals_reviewer` says someone
    other than the user answered it. Anything malformed — an `action` that is
    not one of the three, a `_meta` or `content` that is not an object — is no
    answer, never an exception: an unhashable `action` once raised past serve()
    and took the server down, and a list `_meta` was read as no meta at all, so
    all six tools acted (second review, 2026-09-28). A client that answers
    forms by itself — a Claude Code Elicitation hook the user configured, say —
    reports it as the user's, and xsm cannot tell; that is outside what it can
    detect.

    Codex (0.158): its auto-review does not look at xsm's forms (it reviews only
    elicitations whose `_meta` asks for an approval), but approval_policy
    "never" (or a granular policy with MCP elicitations off) declines them
    without showing them, unless full-access form input is on for the thread.
    That bare decline is also what a person pressing Decline sends, so it is
    reported as either. A bare "your user did not allow it" read as a refusal
    nobody had given (a tester's report, 2026-09-28). `client` is the
    clientInfo.name from initialize, so the wording names Codex only when it
    is Codex. From a client known to show its forms, a decline is the person's
    no (DECLINED); a client that did not say who it is stays unsure. A cancel
    is no answer: the form was dismissed."""
    if not isinstance(reply, dict):
        return None, "the client sent no reply"
    error = reply.get("error")
    if error is not None:
        return None, "the client returned an error: %s" % (
            (error.get("message") if isinstance(error, dict) else None) or error)
    result = reply.get("result")
    if not isinstance(result, dict):
        return None, "the client sent no result"
    action = result.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        return None, "the client answered %r, which is not a form answer" % (action,)
    meta = result.get("_meta")
    if meta is not None and not isinstance(meta, dict):
        return None, "the client sent a malformed _meta, so who answered is unknown"
    reviewer = (meta or {}).get("approvals_reviewer")
    if reviewer not in (None, "user"):
        note = next((v for k, v in meta.items() if k != "approvals_reviewer" and isinstance(v, str)),
                    "")
        return None, ("%s by the client's automatic reviewer, not by your user%s; xsm counts only "
                      "a person's answer" % (ACTIONS[action], (": " + note) if note else ""))
    if action == "decline":
        if client == CODEX_CLIENT:
            return None, ("declined — by your user, or by Codex without showing the form "
                          "(approval_policy \"never\")")
        if client:
            return None, DECLINED
        return None, ("declined — by your user, or by the client without showing the form "
                      "(Codex does this under approval_policy \"never\")")
    if action == "cancel":
        return None, "the form was dismissed"
    content = result.get("content")
    answer = content.get("answer") if isinstance(content, dict) else None
    # Stripped, and compared with choices that were offered stripped (decide).
    answer = answer.strip() if isinstance(answer, str) else ""
    if not answer:
        return None, "the form came back with no choice in it, so nobody picked one"
    if allowed and answer not in allowed:
        return None, "the form came back with %r, which is not one of its choices" % answer
    return answer, "they chose %r" % answer


def unanswered(why: str) -> str:
    """The head of a form tool's result when no choice came back. A decline
    (DECLINED) is the person's no and says so; the agent must not ask again.
    Anything else is no answer, and the agent may ask in plain words."""
    if why == DECLINED:
        return "your user declined (they said no; do not ask again or work around it)"
    return "your user did not answer (%s)" % why


def who_answered(reply, client=None) -> str:
    """Who answered a form, in words for the tool's result."""
    return person_answer(reply, client=client)[1]


class Server:
    def __init__(self, inp=sys.stdin, out=sys.stdout):
        self.inp, self.out = inp, out
        self.client_caps = {}
        self.client_name = None         # clientInfo.name from initialize
        self.call_thread = None         # the thread id the current tools/call names, if it does
        self.next_id = 0

    # -- transport ----------------------------------------------------------------
    def send(self, obj: dict) -> None:
        obj.setdefault("jsonrpc", "2.0")
        self.out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.out.flush()

    def read(self) -> dict | None:
        line = self.inp.readline()
        return json.loads(line) if line else None

    def ask_client(self, method: str, params: dict) -> dict:
        """A request to the client, answered before anything else continues."""
        self.next_id += 1
        rid = "xsm-%d" % self.next_id
        self.send({"id": rid, "method": method, "params": params})
        while True:
            msg = self.read()
            if msg is None:
                raise EOFError("client went away")
            if msg.get("id") == rid and "method" not in msg:
                return msg
            if msg.get("method") == "ping" and "id" in msg:
                self.send({"id": msg["id"], "result": {}})

    def answer(self, reply, allowed) -> tuple:
        return person_answer(reply, allowed, self.client_name)

    # -- the session ---------------------------------------------------------------
    def session(self) -> dict | None:
        pid = identity.ancestor_pid({"claude", "codex"})
        if not pid:
            return None
        rows = [r for r in registry.records() if r.get("pid") == pid and r.get("state") == "live"]
        codex = [r for r in rows if r.get("runtime") == "codex"]
        if self.call_thread and (codex or not rows):
            # Codex 0.158 names the calling thread in every tools/call
            # (`params._meta.threadId`; measured 2026-09-29 with two TUIs on one
            # daemon: one MCP server process per thread, all children of the
            # daemon, so the pid alone names all of them).
            for r in codex:
                if r.get("session_id") == self.call_thread:
                    return r
            # No live record: a sub-agent's thread, or one whose hook has not
            # run. Connect it rather than refuse (user decision, 2026-09-30:
            # "prefer easy connection; accidental connection failures must not
            # happen"). Adoption needs the same consent as `xsm list`'s: xsm
            # installed in that home with trusted hooks; without it, fall
            # through to the most recent record below.
            adopted = registry.adopt_codex_thread(
                self.call_thread, pid, self.codex_homes(codex), mcp_pid=os.getpid())
            if adopted:
                return adopted
        # No thread id (old Codex) or nothing adoptable: the newest live record,
        # as before 0840ee3 (user decision, 2026-09-30: a possibly wrong
        # neighbour is better than a refusal).
        if rows:
            return max(rows, key=lambda r: r.get("updated", 0))
        # A Claude session whose hooks never ran (started before xsm, or before
        # its plugin was enabled) has no record at all: its own process is this
        # server's parent, and Claude's record of it names the session (audit,
        # 2026-10-01: such a session could not use any xsm tool). By pid, never by
        # an environment variable: a Codex started from a Claude shell inherits
        # that session's id.
        return registry.adopt_claude_process(pid)

    @staticmethod
    def codex_homes(codex_rows: list) -> list:
        """Candidate CODEX_HOMEs for a thread with no record, likeliest first:
        this server's own env, the homes of records sharing the daemon, the
        declared Codex homes, then the default. The caller keeps the first one
        whose state DB knows the thread."""
        return registry.codex_homes(codex_rows)

    # -- tools --------------------------------------------------------------------
    def call(self, name: str, args: dict) -> str:
        me = self.session()
        if not me:
            raise channel.ChannelError(registry.unregistered_reason())
        where = channel.resolve(me.get("cwd") or os.getcwd(), args.get("channel"))
        author = {"kind": "agent", "name": me.get("name"), "alias": me.get("alias"),
                  "ref": me.get("ref"), "runtime": me.get("runtime")}
        text = self._call(name, args, me, where, author)
        if me.get("runtime") == "codex" and name != "xsm_inbox":
            from . import inbox
            waiting = inbox.notice(me.get("session_id"))
            if waiting:
                text += "\n\n" + waiting
        from . import bounce
        held = bounce.notice(me.get("session_id"))     # issue #8
        if held:
            text += "\n\n" + held
        return text

    def _call(self, name: str, args: dict, me: dict, where: tuple, author: dict) -> str:
        if name == "xsm_post":
            rec = channel.post(where, author, args.get("text", ""), args.get("tag") or "note",
                               args.get("reply_to"))
            return "posted %s to %s" % (rec["id"], where[0])
        if name == "xsm_channel":
            text = channel.render(channel.read(where[1]), tag=args.get("tag"),
                                  limit=int(args.get("limit") or 30))
            return text or "(no posts in %s)" % where[0]
        if name == "xsm_decide":
            return self.decide(where, me, args)
        if name == "xsm_grant":
            return self.grant(where, me, args)
        if name == "xsm_approve":
            return self.approve(me, args)
        if name == "xsm_join":
            return self.join(me, args)
        if name == "xsm_reach":
            return self.reach(me, args)
        if name == "xsm_link":
            return self.link(me, args)
        if name == "xsm_send":
            from . import send as send_mod
            r = send_mod.send(args.get("target") or "", args.get("text") or "", sender=me,
                              kind=args.get("kind") or "note", reply_to=args.get("reply_to"),
                              wait=float(args.get("wait") or 0),
                              outcome=args.get("outcome") if args.get("kind") == "reply" else None,
                              msg_id=args.get("resend") or None,
                              resend=bool(args.get("resend")), held=args.get("held") or None)
            return "\n".join(list(r.notes) + ["%s: %s" % (r.status, r.reason or "")])
        if name == "xsm_doc_endorse":
            return self.endorse(me, args)
        if name == "xsm_inbox":
            from . import inbox, receive
            wait = float(args.get("wait") or 0)
            if wait > 0:
                # Shorter than the shell's cap: serve() reads one request at a
                # time, so a long block looks to the client like a dead server.
                inbox.wait_for(me.get("session_id"), min(wait, inbox.MCP_MAX_WAIT))
            return "\n\n----\n\n".join(receive.take_inbox(me)) or "(no messages waiting)"
        # A client that loaded its tool list from a newer xsm than this server
        # (the server reads TOOLS once, at its start) names a tool it lacks.
        raise channel.ChannelError(
            "unknown tool %s: this xsm server started before xsm was updated. Reconnect it (Claude "
            "Code: /mcp, then xsm) or start a new session; meanwhile run the same `xsm` command "
            "in the shell" % name)

    def decide(self, where: tuple, me: dict, args: dict) -> str:
        if "elicitation" not in (self.client_caps or {}):
            # The text to record is the agent's, so there is no ask to record
            # for it here: the command comes first (in_words).
            raise channel.ChannelError("this client cannot show a form (no elicitation "
                                       "support). " + self.in_words(
                                           me, 'xsm post --tag decision "<the decision to '
                                               'record>"'))
        question = (args.get("question") or "").strip()
        # Offered as they will be compared: stripped, no empties, no repeats.
        # " yes " used to be offered as is and then compared stripped, so the
        # person's choice was rejected (second review, 2026-09-28).
        options = []
        for o in args.get("options") or []:
            o = o.strip() if isinstance(o, str) else ""
            if o and o not in options:
                options.append(o)
        field = {"type": "string", "title": "Answer"}
        if options:
            field["enum"] = options
        reply = self.ask_client("elicitation/create", {
            "message": question,
            "requestedSchema": {"type": "object", "properties": {"answer": field},
                                "required": ["answer"]}})
        answer, why = self.answer(reply, options)
        if answer is None:
            return "%s; nothing was recorded%s" % (unanswered(why), "" if why == DECLINED else
                                                   ". " + self.in_words(
                                                       me, 'xsm post --tag decision "<the '
                                                           'decision to record>"'))
        summary = (args.get("summary") or "").strip()
        text = "%s: %s" % (summary, answer) if summary else "%s -> %s" % (question, answer)
        author = {"kind": "human", "name": os.environ.get("USER") or "person",
                  "via": "mcp-elicitation", "asked_by": me.get("ref"),
                  "runtime": me.get("runtime")}
        rec = channel.post(where, author, text, "decision", args.get("reply_to"),
                           approved={"question": question, "answer": answer,
                                     "options": options or None})
        return "recorded decision %s in %s: %s" % (rec["id"], where[0], answer)

    def endorse(self, me: dict, args: dict) -> str:
        from . import doc
        path = args.get("doc") or ""
        if not os.path.isabs(path):
            path = os.path.join(me.get("cwd") or os.getcwd(), path)
        node = next((n for n in doc.read(path) if n["id"] == args.get("node")), None)
        if not node:
            raise channel.ChannelError("no node %s in %s" % (args.get("node"), path))
        # The command is the node's own text, so the ask recorded for it here is
        # the one that command finds (consent.digest as the CLI keys it).
        command = "xsm doc add %s --tag endorsed --parent %s --text %s" % (
            shlex.quote(args.get("doc") or ""), shlex.quote(node["id"]), shlex.quote(node["body"]))
        key = ("endorse", consent.digest(args.get("doc"), node["body"], [node["id"]]))
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot show a form. "
                                       + self.in_words(me, command, key))
        preview = node["body"] if len(node["body"]) < 1500 else node["body"][:1500] + " …"
        reply = self.ask_client("elicitation/create", {
            "message": "Endorse this node of %s as the document's text?\n[%s] by %s\n\n%s" % (
                os.path.basename(path), ", ".join(node["tags"]), node["author"], preview),
            "requestedSchema": {"type": "object", "properties": {"answer": {
                "type": "string", "title": "Endorse", "enum": ["endorse", "not now"]}},
                "required": ["answer"]}})
        answer, why = self.answer(reply, ["endorse", "not now"])
        if answer != "endorse":
            return "your user did not endorse it (%s); nothing was added%s" % (
                why, ". " + self.in_words(me, command, key) if answer is None and why != DECLINED
                else "")
        author = {"kind": "human", "name": os.environ.get("USER") or "person",
                  "via": "mcp-elicitation"}
        new = doc.add(path, author, node["body"], ["endorsed"], [node["id"]],
                      approved="asked by %s" % me.get("ref"))
        return "endorsed: node %s now carries %s; run `xsm doc render %s`" % (
            new["id"], node["id"], args.get("doc"))

    def in_words(self, me: dict, command: str, key: tuple | None = None) -> str:
        """What to tell the agent when a form got no answer: ask in plain words,
        then run `command`. The ask is recorded here under `key` (the one that
        command computes), so their first "yes" has an ask to attach to; with no
        key, or no session xsm can keep a reply for, the command goes first
        (consent.in_words)."""
        return consent.in_words(command, bool(key) and consent.ask(me, *key), me.get("runtime"))

    def allowed(self, me: dict, question: str, command: str, key: tuple | None = None) -> tuple:
        """Ask the person allow/deny. Returns (True, None) on their allow, else
        (False, what to tell the agent). The way round a form that cannot show
        is the agent's own shell command, which asks for the user's reply
        (consent.py) — never the person typing it (user decision, 2026-10-01)."""
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot show a form. "
                                       + self.in_words(me, command, key))
        reply = self.ask_client("elicitation/create", {"message": question, "requestedSchema": {
            "type": "object", "properties": {"answer": {"type": "string", "title": "Permission",
                                                        "enum": ["allow", "deny"]}},
            "required": ["answer"]}})
        answer, why = self.answer(reply, ["allow", "deny"])
        if answer == "allow":
            return True, None
        if answer == "deny":
            return False, "your user declined: they chose 'deny'; do not work around it"
        if why == DECLINED:
            return False, unanswered(why)
        return False, "your user did not answer (%s). %s" % (why, self.in_words(me, command, key))

    def link(self, me: dict, args: dict) -> str:
        from . import config
        if not args.get("dir"):
            raise channel.ChannelError("dir: the folder to link with")
        here = me.get("cwd") or os.getcwd()
        root = config.project_root(here)
        # Relative to the session, like reach: the form, the command and the
        # link all name the same absolute root.
        other = config.project_root(os.path.join(here, os.path.expanduser(args["dir"])))
        if args.get("drop"):
            return ("unlinked %s and %s" % (root, other)) if config.drop_link(root, other) \
                else "%s and %s were not linked" % (root, other)
        # What can be checked is checked before the consent is used up: a
        # folder not created yet used it, and the retry asked with a form
        # (review, 2026-09-28).
        try:
            config.check_link(root, other)
        except ValueError as exc:
            raise channel.ChannelError(str(exc))
        if not consent.take(me, "link", other, here):
            # Run from this session's own folder; --dir is taken only from a
            # person at a terminal, so it would refuse the agent (issue #9).
            command = "xsm link %s" % shlex.quote(other)
            ok, refusal = self.allowed(
                me, "%s@%s asks to link %s with %s: the sessions of both folders talk, both ways, "
                "until unlinked.%s\nAllow it?" % (
                    me.get("name"), me.get("alias"), root, other,
                    ("\nReason: " + args["reason"]) if args.get("reason") else ""), command,
                ("link", other, here))
            if not ok:
                return refusal
        try:
            entry, added = config.add_link(root, other, os.environ.get("USER") or "person")
        except ValueError as exc:
            raise channel.ChannelError(str(exc))
        return ("%s: the sessions in %s and %s can talk, both ways, until unlinked; run `xsm list` "
                "to see them" % ("linked" if added else "already linked", root, other))

    def join(self, me: dict, args: dict) -> str:
        from . import config
        project, leaving = (args.get("project") or "").strip(), bool(args.get("leave"))
        root = config.project_root(me.get("cwd") or os.getcwd())
        verb = "leave" if leaving else "join"
        # What the agent runs instead, from this session's own folder: --dir is
        # taken only from a person at a terminal (issue #9).
        command = "xsm %s %s" % (verb, shlex.quote(project))
        if not leaving:
            try:
                config.check_join(project)      # before the consent is used up, as for link
            except ValueError as exc:
                raise channel.ChannelError(str(exc))
        if not consent.take(me, verb, project):       # the person typed /xsm join <project>
            ok, refusal = self.allowed(
                me, "%s@%s asks to let %s %s the xsm project %r.%s\nAllow it?" % (
                    me.get("name"), me.get("alias"), root, verb, project,
                    ("\nReason: " + args["reason"]) if args.get("reason") else ""), command,
                (verb, project))
            if not ok:
                return refusal + "; the folder's projects are unchanged"
        try:
            if leaving:
                changed = config.leave(project, me.get("cwd") or os.getcwd())
                return ("left %s" % project) if changed else "this folder was not in %s" % project
            scope, added = config.join(project, me.get("cwd") or os.getcwd())
        except ValueError as exc:
            raise channel.ChannelError(str(exc))
        others = [m["root"] for m in scope["members"] if os.path.realpath(m["root"]) != root]
        return "%s %s; other members: %s" % ("joined" if added else "already in", project,
                                             ", ".join(others) or "none yet")

    def reach(self, me: dict, args: dict) -> str:
        from . import config
        if not args.get("dir"):
            raise channel.ChannelError("dir: the folder to reach")
        # A relative dir is the session's, and the person's terminal may be
        # anywhere: the form, the command offered instead and the reach itself
        # all name the same absolute root. "../other" was quoted raw in the
        # command while the form showed the root (second review, 2026-09-28).
        folder = os.path.join(me.get("cwd") or os.getcwd(), os.path.expanduser(args["dir"]))
        root = config.project_root(folder)
        if args.get("drop"):
            return "dropped %d reach(es)" % config.drop_reach(me.get("ref"), root, session=me)
        command = "xsm reach %s --session %s" % (shlex.quote(root),
                                                 shlex.quote("ref:%s" % me.get("ref")))
        try:
            config.check_reach(root)            # before the consent is used up, as for link
        except ValueError as exc:
            raise channel.ChannelError(str(exc))
        if not consent.take(me, "reach", root):        # the person typed /xsm reach <dir>
            ok, refusal = self.allowed(
                me, "%s@%s (%s) asks to talk with the sessions in %s, both ways, until it ends.%s\n"
                "Allow it?" % (me.get("name"), me.get("alias"), me.get("ref"), root,
                               ("\nReason: " + args["reason"]) if args.get("reason") else ""),
                command, ("reach", root))
            if not ok:
                return refusal
        try:
            entry, added = config.add_reach(me.get("ref"), root,
                                            os.environ.get("USER") or "person", session=me)
        except ValueError as exc:
            raise channel.ChannelError(str(exc))
        return "%s: this session can now talk with the sessions in %s; run `xsm list` to see them" % (
            "allowed" if added else "already allowed", entry["root"])

    def approve(self, me: dict, args: dict) -> str:
        from . import workers
        mine = [r for r in workers.approvals()
                if (workers.load(r.get("worker") or "") or {}).get("parent_ref") == me.get("ref")]
        req = next((r for r in mine if r["id"] == args.get("id")), None) if args.get("id") \
            else (mine[0] if mine else None)
        if not req:
            return "no waiting request from your workers" + (
                " with id %s" % args["id"] if args.get("id") else "")
        command, key = "xsm approve %s" % shlex.quote(req["id"]), ("approve", req["id"])
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot show a form. "
                                       + self.in_words(me, command, key))
        allow, deny = "allow", "deny"
        reply = self.ask_client("elicitation/create", {
            "message": "Worker %s is waiting for your permission:\n%s\nAllow it?"
                       % (req["worker"], req["summary"]),
            "requestedSchema": {"type": "object", "properties": {"answer": {
                "type": "string", "title": "Permission", "enum": [allow, deny]}},
                "required": ["answer"]}})
        answer, why = self.answer(reply, [allow, deny])
        if why == DECLINED:
            answer = deny               # they said no to the form: the request is answered
        if answer is None:
            return ("your user did not answer (%s); the request is still waiting and the worker "
                    "is blocked until it is answered. %s" % (why, self.in_words(me, command, key)))
        workers.answer_asked(req["id"], answer == allow, me.get("ref"),
                             None if answer == allow else "your user said no")
        return "%s: worker %s's request [%s] %s" % (
            "allowed" if answer == allow else "denied", req["worker"], req["id"], req["summary"])

    def grant(self, where: tuple, me: dict, args: dict) -> str:
        from . import config, workers
        options = sorted(set(o for o in (args.get("options") or []) if o in workers.DANGEROUS))
        if not options:
            raise channel.ChannelError("options must name full_access and/or trust_hooks")
        runtime = args.get("runtime")
        remote = isinstance(runtime, str) and runtime.startswith("remote:")
        # `xsm remote add` takes no --dir: it pairs the session's own folder, and
        # its grant is keyed by that folder, so `dir` cannot move a pairing.
        cwd = os.path.realpath(os.path.expanduser(
            (None if remote else args.get("dir")) or me.get("cwd") or os.getcwd()))
        reason = (args.get("reason") or "").strip() or "(no reason given)"
        words = {"remote": "PAIRING with another machine over SSH: sessions there in the paired "
                           "project can message this project",
                 "outside_scope": "a folder OUTSIDE this session's project: the worker will be "
                                  "able to talk to the sessions there (and to this session, "
                                  "which starts it)",
                 "full_access": "FULL ACCESS: no sandbox and no approval prompts",
                 "trust_hooks": "hooks run WITHOUT Codex's trust review, including any in that "
                                "folder"}
        question = ("%s@%s wants to start a %s worker in %s with %s.\nReason: %s\n"
                    "Allow it once?" % (me.get("name"), me.get("alias"), runtime, cwd,
                                        "; ".join(words[o] for o in options), reason))
        allow, deny = "allow once", "deny"
        reply = self.ask_client("elicitation/create", {
            "message": question, "requestedSchema": {"type": "object", "properties": {
                "answer": {"type": "string", "title": "Permission", "enum": [deny, allow]}},
                "required": ["answer"]}}) if "elicitation" in (self.client_caps or {}) else None
        # What the agent runs without --grant, and the ask that command finds.
        # workers.spawn adds outside_scope itself for a folder outside this
        # session's scope; the other options are the flags it was given.
        flags = [o for o in options if o in ("full_access", "trust_hooks")]
        if remote:
            command, asked = "xsm remote add %s --project <project>" % shlex.quote(runtime[7:]), \
                ["remote"]
        else:
            command = " ".join(["xsm spawn", str(runtime), "--dir", shlex.quote(cwd)]
                               + ["--" + o.replace("_", "-") for o in flags])
            asked = flags + ([] if config.scope_for(me, {"cwd": cwd})[0] else ["outside_scope"])
        key = ("grant", workers.grant_target(runtime, cwd, asked))
        if reply is None:
            raise channel.ChannelError("this client cannot show a form. "
                                       + self.in_words(me, command, key))
        answer, why = self.answer(reply, [deny, allow])
        if answer is None:
            # Nobody chose, so there is no decision to put on record.
            if why == DECLINED:
                return "%s; nothing was granted — do not start that worker" % unanswered(why)
            return ("%s; nothing was granted, so do not start that worker without their yes. %s"
                    % (unanswered(why), self.in_words(me, command, key)))
        author = {"kind": "human", "name": os.environ.get("USER") or "person",
                  "via": "mcp-elicitation", "asked_by": me.get("ref"), "runtime": me.get("runtime")}
        verdict = "allowed" if answer == allow else "refused"
        channel.post(where, author, "worker permission %s: %s %s in %s" % (
            verdict, runtime, "+".join(options), cwd), "decision",
            approved={"question": question, "answer": answer, "options": [deny, allow]})
        if answer != allow:
            return "your user declined: they chose 'deny'; do not start that worker"
        g = workers.create_grant(me.get("ref"), runtime, cwd, options, answer)
        use = command if remote else "xsm spawn %s --dir %s %s" % (
            runtime, cwd, " ".join("--" + o.replace("_", "-") for o in options))
        return "granted %s: %s --grant %s   (one use, %d minutes)" % (
            g["id"], use, g["id"], workers.GRANT_TTL // 60)

    # -- loop -------------------------------------------------------------------------
    def serve(self) -> int:
        while True:
            msg = self.read()
            if msg is None:
                return 0
            method, mid = msg.get("method"), msg.get("id")
            if method == "initialize":
                params = msg.get("params") or {}
                self.client_caps = params.get("capabilities") or {}
                info = params.get("clientInfo")
                name = info.get("name") if isinstance(info, dict) else None
                self.client_name = name if isinstance(name, str) else None
                self.send({"id": mid, "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xsm", "version": "1"}}})
            elif method == "tools/list":
                self.send({"id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = msg.get("params") or {}
                meta = params.get("_meta")
                thread = meta.get("threadId") if isinstance(meta, dict) else None
                self.call_thread = thread if isinstance(thread, str) and thread else None
                try:
                    text, error = self.call(params.get("name"), params.get("arguments") or {}), False
                except (channel.ChannelError, EOFError) as exc:
                    # EOFError: the client went away mid-form; the next read
                    # ends the loop.
                    text, error = str(exc), True
                except Exception as exc:
                    # A tool's bug is that call's error, not the end of every
                    # tool in the session: an exception here once went past
                    # this loop and the server exited with no reply (second
                    # review, 2026-09-28).
                    text, error = "xsm failed: %s: %s" % (type(exc).__name__, exc), True
                    gone = removed_version_note(exc, params.get("name"))
                    if gone:
                        text += "\n" + gone
                self.send({"id": mid, "result": {"content": [{"type": "text", "text": text}],
                                                 "isError": error}})
            elif method == "ping" and mid is not None:
                self.send({"id": mid, "result": {}})
            elif mid is not None and method:
                self.send({"id": mid, "error": {"code": -32601, "message": "not supported"}})


# The shell command each consent-bearing tool stands for, for the note below.
SHELL_FORMS = {"xsm_join": "xsm join <project> (xsm leave <project>)",
               "xsm_link": "xsm link <folder>", "xsm_reach": "xsm reach <folder>",
               "xsm_send": "xsm send <target> --text \"...\""}


def removed_version_note(exc: BaseException, tool: str | None) -> str | None:
    """What to do when this server's own files are gone, or None.

    A plugin update deletes the old version folder while a session started on
    it is still open; a server that had not loaded a module yet then fails with
    ImportError and nothing said why (issue #6: `$xsm join` failed with
    "cannot import name 'workers'" from a deleted 0.4.7 folder). preload()
    prevents this for servers started on 0.4.9 or later; this tells the agent
    and its user the way out on the ones already running."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not isinstance(exc, ImportError) or os.path.isdir(os.path.join(here, "xsm")):
        return None
    shell = SHELL_FORMS.get(tool or "", "xsm <command>")
    return ("This session's xsm MCP server runs from %s, which a plugin update removed. Start a "
            "new session to load the installed xsm. Until then run the same thing in the shell: "
            "`%s`. If it says it needs your user's yes, ask them and run it again."
            % (here, shell))


def beacon_path() -> str:
    return paths.path(paths.MCP, "%d.json" % os.getpid())


def write_beacon() -> None:
    """This server's existence is the liveness signal for the Codex thread it
    serves (see identity._state_of): a Codex TUI launches MCP servers per
    thread and stops them when the thread closes. The hook that registers the
    thread reads the newest beacon under its Codex pid."""
    paths.write_json(beacon_path(), {"pid": os.getpid(), "ppid": os.getppid(),
                                     "lstart": identity.lstart(os.getpid()),
                                     "started": time.time(), "cwd": os.getcwd()})


def remove_beacon() -> None:
    try:
        os.unlink(beacon_path())
    except OSError:
        pass


def preload() -> list:
    """Import every xsm module now, while the files this server started from
    are still there. The tools import most of them on first use; a plugin
    update deletes the old version folder (Codex removes it on `codex plugin
    add`), and a session left open then failed its first xsm tool call with
    the modules gone (measured 2026-09-30: three open Codex sessions still ran
    0.4.7 servers from a deleted folder). With everything loaded, an open
    session keeps working on the version it started with until it restarts.
    Returns the modules that could not be imported."""
    import importlib
    import pkgutil
    from . import __path__ as package_path
    failed = []
    for info in pkgutil.iter_modules(package_path):
        if info.name == "__main__":
            continue
        try:
            importlib.import_module("%s.%s" % (__package__, info.name))
        except Exception:                # noqa: BLE001 - a module the server never uses must not stop it
            failed.append(info.name)
    return failed


def main() -> int:
    # The MCP server runs outside any sandbox (that is why it exists); a
    # worker's `env` setting must not make it refuse like a sandboxed shell.
    os.environ.pop("XSM_SANDBOXED", None)
    preload()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    write_beacon()
    try:
        return Server().serve()
    finally:
        remove_beacon()
