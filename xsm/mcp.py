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
`codex` ancestor is the session, and its registry record is the author.
"""
from __future__ import annotations

import json
import os
import sys

from . import channel, identity, registry

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
         "dir": {"type": "string", "description": "the worker's folder; default this session's"}},
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
         "wait": {"type": "number", "default": 15}}, "required": ["target", "text"]}},
    {"name": "xsm_inbox",
     "description": ("Codex sessions: read messages other sessions sent you that are still "
                     "waiting. Codex takes them only between turns; while you are working, call "
                     "this whenever an xsm result says messages are waiting, and before you wait "
                     "on a peer. A Claude session never needs it: its messages arrive on their "
                     "own."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "xsm_join",
     "description": ("Ask your user to let this session's folder join (or leave) a named xsm "
                     "project, so sessions in other repositories that also joined it can talk "
                     "with this one. Joining is their decision; this shows them a form."),
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "leave": {"type": "boolean", "default": False},
         "reason": {"type": "string"}}, "required": ["project"]}},
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


class Server:
    def __init__(self, inp=sys.stdin, out=sys.stdout):
        self.inp, self.out = inp, out
        self.client_caps = {}
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

    # -- the session ---------------------------------------------------------------
    def session(self) -> dict | None:
        pid = identity.ancestor_pid({"claude", "codex"})
        if not pid:
            return None
        rows = [r for r in registry.records() if r.get("pid") == pid and r.get("state") == "live"]
        return max(rows, key=lambda r: r.get("updated", 0)) if rows else None

    # -- tools --------------------------------------------------------------------
    def call(self, name: str, args: dict) -> str:
        me = self.session()
        if not me:
            raise channel.ChannelError("this session is not registered with xsm; is the xsm hook "
                                       "installed in its home?")
        where = channel.resolve(me.get("cwd") or os.getcwd(), args.get("channel"))
        author = {"kind": "agent", "name": me.get("name"), "alias": me.get("alias"),
                  "ref": me.get("ref"), "runtime": me.get("runtime")}
        text = self._call(name, args, me, where, author)
        if me.get("runtime") == "codex" and name != "xsm_inbox":
            from . import inbox
            waiting = inbox.notice(me.get("session_id"))
            if waiting:
                text += "\n\n" + waiting
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
        if name == "xsm_send":
            from . import send as send_mod
            r = send_mod.send(args.get("target") or "", args.get("text") or "", sender=me,
                              kind=args.get("kind") or "note", reply_to=args.get("reply_to"),
                              wait=float(args.get("wait") or 0))
            return "%s: %s" % (r.status, r.reason or "")
        if name == "xsm_doc_endorse":
            return self.endorse(me, args)
        if name == "xsm_inbox":
            from . import receive
            return "\n\n----\n\n".join(receive.take_inbox(me)) or "(no messages waiting)"
        raise channel.ChannelError("unknown tool %s" % name)

    def decide(self, where: tuple, me: dict, args: dict) -> str:
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot ask its user (no elicitation "
                                       "support); a person can post the decision with "
                                       "`xsm post --tag decision` in a terminal")
        question = (args.get("question") or "").strip()
        options = [o for o in (args.get("options") or []) if isinstance(o, str) and o.strip()]
        field = {"type": "string", "title": "Answer"}
        if options:
            field["enum"] = options
        reply = self.ask_client("elicitation/create", {
            "message": question,
            "requestedSchema": {"type": "object", "properties": {"answer": field},
                                "required": ["answer"]}})
        result = reply.get("result") or {}
        if result.get("action") != "accept":
            return "your user did not answer (%s); nothing was recorded" % (
                result.get("action") or (reply.get("error") or {}).get("message") or "no answer")
        answer = str((result.get("content") or {}).get("answer", "")).strip()
        if not answer:
            return "your user gave an empty answer; nothing was recorded"
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
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot ask its user; they can run "
                                       "`xsm doc add … --tag endorsed` in a terminal")
        path = args.get("doc") or ""
        if not os.path.isabs(path):
            path = os.path.join(me.get("cwd") or os.getcwd(), path)
        node = next((n for n in doc.read(path) if n["id"] == args.get("node")), None)
        if not node:
            raise channel.ChannelError("no node %s in %s" % (args.get("node"), path))
        preview = node["body"] if len(node["body"]) < 1500 else node["body"][:1500] + " …"
        reply = self.ask_client("elicitation/create", {
            "message": "Endorse this node of %s as the document's text?\n[%s] by %s\n\n%s" % (
                os.path.basename(path), ", ".join(node["tags"]), node["author"], preview),
            "requestedSchema": {"type": "object", "properties": {"answer": {
                "type": "string", "title": "Endorse", "enum": ["endorse", "not now"]}},
                "required": ["answer"]}})
        result = reply.get("result") or {}
        if result.get("action") != "accept" or (result.get("content") or {}).get("answer") != "endorse":
            return "your user did not endorse it; nothing was added"
        author = {"kind": "human", "name": os.environ.get("USER") or "person",
                  "via": "mcp-elicitation"}
        new = doc.add(path, author, node["body"], ["endorsed"], [node["id"]],
                      approved="asked by %s" % me.get("ref"))
        return "endorsed: node %s now carries %s; run `xsm doc render %s`" % (
            new["id"], node["id"], args.get("doc"))

    def join(self, me: dict, args: dict) -> str:
        from . import config
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot ask its user; they can run "
                                       "`xsm join` in a terminal")
        project, leaving = (args.get("project") or "").strip(), bool(args.get("leave"))
        root = config.project_root(me.get("cwd") or os.getcwd())
        verb = "leave" if leaving else "join"
        question = ("%s@%s asks to let %s %s the xsm project %r.%s\nAllow it?" % (
            me.get("name"), me.get("alias"), root, verb, project,
            ("\nReason: " + args["reason"]) if args.get("reason") else ""))
        reply = self.ask_client("elicitation/create", {"message": question, "requestedSchema": {
            "type": "object", "properties": {"answer": {"type": "string", "title": "Permission",
                                                        "enum": ["allow", "deny"]}},
            "required": ["answer"]}})
        result = reply.get("result") or {}
        if result.get("action") != "accept" or (result.get("content") or {}).get("answer") != "allow":
            return "your user did not allow it; the folder's projects are unchanged"
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

    def approve(self, me: dict, args: dict) -> str:
        from . import workers
        mine = [r for r in workers.approvals()
                if (workers.load(r.get("worker") or "") or {}).get("parent_ref") == me.get("ref")]
        req = next((r for r in mine if r["id"] == args.get("id")), None) if args.get("id") \
            else (mine[0] if mine else None)
        if not req:
            return "no waiting request from your workers" + (
                " with id %s" % args["id"] if args.get("id") else "")
        if "elicitation" not in (self.client_caps or {}):
            raise channel.ChannelError("this client cannot ask its user; they can answer with "
                                       "`xsm approve %s` in a terminal" % req["id"])
        allow, deny = "allow", "deny"
        reply = self.ask_client("elicitation/create", {
            "message": "Worker %s is waiting for your permission:\n%s\nAllow it?"
                       % (req["worker"], req["summary"]),
            "requestedSchema": {"type": "object", "properties": {"answer": {
                "type": "string", "title": "Permission", "enum": [allow, deny]}},
                "required": ["answer"]}})
        result = reply.get("result") or {}
        answer = (result.get("content") or {}).get("answer") if result.get("action") == "accept" \
            else None
        if answer not in (allow, deny):
            return ("your user did not answer (%s); the request is still waiting — ask again "
                    "or tell them it is blocking the worker" % (result.get("action") or "no answer"))
        workers.answer_asked(req["id"], answer == allow, me.get("ref"),
                             None if answer == allow else "your user said no")
        return "%s: worker %s's request [%s] %s" % (
            "allowed" if answer == allow else "denied", req["worker"], req["id"], req["summary"])

    def grant(self, where: tuple, me: dict, args: dict) -> str:
        from . import workers
        options = sorted(set(o for o in (args.get("options") or []) if o in workers.DANGEROUS))
        if not options:
            raise channel.ChannelError("options must name full_access and/or trust_hooks")
        runtime = args.get("runtime")
        cwd = os.path.realpath(os.path.expanduser(args.get("dir") or me.get("cwd") or os.getcwd()))
        reason = (args.get("reason") or "").strip() or "(no reason given)"
        words = {"remote": "PAIRING with another machine over SSH: sessions there in the paired "
                           "project can message this project",
                 "outside_scope": "a folder OUTSIDE this session's project: the worker will be "
                                  "able to talk to the sessions there",
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
        if reply is None:
            raise channel.ChannelError("this client cannot ask its user; a person can run the "
                                       "spawn in a terminal instead")
        result = reply.get("result") or {}
        answer = (result.get("content") or {}).get("answer") if result.get("action") == "accept" \
            else None
        author = {"kind": "human", "name": os.environ.get("USER") or "person",
                  "via": "mcp-elicitation", "asked_by": me.get("ref"), "runtime": me.get("runtime")}
        verdict = "allowed" if answer == allow else "refused"
        channel.post(where, author, "worker permission %s: %s %s in %s" % (
            verdict, runtime, "+".join(options), cwd), "decision",
            approved={"question": question, "answer": answer or result.get("action") or "none",
                      "options": [deny, allow]})
        if answer != allow:
            return "your user did not allow it (%s); do not start that worker" % (
                answer or result.get("action") or "no answer")
        g = workers.create_grant(me.get("ref"), runtime, cwd, options, answer)
        return ("granted %s: xsm spawn %s --dir %s %s --grant %s   (one use, %d minutes)" % (
            g["id"], runtime, cwd, " ".join("--" + o.replace("_", "-") for o in options), g["id"],
            workers.GRANT_TTL // 60))

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
                self.send({"id": mid, "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xsm", "version": "1"}}})
            elif method == "tools/list":
                self.send({"id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    text, error = self.call(params.get("name"), params.get("arguments") or {}), False
                except (channel.ChannelError, EOFError) as exc:
                    text, error = str(exc), True
                self.send({"id": mid, "result": {"content": [{"type": "text", "text": text}],
                                                 "isError": error}})
            elif method == "ping" and mid is not None:
                self.send({"id": mid, "result": {}})
            elif mid is not None and method:
                self.send({"id": mid, "error": {"code": -32601, "message": "not supported"}})


def main() -> int:
    return Server().serve()
