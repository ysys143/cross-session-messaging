"""Command surface. Every command is a one-shot process: nothing stays running.

Exit codes are part of the contract, so a caller can branch without parsing
prose: 0 ok, 2 refused (scope, liveness, ambiguity), 3 sent but unconfirmed,
4 usage or configuration error.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import sys
import time

from . import config, consent, envelope, housekeeping, inbox, install, ledger, paths, policy, receive, \
    registry, resolve, send, workers

OK, REFUSED, UNCONFIRMED, USAGE = 0, 2, 3, 4


def _here(args, me=None) -> str:
    """The folder a command speaks for: --dir, else the session running it
    (its own cwd, which the shell may have left), else this shell. A command
    that changes or writes on a folder's behalf takes --dir only from a person:
    otherwise one agent could speak for another project (ADR-0009)."""
    if getattr(args, "dir", None):
        if args.command in ("join", "leave", "post", "link") and not workers.human_terminal():
            raise SystemExit("refused: --dir speaks for another folder; only a person at a "
                             "terminal may use it with %s" % args.command)
        return os.path.realpath(os.path.expanduser(args.dir))
    me = me if me is not None else registry.me()
    return (me and me.get("cwd")) or os.getcwd()


def _in_this_project(row: dict, here: str, me: dict | None) -> bool:
    """Whether a session could be talked to from this folder: the same default
    project, or a named project this folder has joined."""
    probe = dict(me) if me else {}
    probe["cwd"] = here
    return bool(config.scope_for(probe, row)[0])


def _width(text: str) -> int:
    """Terminal columns: wide characters (Korean names, say) take two."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _rows(args, me=None) -> list:
    rows = registry.records()
    if getattr(args, "all", False):
        rows += registry.unregistered()
    if getattr(args, "runtime", None):
        rows = [r for r in rows if r.get("runtime") == args.runtime]
    if getattr(args, "home", None):
        rows = [r for r in rows if args.home in (r.get("alias"), r.get("home"))]
    if not getattr(args, "all", False):
        # A thread a Codex TUI just opened is what a caller is usually
        # looking for when a name resolves to nothing; -a already has it.
        rows += [r for r in registry.fresh_codex_threads() if not r.get("session_id")]
        rows = [r for r in rows if r.get("state") not in ("stale", "ended")]
        here = _here(args, me)
        rows = [r for r in rows if (me and r.get("ref") == me.get("ref"))
                or _in_this_project(r, here, me)]
    return rows


def cmd_list_clear(args, me) -> int:
    """Forget stopped sessions now instead of after the retention window.
    Live ones and ones whose state cannot be confirmed stay; a cleared session
    that is resumed registers again under the same ref."""
    here = _here(args, me)
    rows = [r for r in registry.records() if r.get("state") in ("ended", "stale")]
    if not args.all:
        rows = [r for r in rows if _in_this_project(r, here, me)]
    for r in rows:
        try:
            os.unlink(registry._record_path(r["runtime"], r["session_id"]))
        except OSError:
            pass
    if not rows:
        print("nothing to clear%s" % ("" if args.all else " in this project; -a clears every "
                                                          "project"))
        return OK
    print("cleared %d stopped session(s): %s" % (len(rows), ", ".join(
        "%s@%s [%s] %s" % (r.get("name"), r.get("alias"), r.get("ref"), r.get("state"))
        for r in rows)))
    return OK


def _compact_groups(rows: list, me: dict | None, here: str) -> list:
    home = os.path.expanduser("~")
    here = os.path.realpath(here)

    def label(cwd: str) -> str:
        real = os.path.realpath(cwd) if cwd else ""
        if real == here:
            return "%s (here)" % cwd.replace(home, "~", 1)
        if real.startswith(here + os.sep):
            return "./" + os.path.relpath(real, here)
        return (cwd or "?").replace(home, "~", 1)

    lines = []
    for folder, members in _folder_groups(rows, me, here, label):
        lines.append(folder)
        for r, flags in members:
            lines.append(" %s@%s [%s]%s" % (r.get("name"), r.get("alias"), r.get("ref") or "-",
                                            (" (" + ", ".join(flags) + ")") if flags else ""))
    return lines


def _table(rows: list, me: dict | None, here: str) -> list:
    """The same list as a Markdown table, for a TUI to draw: Claude Code and
    Codex both render tables with borders and aligned columns, so the model
    passes it through as it is instead of copying it into a code block."""
    home = os.path.expanduser("~")
    here = os.path.realpath(here)

    def label(cwd: str) -> str:
        real = os.path.realpath(cwd) if cwd else ""
        if real == here:
            return "here"
        if real.startswith(here + os.sep):
            return "./" + os.path.relpath(real, here)
        return (cwd or "?").replace(home, "~", 1)

    def cell(text: str) -> str:
        return (text or "").replace("|", "\\|")

    lines = ["| folder | session | runtime | ref | note |", "|---|---|---|---|---|"]
    for folder, members in _folder_groups(rows, me, here, label):
        for i, (r, flags) in enumerate(members):
            lines.append("| %s | %s | %s | `%s` | %s |" % (
                cell(folder) if i == 0 else "", cell(r.get("name") or ""),
                cell(r.get("alias") or ""), r.get("ref") or "-", cell(", ".join(flags))))
    return lines


def _folder_groups(rows: list, me: dict | None, here: str, label) -> list:
    """[(folder label, [(row, flags)])], this folder first, then folders under
    it, then the rest; within a folder, this session first."""
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get("cwd") or "", []).append(r)
    order = sorted(groups, key=lambda c: (os.path.realpath(c) != here if c else True,
                                          not (c and os.path.realpath(c).startswith(here)), c))
    out = []
    for cwd in order:
        members = []
        mine = sorted(groups[cwd], key=lambda r: not (me and r.get("ref") == me.get("ref")))
        for r in mine:
            flags = [f for f in (
                "you" if me and r.get("ref") == me.get("ref") else "",
                "" if r.get("registered") or r.get("fresh") else "unregistered",
                "out-of-scope" if me and not r.get("scope") and r.get("scope_reason") != "self" else "",
                "would-be-held" if r.get("native") == "hold" else "",
                "interrupted (Esc)" if r.get("interrupted") else "",
                r.get("state") if r.get("state") != "live" and not r.get("fresh") else "") if f]
            if r.get("why"):
                flags.append(r["why"])
            members.append((r, flags))
        out.append((label(cwd), members))
    return out


def cmd_list(args) -> int:
    registry.adopt_open_codex()
    me = registry.me()
    if getattr(args, "action", None) == "clear":
        return cmd_list_clear(args, me)
    rows = _rows(args, me)
    for row in rows:
        row["inbound"] = registry.inbound_setting(row.get("home", ""), row.get("cwd")) if \
            row.get("runtime") == "claude" else None
        row["native"] = send.native_forecast(me, row)[0] if me and row.get("ref") != me.get("ref") \
            else "n/a"
        scope, reason = (None, "self") if me and row.get("ref") == me.get("ref") \
            else config.scope_for(me, row) if me else (None, "no registered session here")
        row["scope"] = scope
        row["scope_reason"] = reason
        row["addressable"] = bool(row.get("registered") and row.get("state") == "live" and
                                  (scope or reason == "self"))
        # Informational: a queued message waits in such a thread unless the
        # daemon starts it (ADR-0002 appendix, 2026-09-28).
        row["interrupted"] = bool(row.get("runtime") == "codex" and row.get("state") == "live"
                                  and registry.codex_interrupted(row.get("home") or "",
                                                                 str(row.get("session_id") or "")))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return OK
    if not rows:
        if args.all:
            print("no sessions registered. Install the hooks first: xsm install --help")
        else:
            others = len([r for r in registry.records() if r.get("state") == "live"])
            print("no live sessions in this project (%s)%s" % (
                config.default_project(_here(args, me))[0],
                "; xsm list -a shows %d elsewhere" % others if others else ""))
        return OK
    if getattr(args, "table", False):
        print("\n".join(_table(rows, me, _here(args, me))))
        return OK
    if args.compact:
        # For a model to copy back verbatim: no alignment padding (every space
        # is a token), and only the flags that change whether a message would
        # arrive. Grouped by folder: a path on every line was most of the
        # output and wrapped each entry onto two or three lines in a narrow
        # Codex pane (2026-09-22). This folder first, a folder under it as ./….
        print("\n".join(_compact_groups(rows, me, _here(args, me))))
        return OK
    if not me:
        # Run from a plain terminal there is no "us" to be in scope with, and
        # saying "out-of-scope" about every row would read as a verdict.
        print("(this terminal is not a registered session, so scope is not shown)")
    width = max(_width("%s@%s" % (r.get("name"), r.get("alias"))) for r in rows)
    for r in rows:
        addr = "%s@%s" % (r.get("name"), r.get("alias"))
        flags = [] if r.get("registered") else ["unregistered"]
        if me and not r.get("scope") and r.get("scope_reason") != "self":
            flags.append("out-of-scope")
        if me and r.get("ref") == me.get("ref"):
            flags.append("you")
        if r.get("native") == "hold":
            flags.append("would be held")
        print("%s  [%s]  %-6s %-7s %-9s %s%s" % (
            addr + " " * (width - _width(addr)), r.get("ref"), r.get("runtime"), r.get("state"),
            r.get("permission_mode") or "mode?", r.get("cwd") or "",
            ("  (" + ", ".join(flags) + ")") if flags else ""))
    return OK


def _md(text) -> str:
    """One Markdown table cell: pipes escaped, newlines folded."""
    return str("" if text is None else text).replace("|", "\\|").replace("\n", " ")


def _short(text, width: int = 20) -> str:
    """Narrow cells keep a table a table: past the pane's width Codex draws
    each row as a stacked card, and ten messages became fifty lines."""
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= width else text[:width - 1] + "…"


def _md_table(headers: list, rows: list) -> list:
    """A Markdown table for a session's TUI to draw (Claude Code and Codex
    both render them). The display commands pass it through bare."""
    return (["| %s |" % " | ".join(headers), "|%s|" % "|".join("---" for _ in headers)] +
            ["| %s |" % " | ".join(_md(c) for c in row) for row in rows])


def cmd_who(args) -> int:
    # The same adoption `list` runs first. Without it `who` said "not
    # registered", then `list` adopted the thread and marked its row `you`,
    # then `who` found it: two answers to one question (review, 2026-09-28).
    registry.adopt_open_codex()
    me = registry.me()
    if not me:
        print(registry.unregistered_reason(), file=sys.stderr)
        return REFUSED
    if args.json:
        print(json.dumps(me, ensure_ascii=False, indent=1))
        return OK
    if getattr(args, "table", False):
        auto = me.get("name_source") and me["name_source"] != "user"
        rows = [("name", me["name"] + (" (%s: can change)" % me["name_source"] if auto else "")),
                ("address", "`%s@%s`, or stable: `ref:%s`" % (me["name"], me["alias"], me["ref"])),
                ("runtime", "%s, home %s" % (me["runtime"], _home_tilde(me.get("home") or ""))),
                ("folder", _home_tilde(me.get("cwd") or ""))]
        print("\n".join(_md_table(["this session", ""], rows)))
        return OK
    print("%s@%s [%s] %s %s" % (me["name"], me["alias"], me["ref"],
                                me["runtime"], me.get("cwd") or ""))
    if me.get("name_source") and me["name_source"] != "user":
        print("this name was %s, not chosen by your user; it can change. "
              "The stable address is ref:%s" % (me["name_source"], me["ref"]))
    return OK


def _home_tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path.startswith(home) else path


def _print_members(scope: dict, root: str) -> None:
    for m in scope.get("members", []):
        mine = os.path.realpath(m.get("root", "")) == root
        print("  %s%s" % (_home_tilde(m.get("root", "")), "  (this project)" if mine else ""))


# The reply of the person that let the last _person_or_refuse through, if
# that is what did; recorded as `by` on what it changed.
_verdict: str | None = None


def _by() -> str:
    """Who decided: the person's own reply when one was given, else the user."""
    return consent.by(_verdict)


def _person_or_refuse(what: str, mcp_tool: str, typed: tuple | None = None,
                      ask: tuple | None = None) -> str | None:
    """Changing who may talk to whom is the user's decision (ADR-0009). A
    person at this terminal decides; so does the command the person typed
    into the session running this (`typed` = (verb, target[, here]), see
    consent.py), and so does their reply when the agent asked them.

    Asking is the agent's job and the typing too (user decision, 2026-10-01:
    never send the person off to type a command). Without consent the request
    is recorded and the agent is told to ask in plain words. The person's
    latest message in that session is kept as the verdict, and running the
    same command again shows the agent that reply without acting on it (a
    question or a no is not a yes); the run after that goes ahead, once.
    `ask` = (verb, target) does the same for the decisions nobody types as an
    /xsm command (approve, unblock, …)."""
    global _verdict
    _verdict = None
    if workers.human_terminal():
        return None
    me = registry.me()
    if typed and consent.take(me, *typed):
        return None
    key = typed or ask
    if not key:
        return ("%s is your user's decision: ask them in plain words, then run this again"
                % what)
    verdict, go, kept = consent.take_or_request(me, *key)
    if verdict is not None and not go:
        return consent.shown_refusal(verdict, what, kept)
    if verdict is not None:
        _verdict = verdict
        print(consent.approved(key[0], key[1], verdict, kept, (me or {}).get("name")))
        return None
    if not kept:
        return consent.cannot_keep(what, mcp_tool)
    tool = " (The %s MCP tool asks with a form instead.)" % mcp_tool \
        if mcp_tool and mcp_tool != "no" else ""
    return consent.asks(what, tool, (me or {}).get("runtime"))


def cmd_join(args) -> int:
    try:
        config.check_join(args.project)         # before the typed consent is used up
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return USAGE
    here = _here(args)                          # refuses an agent's --dir, before the reply is used
    why = _person_or_refuse("joining a project", "xsm_join", ("join", args.project))
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
    try:
        scope, added = config.join(args.project, here)
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return USAGE
    root = config.project_root(here)
    print("%s project %s: %s" % ("joined" if added else "already in", args.project,
                                 _home_tilde(root)))
    print("this folder is in: %s" % ", ".join(_memberships(here)))
    print("members:")
    _print_members(scope, root)
    others = [m for m in scope["members"] if os.path.realpath(m["root"]) != root]
    if not others:
        print("no other project has joined %s yet. A session there asks its user and runs: "
              "xsm join %s" % (args.project, args.project))
        return OK
    reachable = [r for r in registry.records()
                 if r.get("state") == "live" and config.project_root(r.get("cwd") or "/") != root
                 and config.scope_for({"cwd": here}, r)[0] == args.project]
    if reachable:
        print("sessions in the other projects you can now reach:")
        for r in reachable:
            print("  %s@%s [%s] %s" % (r.get("name"), r.get("alias"), r.get("ref"),
                                       _home_tilde(r.get("cwd") or "")))
    return OK


def cmd_leave(args) -> int:
    here = _here(args)                          # as in join
    why = _person_or_refuse("leaving a project", "xsm_join (with leave)", ("leave", args.project))
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
    if config.leave(args.project, here):
        print("left project %s: %s" % (args.project, _home_tilde(config.project_root(here))))
        return OK
    print("this project is not in %s" % args.project, file=sys.stderr)
    return REFUSED


def _memberships(here: str) -> list:
    """Every project a session started in `here` belongs to: the default one
    first, then the named ones its folder has joined."""
    default, _ = config.default_project(here)
    root = config.project_root(here)
    named = [s.get("id") for s in config.projects()
             if any(os.path.realpath(m.get("root", "")) == root for m in s.get("members", []))]
    return ["%s (default)" % default] + named


def cmd_reach(args) -> int:
    """Let one running session talk with the sessions of one other folder."""
    if args.session:
        found = resolve.resolve(args.session)
        me = found.record if found.ok else None
        if me is None:
            print("refused: %s" % (found.reason or "no such session: %s" % args.session),
                  file=sys.stderr)
            return REFUSED
    else:
        me = registry.me()
    if not args.folder and not args.drop:
        rows = config.reaches()
        if not rows:
            print("no reaches")
        for r in rows:
            print("ref:%s -> %s" % (r.get("ref"), _home_tilde(r.get("root", ""))))
        return OK
    if not me or not me.get("ref"):
        print("refused: which session? name it with --session ref:xxxxxx", file=sys.stderr)
        return USAGE
    # A relative folder is the session's, as consent.take reads it: resolved
    # once, so the consent check and the reach name the same folder. The raw
    # string went to add_reach and was read against the shell's cwd, so after
    # a `cd` another folder was allowed (review, 2026-09-28).
    folder = os.path.join(me.get("cwd") or os.getcwd(), os.path.expanduser(args.folder)) \
        if args.folder else None
    if args.drop:
        n = config.drop_reach(me["ref"], folder, session=me)
        print("dropped %d reach(es) of ref:%s" % (n, me["ref"]))
        return OK
    try:
        config.check_reach(folder)              # before the typed consent is used up
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return USAGE
    # The typed command is consent only for the session that typed it.
    caller = registry.me()
    typed = ("reach", folder) if caller and caller.get("ref") == me.get("ref") else None
    why = _person_or_refuse("letting a session reach another folder", "xsm_reach", typed)
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
    try:
        entry, added = config.add_reach(me["ref"], folder, _by(),
                                        session=me)
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return USAGE
    print("%s: %s@%s [%s] can talk with the sessions in %s while it runs" % (
        "allowed" if added else "already allowed", me.get("name"), me.get("alias"), me["ref"],
        _home_tilde(entry["root"])))
    return OK


def _links_here(root: str) -> list:
    """The folders linked with this project folder, each as its label: a link
    of a folder above this one covers it too, and says so."""
    return [_home_tilde(other) + ("" if mine == root else " (via %s)" % _home_tilde(mine))
            for mine, other in config.links_covering(root)]


def cmd_link(args) -> int:
    """Link this project folder with another one, both ways, until unlinked."""
    here = _here(args)
    root = config.project_root(here)
    if not args.folder:
        rows = config.links()
        if not rows:
            print("no links. To link another folder, ask your user and run: xsm link <folder>")
        for ln in rows:
            ends = (os.path.realpath(ln.get("a") or ""), os.path.realpath(ln.get("b") or ""))
            via = any(root.startswith(e.rstrip("/") + "/") for e in ends)
            print("%s <-> %s%s" % (_home_tilde(ln.get("a", "")), _home_tilde(ln.get("b", "")),
                                   "  (this folder)" if root in ends else
                                   "  (covers this folder)" if via else ""))
        return OK
    other = config.project_root(os.path.join(here, os.path.expanduser(args.folder)))
    if args.command == "unlink":
        # Narrowing: anyone may.
        if config.drop_link(here, other):
            print("unlinked: %s and %s" % (_home_tilde(root), _home_tilde(other)))
            return OK
        # The link that applies may be a folder above this one's (review,
        # 2026-09-28): name it and the command that removes it, and leave it.
        above = [m for m, o in config.links_covering(root) if m != root and o == other]
        if above:
            print("no link of %s itself; it is linked with %s through %s. Remove that link "
                  "with: xsm unlink %s --dir %s" % (
                      _home_tilde(root), _home_tilde(other), _home_tilde(above[0]),
                      shlex.quote(other), shlex.quote(above[0])), file=sys.stderr)
            return REFUSED
        print("no link between %s and %s" % (_home_tilde(root), args.folder), file=sys.stderr)
        return REFUSED
    try:
        config.check_link(here, other)          # before the typed consent is used up
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return USAGE
    why = _person_or_refuse("linking two folders", "xsm_link", ("link", other, here))
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
    try:
        entry, added = config.add_link(here, other, _by())
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return USAGE
    there = entry["b"] if os.path.realpath(entry["a"]) == root else entry["a"]
    print("%s: the sessions in %s and in %s can talk, both ways, until `xsm unlink %s`" % (
        "linked" if added else "already linked", _home_tilde(root), _home_tilde(there),
        _home_tilde(there)))
    return OK


def cmd_block(args) -> int:
    ref = config.bare_ref(args.ref)     # the deny list holds bare refs, as `list` shows them
    if not ref or len(ref.split()) != 1:        # before anyone is asked, or anything is stored
        print("usage: xsm %s <ref> (the [ref] `xsm list` shows, e.g. a1b2c3)" % args.command,
              file=sys.stderr)
        return USAGE
    if args.command == "unblock":
        why = _person_or_refuse("lifting the block on session %s" % ref, "no",
                                ask=("unblock", ref))
        if why:
            print("refused: %s" % why, file=sys.stderr)
            return REFUSED
        changed = config.unblock(ref)
    else:
        changed = config.block(ref)
    print("%s %s" % (args.command + "ed" if changed else "no change for", ref))
    return OK


def cmd_frameworks(args) -> int:
    """Whether xsm starts workers inside Orca or herdr. By default it does not
    (the framework owns them); your user can lift that per framework: the agent
    asks them and runs `xsm frameworks ignore <name>`."""
    if args.action in ("ignore", "respect"):
        if not args.name:
            print("usage: xsm frameworks %s orca|herdr|all" % args.action, file=sys.stderr)
            return USAGE
        if args.action == "ignore":
            if args.name not in config.FRAMEWORK_NAMES + ("all",):   # before anyone is asked
                print("unknown framework %r: one of %s, or all" % (
                    args.name, ", ".join(config.FRAMEWORK_NAMES)), file=sys.stderr)
                return USAGE
            why = _person_or_refuse("letting xsm start workers inside %s" % args.name, "no",
                                    ask=("frameworks-ignore", args.name))
            if why:
                print("refused: %s" % why, file=sys.stderr)
                return REFUSED
        try:
            changed = config.set_framework_ignored(args.name, args.action == "ignore")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return USAGE
        print("%s: %s" % (args.name, "no change" if not changed else
                          "xsm starts workers inside it" if args.action == "ignore" else
                          "xsm leaves its workers to it"))
        return OK
    ignored = config.ignored_frameworks()
    here = workers.framework_host()
    for name in config.FRAMEWORK_NAMES:
        off = name in ignored or "all" in ignored
        print("%-6s %s%s" % (name, "ignored: xsm starts workers inside it" if off else
                             "respected: xsm starts no workers inside it",
                             "  (this terminal)" if here == name else ""))
    return OK


def cmd_projects(args) -> int:
    here = _here(args)
    root = config.project_root(here)
    if getattr(args, "table", False):
        table = [("**this folder**", _home_tilde(root), ", ".join(_memberships(here)))]
        for scope in config.projects():
            for i, m in enumerate(scope.get("members", [])):
                mine = os.path.realpath(m.get("root", "")) == root
                table.append((scope.get("id") if i == 0 else "",
                              _home_tilde(m.get("root", "")) + (" (this folder)" if mine else ""),
                              ""))
        for other in _links_here(root):
            table.append(("linked", other, ""))
        print("\n".join(_md_table(["project", "folder", "member of"], table)))
        if not config.projects() and not _links_here(root):
            print("\nNo links or named projects yet. To connect another folder, ask your user "
                  "and run `xsm link <folder>`.")
        return OK
    print("this folder (%s) is in: %s" % (_home_tilde(root), ", ".join(_memberships(here))))
    for other in _links_here(root):
        print("linked with: %s" % other)
    rows = config.projects()
    if not rows:
        print("no named projects. To connect another folder, ask your user and run: xsm link "
              "<folder>; for a group of several folders: xsm join <name>")
        return OK
    print("named projects:")
    for scope in rows:
        print("%s" % scope.get("id"))
        _print_members(scope, root)
    return OK


def cmd_homes(args) -> int:
    if args.action == "add":
        if not args.path or not args.runtime:
            print("usage: xsm homes add PATH --runtime claude|codex [--alias A]", file=sys.stderr)
            return USAGE
        print(json.dumps(config.add_home(args.path, args.runtime, args.alias), ensure_ascii=False))
        return OK
    if args.action == "remove":
        return OK if config.remove_home(args.path or "") else REFUSED
    for home in config.homes():
        print("%-10s %-7s %s" % (home.get("alias"), home.get("runtime"), home.get("path")))
    return OK


def cmd_send(args) -> int:
    body = args.text
    if args.held:
        if args.target or args.text or args.text_file or args.resend:
            print("refused: --held sends the message that was kept, as it was kept: give it no "
                  "target, text or --resend", file=sys.stderr)
            return USAGE
    else:
        if not args.target:
            print("usage: xsm send <target> --text \"...\"", file=sys.stderr)
            return USAGE
        if args.text_file:
            body = sys.stdin.read() if args.text_file == "-" else open(args.text_file).read()
        if not body:
            print("nothing to send: pass --text or --text-file", file=sys.stderr)
            return USAGE
        if args.outcome and args.kind != "reply":
            print("refused: --outcome belongs to the reply that closes a task, not to %s"
                  % args.kind, file=sys.stderr)
            return REFUSED
    result = send.send(args.target or "", body or "", kind=args.kind, reply_to=args.reply_to,
                       priority=args.priority, wait=args.wait, outcome=args.outcome,
                       msg_id=args.resend, resend=bool(args.resend), held=args.held)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False))
    else:
        print("\n".join(result.lines()))
    return {"delivered": OK, "sent-unconfirmed": UNCONFIRMED, "unknown": UNCONFIRMED,
            "held": REFUSED, "blocked": REFUSED, "refused": REFUSED}.get(result.status, USAGE)


def cmd_inbox(args) -> int:
    """Messages waiting for this Codex session, now rather than at the end of
    its turn. For a Claude session there is nothing to take: its inbox socket
    delivers as soon as it is idle."""
    me = registry.me()
    if not me:
        print("this session is not registered; run `xsm doctor`", file=sys.stderr)
        return REFUSED
    if args.wait > 0:
        if args.wait > inbox.MAX_WAIT:
            print("[xsm] --wait clamped to %ds; it blocks one command, it is not a daemon."
                  % inbox.MAX_WAIT, file=sys.stderr)
        inbox.wait_for(me.get("session_id"), args.wait, out=sys.stderr)
    texts = receive.take_inbox(me)
    if not texts:
        print("(no messages waiting)")
        return OK
    print(("\n\n" + "-" * 40 + "\n\n").join(texts))
    return OK


def cmd_status(args) -> int:
    if (ledger.status(args.msg_id).get("scope") or "").startswith("remote:"):
        # Its receipt is on the other machine; only the peer can say (issue #4).
        from . import remote
        state = remote.reconcile(args.msg_id, args.wait)
    else:
        state = ledger.wait_for(args.msg_id, args.wait) if args.wait else ledger.status(args.msg_id)
    if not state:
        print("no such message", file=sys.stderr)
        return REFUSED
    # The outcome is the task's, not the delivery's: a task can be delivered and
    # then fail, so it goes after the status rather than in place of it.
    ending = "  (task %s)" % state["outcome"] if state.get("outcome") else ""
    if state.get("note") or state.get("status") in ("unknown", "error", "refused"):
        ending += "  - %s" % (state.get("note") or state.get("error") or "")
    print(json.dumps(state, ensure_ascii=False, indent=1) if args.json
          else "%s  %s -> %s  %s%s" % (state.get("status"), (state.get("from") or {}).get("name"),
                                       (state.get("to") or {}).get("name"), state.get("id"),
                                       ending))
    return OK if state.get("status") == "delivered" else UNCONFIRMED


def _mark_undelivered(rows: list) -> list:
    """A message still `queued` whose target has since stopped will never be
    recorded as delivered. Say so instead of leaving it looking pending."""
    live = {r.get("ref") for r in registry.records() if r.get("state") == "live"}
    for row in rows:
        if row.get("status") == "queued" and (row.get("to") or {}).get("runtime") == "remote":
            # The target lives on the peer, not in this registry; its receipt
            # is there too (issue #4).
            row["note"] = "on %s; `xsm status %s` asks it" % (
                (row.get("scope") or "")[len("remote:"):], row.get("id"))
        elif row.get("status") == "queued" and (row.get("to") or {}).get("ref") not in live:
            row["status"] = "undelivered"
            row["note"] = "target stopped before recording it"
        elif row.get("status") == "queued" and row.get("forecast") == "hold":
            # Claude holds it inside the receiving session, where xsm cannot
            # see it; left as `queued` it read as on its way (report, 2026-09-29).
            row["status"] = "awaiting-approval"
            row["note"] = "Claude held it for its user; it arrives only if they approve it"
    return rows


def cmd_ledger(args) -> int:
    if getattr(args, "mine", False):
        me = registry.me()
        mine = (me or {}).get("ref")
        rows = [r for r in _mark_undelivered(ledger.recent(10 ** 6))
                if mine and mine in ((r.get("from") or {}).get("ref"),
                                     (r.get("to") or {}).get("ref"))][:args.last]
    else:
        rows = _mark_undelivered(ledger.recent(args.last))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return OK
    if getattr(args, "table", False):
        # Three narrow columns. The id is left out: `xsm ledger` has it, and a
        # sixteen-character cell was what pushed the table past the pane.
        me_ref = (registry.me() or {}).get("ref") if getattr(args, "mine", False) else None

        def who(side: dict) -> str:
            return "you" if me_ref and side.get("ref") == me_ref else _short(side.get("name"), 18)
        print("\n".join(_md_table(["status", "from → to", "message"], [
            ("%s%s" % (row.get("status"),
                       " / task %s" % row["outcome"] if row.get("outcome") else ""),
             "%s → %s" % (who(row.get("from") or {}), who(row.get("to") or {})),
             _short(row.get("preview"), 36))
            for row in rows])) if rows else "no messages")
        return OK
    if args.compact:
        for row in rows:
            print("%s%s %s->%s %s: %s" % (
                row.get("status"), "/%s" % row["outcome"] if row.get("outcome") else "",
                (row.get("from") or {}).get("name"), (row.get("to") or {}).get("name"),
                row.get("id"), (row.get("preview") or "").replace("\n", " ")[:40]))
        if not rows:
            print("no messages")
        return OK
    for row in rows:
        print("%-16s %-17s %s -> %s  %s%s" % (
            row.get("id"), row.get("status"), (row.get("from") or {}).get("name"),
            (row.get("to") or {}).get("name"), (row.get("preview") or "").replace("\n", " ")[:50],
            "  [task %s]" % row["outcome"] if row.get("outcome") else ""))
    return OK


def cmd_metrics(args) -> int:
    from . import telemetry
    report = telemetry.summarize()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return OK
    if telemetry.DISABLED:
        print("telemetry is off (XSM_NO_TELEMETRY is set)")
    if not report["span_count"] and not report["point_count"]:
        print("nothing recorded yet")
        return OK
    print("%d span(s)" % report["span_count"])
    for name, row in sorted(report["spans"].items()):
        print("  %-26s %5d call(s)  %4d error(s)  avg %7.1fms  p95 %7.1fms" % (
            name, row["count"], row["errors"], row["avg_ms"], row["p95_ms"]))
    for name, row in sorted(report["counters"].items()):
        print("  %-26s %5g total over %d point(s)" % (name, row["total"], row["count"]))
    for name, row in sorted(report["histograms"].items()):
        print("  %-26s %5d sample(s)  avg %7.1fms  p95 %7.1fms" % (
            name, row["count"], row["avg_ms"], row["p95_ms"]))
    return OK


def cmd_otlp_export(args) -> int:
    from . import otlp_export
    url = otlp_export.endpoint(args.endpoint)
    if args.follow:
        return otlp_export.run(url, interval=args.interval)
    result = otlp_export.export_once(url)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return OK
    if not result["spans_sent"] and not result["points_sent"]:
        failed = False in (result["traces_ok"], result["metrics_ok"])
        print("%s -> %s" % ("could not reach the collector" if failed else "nothing new to send",
                            url))
        return OK
    print("sent %d span(s) and %d metric point(s) -> %s" % (
        result["spans_sent"], result["points_sent"], url))
    return OK


def _held_deliver(args) -> int:
    """Hand the session a message its gate kept, on its user's yes (user decision,
    2026-10-01: a hold is a stop for the person to decide, never a wall). The
    agent asks, runs this to be shown the reply, and once more on a yes; the
    message then comes as this command's output, with the sender's context, and
    leaves the held list. Only the session it was held for takes it, unless the
    person runs the command themselves."""
    name = os.path.basename(args.id or "")
    p = paths.path(paths.HELD, "%s.json" % name)
    entry = paths.read_json(p) if name else None
    if not isinstance(entry, dict):
        print("no such held message", file=sys.stderr)
        return REFUSED
    me = registry.me()
    if entry.get("receiver_ref") and me and me.get("ref") != entry["receiver_ref"] and \
            not workers.human_terminal():
        print("refused: it was held for another session (ref:%s); that session delivers it"
              % entry["receiver_ref"], file=sys.stderr)
        return REFUSED
    # A hold from before receiver_ref was kept names its session only: it goes to the
    # session of that name, or to the person running this themselves (2026-10-02).
    if not entry.get("receiver_ref") and not workers.human_terminal() and \
            (not me or entry.get("receiver") not in (me.get("name"), "%s@%s" % (
                me.get("name"), me.get("alias")))):
        print("refused: an older xsm held this %s" % (
            "for %s, not for this session; that session delivers it" % entry["receiver"]
            if entry.get("receiver") else "without saying which session it was for"),
            file=sys.stderr)
        return REFUSED
    # The sender's own words, once and short: they go into the agent's context.
    who = " ".join(str(entry.get("from") or "an unknown sender").split())[:60]
    why = _person_or_refuse("delivering the held message %s from %s to this session (it was "
                            "held: %s)" % (name, who, " ".join(str(entry.get("reason")).split())[:120]),
                            "no", ask=("held-deliver", name))
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
    header = entry.get("header") or {k: entry[k] for k in ("id", "from", "scope") if entry.get(k)}
    parsed = envelope.Parsed(True, header, entry.get("body") or "", entry.get("attrs") or {})
    runtime = (me or {}).get("runtime") or entry.get("runtime") or "claude"
    if entry.get("id"):
        receive._safely(ledger.receipt, entry["id"], "delivered", me,
                        "released from the held list on your user's reply")
    paths.append_jsonl("decisions.jsonl", {
        "event": "held-deliver", "decision": "pass", "id": entry.get("id"), "held": name,
        "reason": "released on the person's reply: %s" % (_verdict or "")[:200],
        "receiver": (me or {}).get("name")})
    try:
        os.unlink(p)
    except OSError:
        pass                                    # gone already: the message still comes
    print(envelope.sender_context(parsed, runtime, cwd=(me or {}).get("cwd")))
    print()
    print(parsed.body)
    if entry.get("truncated"):
        print("\n[xsm] The held copy was cut at 4000 characters.")
    return OK


def cmd_held(args) -> int:
    entries = sorted(glob.glob(paths.path(paths.HELD, "*.json")))
    if args.action == "deliver":
        return _held_deliver(args)
    if args.action == "show":
        entry = paths.read_json(paths.path(paths.HELD, "%s.json" % args.id))
        if not entry:
            print("no such held message", file=sys.stderr)
            return REFUSED
        print(json.dumps(entry, ensure_ascii=False, indent=1))
        return OK
    if args.action == "drop":
        target = paths.path(paths.HELD, "%s.json" % args.id)
        if not os.path.exists(target):
            return REFUSED
        os.unlink(target)
        return OK
    if getattr(args, "table", False):
        rows = []
        for p in entries:
            entry = paths.read_json(p, {}) or {}
            body = (entry.get("body") or "").replace("\n", " ")
            rows.append((_short(entry.get("from") or "unknown", 18),
                         _short(entry.get("reason"), 30), _short(body, 24)))
        print("\n".join(_md_table(["from", "why it was held", "message"], rows))
              if rows else "nothing held")
        return OK
    for p in entries:
        entry = paths.read_json(p, {}) or {}
        body = (entry.get("body") or "").replace("\n", " ")
        print("%-16s %-7s from %-28s %s\n%s%s" % (
            os.path.basename(p)[:-5], entry.get("runtime"), entry.get("from") or "unknown",
            entry.get("reason"), " " * 17, body[:70] + ("…" if len(body) > 70 else "")))
    if not entries:
        print("nothing held")
    return OK


def cmd_install(args) -> int:
    """`--dev` is the policy runtime=checkout for this run (policy.py), through
    the variable that overrides it; the variable is put back afterwards."""
    if not args.dev:
        return _cmd_install(args)
    before = os.environ.get("XSM_RUNTIME")
    os.environ["XSM_RUNTIME"] = "checkout"
    try:
        return _cmd_install(args)
    finally:
        if before is None:
            os.environ.pop("XSM_RUNTIME", None)
        else:
            os.environ["XSM_RUNTIME"] = before


def _cmd_install(args) -> int:
    targets = [(h, "claude") for h in (args.claude_home or [])] + \
              [(h, "codex") for h in (args.codex_home or [])]
    if args.refresh and not targets:
        # Every home xsm already knows: the commands, skills and MCP entry it
        # wrote there are copies, and a copy eight versions behind is what a
        # second profile quietly ran for a day (2026-09-23).
        targets = [(h["path"], h["runtime"]) for h in config.homes()]
        # A home its user deleted stays in the list; writing into it would
        # bring back a profile they threw away (2026-09-27).
        for home, runtime in list(targets):
            if not os.path.isdir(home):
                print("%s: gone; skipped (forget it with `xsm homes remove %s`)"
                      % (_home_tilde(home), home))
                targets.remove((home, runtime))
        if not targets and config.homes():
            return OK
        if not targets:
            print("nothing installed yet; name a home: --claude-home ~/.claude", file=sys.stderr)
            return USAGE
    if not targets:
        print("name at least one home: --claude-home ~/.claude-3 --codex-home ~/.codex\n"
              "or refresh the ones already installed: xsm install --refresh", file=sys.stderr)
        return USAGE
    for home, runtime in list(targets):
        plugin = install.plugin_installed(home)
        if not plugin or args.force:
            continue
        if args.refresh:
            # Refreshing every home it knows must not stop at one that has
            # moved to the plugin; the plugin updates itself by version. What
            # an earlier direct install left there is still ours to clear.
            missing = install.plugin_missing_hooks(home)
            print("%s: the xsm plugin (%s) %s; skipped" % (
                _home_tilde(home), plugin, install.plugin_outdated_note(missing, home) if missing
                else "keeps it up to date"))
            _print_retired(home, install.remove_retired(home))
            if runtime == "claude":
                _claude_settings(home, quiet=True)
            if runtime == "codex":
                cleared = install.clear_codex_leftovers(home)
                if cleared:
                    print("  removed what an earlier `xsm install` left beside the plugin: %s"
                          % ", ".join(cleared))
            targets.remove((home, runtime))
            continue
        print("refused: %s has the xsm plugin (%s), which brings its own hooks; installing "
              "again would run every hook twice. Remove the plugin, or pass --force if you "
              "know why you want both." % (_home_tilde(home), plugin), file=sys.stderr)
        return REFUSED
    if not targets:
        print("every home xsm knows is on the plugin; nothing to refresh")
        return OK
    code = _install(args, targets)
    if code == OK and not args.dry_run:
        print(RESTART_NOTE)
    return code


# Printed after every install: a running MCP server keeps the tool list it
# started with, so a session opened before an update had no xsm_link while its
# newly loaded skill told it to call one (review, 2026-09-28).
RESTART_NOTE = ("Sessions already open keep the xsm tools and hooks they started with: start a new "
                "session, or reconnect the MCP server (Claude Code: /mcp, then xsm), to use new "
                "ones. Until then, the agent runs the shell command (e.g. `xsm link <folder>`), "
                "which asks for your reply.")


def _install(args, targets) -> int:
    try:
        chosen = install.resolve_python(args.python)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return USAGE
    if args.dry_run:
        print("hooks would run under %s" % chosen)
    else:
        record = install.pin_python(chosen)
        print("hooks will run under %s%s" % (record["path"],
                                            " (%s)" % record["version"] if record["version"] else ""))
    root = _make_runtime(args.dry_run)
    failed, linked = False, False
    for home, runtime in targets:
        if args.dry_run:
            print(install.diff(home, runtime, root))
            continue
        result = install.apply(home, runtime)
        if result.get("error"):
            print("%s: %s" % (result["file"], result["error"]), file=sys.stderr)
            failed = True
            continue
        if result.get("unchanged"):
            print("already installed in %s (nothing changed)" % result["file"])
        else:
            print("installed into %s (backup: %s)" % (result["file"], result.get("backup", "none")))
        _print_retired(home, install.remove_retired(home))
        if runtime == "claude":
            _claude_settings(home)
        if not linked:
            linked = True
            _link_cli()
        if not args.no_commands:
            state, detail = install.install_skill(home, refresh=args.refresh)
            print("  skill: %s" % {
                "linked": "linked to the installed runtime",
                "copy-current": "a copy is in place and matches the runtime",
                "copy-stale": "a copy has fallen behind; refresh it with `xsm install --refresh`",
                "link-stale": "linked to an older runtime or plugin version; refresh it with "
                              "`xsm install --refresh`",
                "nested-link": "a link sits inside the existing directory (%s);\n"
                               "           remove it: rm %s" % (detail, detail),
                "foreign": "something else is at skills/xsm; left alone",
            }.get(state, state))
            print("  commands: %s" % ("/xsm list, /xsm send <target> <message>, … "
                                      "(the skill takes them as arguments)" if runtime == "claude"
                                      else "$xsm list, $xsm send <target> <message>, …"))
        if runtime == "claude" and args.statusline:
            outcome = install.install_statusline(home)
            print("  statusLine: %s" % {
                "installed": "set to `xsm statusline` (no model call)",
                "already": "already set",
                "composed": "kept yours and added one xsm line under it",
            }[outcome])
        if not args.no_mcp:
            outcome = install.install_mcp(home, runtime)
            print("  MCP server: %s" % {
                "added": "registered (xsm_post, xsm_channel, xsm_decide)",
                "current": "already registered",
                "replaced": "re-registered with the current command",
            }.get(outcome, outcome))
        if runtime == "codex":
            print("  Codex asks you to trust hooks once, at the next session start. "
                  "Until you do, the hook does not run. Codex has no SessionEnd, so a "
                  "stopped Codex session always reads as stale. A hook already trusted keeps "
                  "the command it has, so a refresh never asks again; its script stays where "
                  "it was, and only the plugin (`codex plugin add xsm@xsm`) moves it.")
    if not args.dry_run:
        try:
            gone = install.prune_snapshots()
        except OSError:
            gone = []
        if gone:
            print("removed %d older runtime cop%s no session or config uses: %s" % (
                len(gone), "y" if len(gone) == 1 else "ies", ", ".join(gone)))
    return USAGE if failed else OK


def _make_runtime(dry_run: bool) -> str:
    """Make the folder the hooks, the MCP server and `xsm` on PATH will run from,
    and say which it is. A copy of this checkout under ~/.xsm/runtime, unless
    this CLI is one already (a snapshot, a plugin copy), or the policy says
    checkout (--dev). Failing to copy is not failing to install: the hooks then
    run from the checkout, as they did before there were copies."""
    if install.runtime_in_place():
        print("runtime: %s (this is a copy already; nothing to copy)" % _home_tilde(install.REPO))
        return install.runtime_root()
    if policy.get("runtime") == "checkout":
        print("runtime: this checkout, %s (--dev): a session that macOS keeps out of this folder "
              "cannot run the hooks" % _home_tilde(install.REPO))
        return install.REPO
    if dry_run:
        root = install.runtime_root(planned=True)
        print("hooks would run from a copy of this checkout at %s" % _home_tilde(root))
        return root
    try:
        snapshot = install.make_snapshot()
    except OSError as err:
        print("warning: could not copy the runtime under %s (%s); the hooks run from this "
              "checkout" % (_home_tilde(paths.HOME), err), file=sys.stderr)
        return install.REPO
    print("runtime: %s, a copy of %s%s (hooks, MCP server and `xsm` run from it, outside the "
          "folders macOS guards)" % (
              _home_tilde(snapshot["path"]), _home_tilde(install.REPO),
              " at %s" % (snapshot.get("describe") or snapshot["rev"][:7])
              if snapshot.get("describe") or snapshot.get("rev") else ""))
    return snapshot["path"]


def _link_cli() -> None:
    """`xsm` on PATH is the runtime's launcher, whichever runtime the home is."""
    state = install.install_cli()
    if state == "foreign":
        print("  cli: ~/.local/bin/xsm is something else; link the launcher onto PATH yourself: "
              "ln -s %s <directory-on-PATH>/xsm" % shlex.quote(install.launcher()))
        return
    print("  cli: %s (~/.local/bin/xsm)" % state)
    if os.path.expanduser("~/.local/bin") not in os.environ.get("PATH", "").split(os.pathsep):
        print("  warning: add ~/.local/bin to PATH")


def _claude_settings(home: str, quiet: bool = False) -> None:
    """What install writes into a Claude home's settings.json besides the hooks:
    the allow rules, crossSessionInbound, and the path in xsm's own statusLine.
    `quiet` (a home that is on the plugin) says only what changed."""
    state = install.allow_form_tools(home)
    if state == "invalid":
        print(_settings_invalid_note(home))
        return
    if state == "updated":
        print("  took out allow rules an earlier xsm added and no longer wants")
    elif state == "added" or not quiet:
        print("  allow rules: %s (the xsm skill, the approval forms and the messaging commands and "
              "tools; so Claude's auto and default modes never stop a message or its ask)" % state)
    inbound = install.set_inbound(home)
    if inbound == "set":
        print("  crossSessionInbound: set to \"accept\" (Claude delivers a message from another "
              "of your sessions whatever the two permission modes are)")
    elif inbound.startswith("kept:"):
        print("  crossSessionInbound: this home says \"%s\", which is yours, so it stays; Claude "
              "will %s the messages from your other sessions" % (
                  inbound[5:], "hold for you" if inbound[5:] == "hold" else inbound[5:]))
    if install.refresh_statusline(home):
        print("  statusLine: now runs the installed runtime")


def _settings_invalid_note(home: str) -> str:
    """A Claude settings file xsm cannot read is left as it is, and named."""
    return ("  %s is not valid JSON, so xsm left it as it is and added no allow rules; fix it, "
            "then run `xsm install --refresh`" % _home_tilde(install.settings_invalid(home) or home))


def _print_retired(home: str, gone: list) -> None:
    if gone:
        print("  removed %d per-command file(s) from before /xsm <command>: %s" % (
            len(gone), ", ".join(os.path.basename(p) for p in gone)))


def cmd_uninstall(args) -> int:
    targets = [(h, "claude") for h in (args.claude_home or [])] + \
              [(h, "codex") for h in (args.codex_home or [])]
    for home, runtime in targets or [(h["path"], h["runtime"]) for h in config.homes()]:
        result = install.remove(home, runtime)
        if install.remove_mcp(home, runtime):
            print("%s: removed the MCP server" % home)
        gone = install.remove_retired(home)
        if gone:
            print("%s: removed %d command file(s) from an earlier version" % (home, len(gone)))
        if install.remove_skill(home):
            print("%s: unlinked the skill" % home)
        if runtime == "claude":
            if install.remove_statusline(home):
                print("%s: removed the xsm statusLine" % home)
            if install.remove_inbound(home):
                print("%s: removed the crossSessionInbound xsm set" % home)
            if install.remove_form_tools(home):
                print("%s: removed the xsm skill, approval-form tools, asking commands and "
                      "messaging rules from permissions.allow" % home)
        print("%s: removed %s xsm hook group(s)%s" % (
            result.get("file"), result.get("removed", 0),
            "" if not result.get("error") else " (%s)" % result["error"]))
    return OK


def cmd_doctor(args) -> int:
    report = install.doctor()
    report["policy"] = policy.report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return OK
    if getattr(args, "table", False):
        print("\n".join(_md_table(["check", "result"], _doctor_rows(report))))
        return OK
    print("xsm        %s" % (report.get("version") or "?"))
    cli = report.get("cli")
    if cli:
        print("cli        %s" % install.cli_text(cli))
        for other in cli.get("on_path") or []:
            print("cli        also on PATH as %s: %s" % (other["via"], install.cli_text(other)))
    print("state      %s" % report["xsm_home"])
    for line in _runtime_lines(report.get("runtime") or {}):
        print("runtime    %s" % line)
    if report.get("policy"):
        print("policy     %s" % _policy_note(report["policy"]))
    print("python     %s%s" % (report["interpreter"], "" if report["interpreter_ok"] else "  TOO OLD"))
    versions = report.get("codex_binaries") or []
    if not versions:
        print("codex      not found on PATH")
    for path, note in versions:
        broken = note.startswith("does not run")
        print("codex      %-45s %s%s" % (_home_tilde(path), note,
                                         "  <- xsm skips this one" if broken else ""))
    print("sessions   %(registered)d registered, %(live)d live, %(unregistered)d unregistered"
          % report["sessions"])
    print("hooks      %d decision(s) recorded, %d internal error(s)"
          % (report["decisions_seen"], report["hook_errors_recent"]))
    print("held       %d message(s)" % report["held"])
    print("native     %s" % _native_note(report))
    for pid, folder in report.get("orphaned_servers") or []:
        print("orphaned   %s" % _orphan_note(pid, folder))
    for plan in report["installs"]:
        if plan.get("error"):
            print("install    %s: %s" % (plan["file"], plan["error"]))
            continue
        states = ", ".join("%s:%s" % (a["event"], a["action"]) for a in plan["actions"])
        print("install    %-45s %s" % (plan["file"], states))
        for line in _hook_form_lines(plan):
            print("hooks      %s" % line)
    for home in report.get("gone") or []:
        print("gone       %s: deleted; forget it with `xsm homes remove %s`" % (_home_tilde(home), home))
    for home, trust in (report.get("codex_trust") or {}).items():
        if not trust:
            print("codex      %s: xsm hooks not installed" % home)
            continue
        missing = [e for e, ok in trust.items() if not ok]
        print("codex      %s: hooks %s" % (home, "trusted" if not missing else
              "NOT trusted for %s — start codex there and choose 'Trust all and continue'"
              % ", ".join(missing)))
    if not report.get("tmux"):
        print("tmux       not found: `xsm spawn` needs it (messaging does not)")
    for home, plugin in (report.get("plugins") or {}).items():
        if plugin:
            missing = (report.get("plugin_missing_hooks") or {}).get(home)
            root = (report.get("plugin_roots") or {}).get(home)
            older = (report.get("plugin_older") or {}).get(home)
            print("plugin     %-45s xsm %s%s%s%s" % (
                _home_tilde(home), plugin, " at %s" % root if root else "",
                "  " + older if older else "",
                ("  " + install.plugin_outdated_note(missing, home)) if missing else ""))
    for home, missing in (report.get("allow_missing") or {}).items():
        if missing:
            print("allow      %s" % _allow_note(home, missing))
    for home in report.get("settings_invalid") or {}:
        print("allow      %s" % _settings_invalid_note(home).strip())
    for home, value in (report.get("inbound") or {}).items():
        note = _inbound_note(home, value, report.get("policy") or {})
        if note:
            print("inbound    %s" % note)
    for home, files in (report.get("stale") or {}).items():
        if files:
            print("stale      %s: %d file(s) behind the repo; refresh with `xsm install --refresh`"
                  % (_home_tilde(home), len(files)))
    runtimes = {h["path"]: h.get("runtime") for h in config.homes()}
    for home, files in (report.get("leftovers") or {}).items():
        if files and runtimes.get(home) == "codex":
            print("leftover   %s: %s from an earlier `xsm install` beside the plugin; its hooks run "
                  "twice and the second refuses every message. Remove with `xsm install --refresh`"
                  % (_home_tilde(home), ", ".join(_home_tilde(f) for f in files)))
        elif files:
            print("leftover   %s: a skills/xsm from an earlier `xsm install`; bare /xsm goes to "
                  "it, not to the plugin. Remove with `xsm uninstall --claude-home %s`"
                  % (_home_tilde(home), _home_tilde(home)))
    for home, files in (report.get("retired") or {}).items():
        if files:
            print("retired    %s: %d per-command file(s) from before /xsm <command>; clear them "
                  "with `xsm install --refresh`" % (_home_tilde(home), len(files)))
    if not report.get("xsm_on_path"):
        print("path       `xsm` is not on PATH; the skill runs it by that name "
              "(ln -s %s ~/.local/bin/xsm)" % install.launcher())
    for line in _stuck_lines(report.get("stuck") or {}):
        print("stuck      %s" % line)
    for note in report["limits"]:
        print("limit      %s" % note)
    return OK


def _stuck_lines(stuck: dict) -> list:
    """What is waiting on someone. Each of these cost an investigation once."""
    lines = []
    for req in stuck.get("approvals") or []:
        if req.get("parent"):
            lines.append("%s has waited %ds for a yes or no from you: %s (the session that "
                         "started it asks you and runs `xsm approve %s`)" % (
                             req.get("worker"), req.get("waiting_s"), req.get("tool"),
                             req.get("id")))
        else:        # no session started it, and only a session's parent asks about a worker
            lines.append("%s has waited %ds for approval from whoever started it: %s [%s] (no "
                         "session did, so only a person at a terminal can answer)" % (
                             req.get("worker"), req.get("waiting_s"), req.get("tool"),
                             req.get("id")))
    for row in stuck.get("undelivered") or []:
        lines.append("%s -> %s is still queued: %s" % (
            (row.get("from") or {}).get("name"), (row.get("to") or {}).get("name"), row.get("id")))
    for row in stuck.get("uncertain") or []:
        lines.append("%s -> %s may or may not have arrived: %s (xsm status %s)" % (
            (row.get("from") or {}).get("name"), (row.get("to") or {}).get("name"), row.get("id"),
            row.get("id")))
    for reason, count in sorted((stuck.get("send_failures") or {}).items()):
        lines.append("%d send(s) failed with %s" % (count, reason))
    for rec in stuck.get("threads_replaced") or []:
        lines.append("%s [%s] is a Codex thread its TUI has left; it reads nothing" % (
            rec.get("name"), rec.get("ref")))
    return lines


# From this version on the MCP server loads every module at start (mcp.preload),
# so one running from a removed folder keeps working on its old version.
PRELOADS_FROM = "0.4.9"


def _orphan_note(pid: int, folder: str) -> str:
    """A server whose folder a plugin update removed: broken before 0.4.9, only
    old after it (issue #6)."""
    version = os.path.basename(folder)
    if version[:1].isdigit() and install.version_key(version) >= install.version_key(PRELOADS_FROM):
        return ("xsm MCP server pid %d still runs %s (its folder was removed; it keeps working); "
                "restart that session to use the installed version" % (pid, version))
    return ("xsm MCP server pid %d runs from %s, which was removed; its xsm tools fail until "
            "that session restarts" % (pid, _home_tilde(folder)))


def _allow_note(home: str, missing: list) -> str:
    """A Claude home whose settings lack the rules that let the agent ask
    (issue #9): auto mode can refuse the skill or the command before xsm asks.
    Only `xsm install --refresh` writes them (never a hook), and the agent runs it.
    Whether this home runs in auto mode is not knowable from here (a flag, a
    project setting or Shift+Tab turns it on per session), so it says it only
    matters there (2026-10-01)."""
    return ("%s: %d xsm allow rule(s) missing from settings.json (the skill, the approval "
            "forms, the commands that ask you, and the messaging commands and tools). Claude's "
            "auto mode and default mode can stop the agent on them, before xsm asks you or "
            "before a message goes; your agent can add them by running `xsm install --refresh`"
            % (_home_tilde(home), len(missing)))


def _runtime_lines(rt: dict) -> list:
    """Which runtime the installed hooks run from, and whether the checkout it
    was copied from has moved on (the agent then runs `install --refresh`)."""
    if rt.get("mode") == "checkout":
        return ["the checkout %s (policy runtime=checkout, or --dev): the hooks, the MCP server "
                "and `xsm` run from it, and a session macOS keeps out of that folder cannot run "
                "them; `xsm install --refresh` without --dev moves them to a copy"
                % _home_tilde(rt.get("running_from") or "")]
    snap = rt.get("snapshot")
    if not snap:
        if rt.get("in_place"):
            return ["this CLI runs from %s, a copy already" % _home_tilde(rt.get("running_from"))]
        return ["no copy installed: the hooks, the MCP server and `xsm` run from the checkout %s, "
                "and a session macOS keeps out of that folder cannot run them; your agent can "
                "run `xsm install --refresh` to make a copy under ~/.xsm/runtime"
                % _home_tilde(rt.get("running_from") or "")]
    stamp = snap.get("describe") or (snap.get("rev") or "")[:7]
    out = ["snapshot %s (xsm %s) at %s, copied from %s%s" % (
        snap.get("id"), snap.get("version") or "?", _home_tilde(snap.get("path") or ""),
        _home_tilde(snap.get("source") or "?"), " at %s" % stamp if stamp else "")]
    checkout = rt.get("checkout")
    if checkout and checkout.get("gone"):
        out.append("the checkout %s it was copied from is gone; the copy keeps working"
                   % _home_tilde(checkout["path"]))
    elif checkout and not checkout.get("same"):
        ahead = checkout.get("ahead")
        out.append("the checkout %s is %s the copy: your agent can update it by running `%s "
                   "install --refresh`" % (
                       _home_tilde(checkout["path"]),
                       "%d commit(s) ahead of" % ahead if ahead else "different from",
                       os.path.join(checkout["path"], "bin", "xsm")))
    return out


def _policy_note(values: dict) -> str:
    """Every switch and its value (policy.py, config.POLICY_DEFAULTS; set in
    config.json or XSM_<NAME>); the ones moved off their open default are named,
    so a machine put back on the old rules says so."""
    shown = " ".join("%s=%s" % (k, str(v).lower()) for k, v in sorted(values.items()))
    base = policy.defaults()
    changed = sorted(k for k, v in values.items() if v != base.get(k))
    return shown + ("  (not the default: %s)" % ", ".join(changed) if changed else "")


def _hook_form_lines(plan: dict) -> list:
    """What the hook commands in a Claude home's settings.json can do to a person:
    the old forms are `python <script>`, whose status 2 for a script it cannot
    open is "block" to Claude Code (2026-10-01)."""
    if plan.get("runtime") != "claude":
        return []
    forms = {f for a in plan.get("actions", []) for f in a.get("forms", [])}
    gone = sorted({s for a in plan.get("actions", []) for s in a.get("gone", [])})
    where = _home_tilde(plan.get("file") or "")
    out = []
    if "unguarded" in forms:
        out.append("%s: a hook here is the old `python <script>` form. If python cannot open the "
                   "script (a folder macOS denies a session, a moved checkout) it exits 2 and "
                   "Claude BLOCKS every prompt. Your agent can replace it by running "
                   "`xsm install --refresh`" % where)
    elif "guarded" in forms:
        out.append("%s: a hook here is the `python <script> || exit 1` form. It no longer blocks "
                   "a prompt, but stops working when that python or path goes away. Your agent "
                   "can replace it by running `xsm install --refresh`" % where)
    if gone:
        out.append("%s: the hook script %s is gone, so those hooks fail on every event; `xsm "
                   "install --refresh`" % (where, ", ".join(_home_tilde(s) for s in gone)))
    return out


def _inbound_note(home: str, value, changed: dict) -> str | None:
    """What a Claude home's crossSessionInbound means for the messages it gets, or
    None when it is "accept" or xsm was told to leave it."""
    if value == "accept" or changed.get("claude_inbound") == "leave":
        return None
    where = _home_tilde(home)
    if value is None:
        return ("%s: crossSessionInbound is not set, so Claude holds a message from a session in "
                "another permission mode for its user; your agent can run `xsm install "
                "--refresh`, which sets \"accept\" (the setting is documented from Claude Code "
                "2.1.224)" % where)
    return ("%s: crossSessionInbound is \"%s\"%s, so Claude %s messages from your other "
            "sessions; `xsm install` leaves a value that is set" % (
                where, value, " (yours)" if value in ("hold", "refuse") else
                ", which Claude does not recognize",
                "drops" if value == "refuse" else "holds"))


def _native_note(report: dict) -> str:
    """How the gate treats Claude's own messages (no xsm header), ADR-0013."""
    if report.get("strict_peers"):
        return "Claude messages without an xsm header are all held (strict_peers)"
    off_machine = ("from off it they are held (remote_native)"
                   if (report.get("policy") or {}).get("remote_native") == "hold" else
                   "from off it they pass, with a note naming where they came from")
    return ("Claude messages without an xsm header pass from this machine, whatever the scope; "
            + off_machine)


def _doctor_rows(report: dict) -> list:
    rows = [("state", _home_tilde(report["xsm_home"])),
            ("python", report["interpreter"] + ("" if report["interpreter_ok"] else " **TOO OLD**")),
            ("codex", report["codex_binary"] or "not found on PATH"),
            ("sessions", "%(registered)d registered, %(live)d live, %(unregistered)d unregistered"
             % report["sessions"]),
            ("hooks", "%d decision(s) recorded, %d internal error(s)"
             % (report["decisions_seen"], report["hook_errors_recent"])),
            ("held", "%d message(s)" % report["held"]),
            ("native", _native_note(report))]
    if report.get("cli"):
        rows.insert(0, ("cli", install.cli_text(report["cli"])))
    for line in _runtime_lines(report.get("runtime") or {}):
        rows.append(("runtime", line))
    if report.get("policy"):
        rows.append(("policy", _policy_note(report["policy"])))
    for pid, folder in report.get("orphaned_servers") or []:
        rows.append(("orphaned", _orphan_note(pid, folder)))
    for plan in report["installs"]:
        rows.append(("install", "%s: %s" % (_home_tilde(plan["file"]), plan["error"])
                     if plan.get("error") else "%s: %s" % (
                         _home_tilde(plan["file"]),
                         ", ".join("%s %s" % (a["event"], a["action"]) for a in plan["actions"]))))
        for line in _hook_form_lines(plan):
            rows.append(("hooks", line))
    for home, trust in (report.get("codex_trust") or {}).items():
        missing = [e for e, ok in (trust or {}).items() if not ok]
        rows.append(("codex trust", "%s: %s" % (_home_tilde(home), (
            "xsm hooks not installed" if not trust else "hooks trusted" if not missing else
            "**NOT trusted** for %s: start codex there and choose 'Trust all and continue'"
            % ", ".join(missing)))))
    for home, missing in (report.get("plugin_missing_hooks") or {}).items():
        if missing:
            rows.append(("plugin", "%s: %s" % (_home_tilde(home),
                                               install.plugin_outdated_note(missing, home))))
    for home, older in (report.get("plugin_older") or {}).items():
        if older:
            rows.append(("plugin", "%s: xsm %s at %s, %s" % (
                _home_tilde(home), report["plugins"][home], report["plugin_roots"][home], older)))
    for home, missing in (report.get("allow_missing") or {}).items():
        if missing:
            rows.append(("allow", _allow_note(home, missing)))
    for home in report.get("settings_invalid") or {}:
        rows.append(("allow", _settings_invalid_note(home).strip()))
    for home, value in (report.get("inbound") or {}).items():
        note = _inbound_note(home, value, report.get("policy") or {})
        if note:
            rows.append(("inbound", note))
    for home, files in (report.get("stale") or {}).items():
        if files:
            rows.append(("stale", "%s: %d file(s) behind; refresh with `xsm install --refresh`"
                         % (_home_tilde(home), len(files))))
    runtimes = {h["path"]: h.get("runtime") for h in config.homes()}
    for home, files in (report.get("leftovers") or {}).items():
        if files:
            rows.append(("leftover", "%s: %s from an earlier `xsm install` beside the plugin; "
                         "remove with %s" % (
                             _home_tilde(home), ", ".join(_home_tilde(f) for f in files),
                             "`xsm install --refresh`" if runtimes.get(home) == "codex"
                             else "`xsm uninstall --claude-home %s`" % _home_tilde(home))))
    for note in report["limits"]:
        rows.append(("limit", note))
    return rows


def cmd_selftest(args) -> int:
    """Prove what the hook does with a peer message when its own code breaks: it
    lets it through with a note that it was not checked (policy fail_open, the
    default, user decision 2026-10-01), or refuses it when fail_open is off. A
    person's own prompt is never touched, and no status of 2 comes back."""
    import subprocess
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "hooks", "xsm-hook.py")
    probe = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "selftest",
                        "cwd": os.getcwd(), "prompt": "<cross-session-message from-mode=\"bypass\">\n"
                                                      "[xsm v1 id=selftest]\nhi\n</cross-session-message>"})
    env = dict(os.environ, XSM_FORCE_ERROR="1")
    out = subprocess.run([sys.executable, entry], input=probe, capture_output=True, text=True, env=env)
    blocked = '"decision": "block"' in out.stdout
    noted = not blocked and "could not check" in out.stdout
    closed = not config.policy("fail_open")
    human = subprocess.run([sys.executable, entry], input=json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": "selftest", "cwd": os.getcwd(),
         "prompt": "just me typing"}), capture_output=True, text=True, env=env)
    passed = (blocked if closed else noted) and not human.stdout.strip() \
        and out.returncode != 2 and human.returncode != 2
    print("peer message on a broken hook: %s" % (
        ("blocked (fail_open is off, as set)" if blocked else "PASSED THROUGH") if closed else
        "passed through with a note that it was not checked (good)" if noted else
        "BLOCKED, or passed with no note"))
    print("human prompt on a broken hook: %s" % ("passed (good)" if not human.stdout.strip()
                                                 else "BLOCKED"))
    return OK if passed else USAGE


def cmd_statusline(args) -> int:
    """xsm's line for Claude's statusLine, which runs on every render and calls
    no model. Deliberately cheap: pointer files plus a kill(pid, 0) each, no
    socket probes and no `ps`, so it costs nothing to show continuously.

    With --base, the user's own statusLine (kept at install) runs first with
    the same input and its output goes out unchanged; xsm adds one line under
    it and nothing else — a dashboard or Orca's line stays exactly as it was."""
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    if getattr(args, "base", None):
        _run_base_statusline(args.base, raw)
    try:
        rows = registry.cheap_records()
    except Exception:                              # a statusline must never break the UI
        print("xsm ?")
        return OK
    # Claude hands a statusLine command the session as JSON on stdin; fall back
    # to the environment when run by hand.
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    try:
        session_id = (json.loads(raw or "{}") or {}).get("session_id") or session_id
    except ValueError:
        pass
    me = next((r for r in rows if r.get("session_id") == session_id), None)
    others = [r for r in rows if r is not me]
    held = 0
    try:
        held = len([f for f in os.listdir(paths.path(paths.HELD)) if f.endswith(".json")])
    except OSError:
        pass
    parts = ["xsm %d peer%s" % (len(others), "" if len(others) == 1 else "s")]
    if me:
        parts.append("as %s" % (me.get("name") or "ref:%s" % me.get("ref")))
    if held:
        parts.append("%d held" % held)
    parts.extend(_worker_status(me))
    print(" · ".join(parts))
    return OK


def _run_base_statusline(settings_file: str, raw: str) -> None:
    """Print what the user's own statusLine prints. Its failure or slowness
    must not take xsm's line with it, and it must not print xsm's line twice."""
    try:
        original = (install.statusline_base(settings_file) or {}).get("command")
        if not original or install.MARKER in original:
            return
        import subprocess
        out = subprocess.run(["/bin/sh", "-c", original], input=raw, capture_output=True,
                             text=True, timeout=5).stdout
        if out.strip():
            print(out.rstrip("\n"))
    except Exception:                              # a statusline must never break the UI
        pass


def _worker_status(me: dict | None) -> list:
    """The workers this session started, for the statusline: a background one
    has no pane beside you, so this is where you see it is still there and
    whether it is waiting for you. kill(pid, 0) only — no `ps` on every render."""
    try:
        from . import identity
        mine = [w for w in workers.all_workers()
                if not me or w.get("parent_ref") == me.get("ref")]
        if not mine:
            return []
        waiting = {}
        for req in workers.approvals():
            waiting[req.get("worker")] = waiting.get(req.get("worker"), 0) + 1
        shown = []
        for w in mine:
            alive = bool(w.get("pid")) and identity.pid_alive(w["pid"])
            tags = [t for t in ("bg" if w.get("mode") == "background" else "",
                                "" if alive else "gone",
                                "%d asks" % waiting[w["name"]] if waiting.get(w["name"]) else "")
                    if t]
            shown.append("%s%s" % (w["name"], "(%s)" % ",".join(tags) if tags else ""))
        return ["workers " + " ".join(shown)]
    except Exception:                              # a statusline must never break the UI
        return ["workers ?"]


def cmd_spawn(args) -> int:
    caller = registry.me()
    if args.once and not args.task:
        print("refused: --once stops the worker when its answer to --task arrives, so it "
              "needs --task", file=sys.stderr)
        return USAGE
    if args.task and not caller:
        print("refused: --task needs a registered session to send it from and to report back "
              "to; run spawn from a session", file=sys.stderr)
        return REFUSED
    # The id is on record before the task leaves, so the answer can never
    # arrive ahead of it (a `once` worker stops only on that exact answer).
    task = {"id": envelope.new_id(), "text": args.task} if args.task else None
    try:
        worker = workers.spawn(args.runtime, name=args.name, model=args.model, effort=args.effort,
                               cwd=args.dir, home=args.home, once=args.once,
                               background=args.background,
                               approval_timeout=args.approval_timeout,
                               wait=args.wait, caller=caller, max_depth=args.max_depth,
                               full_access=args.full_access, trust_hooks=args.trust_hooks,
                               grant=args.grant, task=task, retry_of=args.retry_of)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    where = ("tmux pane %s" if worker["mode"] == "pane" else
             "background, tmux " + workers.BACKGROUND_SESSION + " %s") % worker.get("pane")
    print("started %s (%s, %s%s) [%s] in %s" % (
        worker["name"], worker["runtime"], where,
        ", model %s" % worker["model"] if worker.get("model") else "",
        worker.get("ref") or "not registered yet", worker["cwd"]))
    if worker.get("waiting"):
        return _spawn_waiting_for_trust(worker)
    if task:
        worker.pop("pending_task", None)
        worker["task_id"] = task["id"]
        workers.save(worker)
        result = send.send("ref:%s" % worker["ref"], task["text"], sender=caller, kind="task",
                           wait=args.task_wait, msg_id=task["id"])
        print("task %s: %s%s" % (result.msg_id or "-", result.status,
                                 ": " + result.reason if result.reason else ""))
    print("stop it: xsm stop %s" % worker["name"])
    if worker.get("once"):
        print("it stops by itself once its answer to the task reaches this session")
    return OK


def _spawn_waiting_for_trust(worker: dict) -> int:
    """The worker stopped at a folder-trust screen. A person at this terminal
    answers here; an agent is told to put the question to its user now."""
    req_id = worker["waiting"]
    question = "%s asks to trust %s" % (worker["name"], worker["cwd"])
    if workers.human_terminal():
        with open("/dev/tty", "w") as tty_out, open("/dev/tty") as tty_in:
            tty_out.write("%s. Trust it? Type yes to trust: " % question)
            tty_out.flush()
            yes = tty_in.readline().strip().lower() == "yes"
        workers.answer(req_id, yes, None if yes else "the person said no")
        print("%s; the worker %s" % ("trusted" if yes else "not trusted",
                                     "carries on" if yes else "is being stopped"))
        return OK if yes else REFUSED
    print("waiting: %s. Call the xsm_approve MCP tool with id %s now: it puts the question to "
          "your user. Do not decide it yourself. Once they answer, the worker %s on its own%s."
          % (question, req_id, "starts" , ", and its task goes to it" if worker.get("pending_task")
             else ""))
    return UNCONFIRMED


def cmd_worker_finish(args) -> int:
    return workers.finish(args.name)


def cmd_reap(args) -> int:
    if args.after_pid:
        from . import identity
        deadline = time.time() + 120
        while identity.pid_alive(args.after_pid) and time.time() < deadline:
            time.sleep(0.5)
    for name, why in workers.reap():
        print("stopped %s: %s" % (name, why))
    return OK


def cmd_workers(args) -> int:
    if getattr(args, "policy", False):
        print("what a background worker does without asking (PROTOCOL 5.5):")
        for name, rule in workers.WORKER_POLICY.items():
            print("  %-6s %s" % (name, rule))
        print("  claude tools: %s" % ", ".join(
            list(workers.CLAUDE_WORKER_TOOLS) + list(workers.CLAUDE_WORKER_MCP)))
        print("  codex:        -s workspace-write -a never, xsm store writable, MCP approved")
        return OK
    if getattr(args, "action", "list") == "read":
        return _cmd_workers_read(args)
    return _cmd_workers(args)


def _cmd_workers_read(args) -> int:
    """The worker's screen, for a caller that cannot go to its pane."""
    if not args.name:
        print("which worker? xsm workers read <name>", file=sys.stderr)
        return USAGE
    try:
        text = workers.screen(args.name, args.lines)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    print(text if text.strip() else "(no screen: the tmux pane is gone)")
    return OK


def _cmd_workers(args) -> int:
    for name, why in workers.reap():
        print("stopped %s: %s" % (name, why))
    rows = workers.all_workers()
    if not rows:
        print("no workers")
        return OK
    for w in rows:
        # A worker that has already answered its task says how it ended, so a
        # caller reading this list does not have to look the task up.
        done = ledger.status(w.get("task_id") or "").get("outcome") if w.get("task_id") else None
        print("%s [%s] %s %s %s %s depth %s/%s%s%s%s%s" % (
            w["name"], w.get("ref"), w["runtime"], w["mode"], workers.state(w),
            w.get("model") or "-", w.get("depth", 1), w.get("max_depth", 1),
            "  once" if w.get("once") else "", "  task %s" % done if done else "",
            "  FULL-ACCESS" if w.get("full_access") else "",
            "  hooks-untrusted" if w.get("trust_hooks") else ""))
    return OK


def cmd_attempts(args) -> int:
    """What has been tried and how it went. `clear` is a person's: an agent that
    could lift its own limit does not have one."""
    from . import attempts
    if args.action == "clear":
        if not args.key:
            print("which lineage? xsm attempts clear <key>", file=sys.stderr)
            return USAGE
        why = _person_or_refuse(
            "clearing the attempts of %s (tell them what failed and what the worker said it "
            "needed)" % args.key, "no", ask=("attempts-clear", args.key))
        if why:
            print("refused: %s" % why, file=sys.stderr)
            return REFUSED
        print("cleared %s" % args.key if attempts.clear(args.key) else "no such lineage %s"
              % args.key)
        return OK
    if args.action == "show":
        rec = attempts.read(args.key or "")
        if not rec:
            print("no such lineage", file=sys.stderr)
            return REFUSED
        print(json.dumps(rec, ensure_ascii=False, indent=1) if args.json else
              "\n".join(["%s  %s  %s" % (rec["key"], rec.get("cwd"), rec.get("text"))] +
                        ["  %s %s %s" % (time.strftime("%m-%d %H:%M", time.localtime(t.get("t", 0))),
                                         t.get("worker"), t.get("outcome") or "no answer")
                         for t in rec.get("tries") or []]))
        return OK
    rows = attempts.all_records()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return OK
    if not rows:
        print("nothing tried yet")
        return OK
    for rec in rows:
        failed = attempts.failures(rec["key"])
        print("%s  %d try/tries, %d failure(s) since the last success  %s" % (
            rec["key"], len(rec.get("tries") or []), failed,
            (rec.get("text") or "").replace("\n", " ")[:50]))
    return OK


def cmd_stop(args) -> int:
    try:
        if not args.internal:           # the hook's own once-stop is not a framework's call
            workers.refuse_inside_framework()
        worker = workers.stop(args.name)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    print("stopped %s and removed its records" % worker["name"])
    return OK


def cmd_attach(args) -> int:
    try:
        return workers.attach(args.name)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED


def cmd_approvals(args) -> int:
    rows = workers.approvals()
    if not rows:
        print("no approvals waiting")
        return OK
    for r in rows:
        print("[%s] %s asks: %s  (xsm approve %s | xsm deny %s)" % (
            r["id"], r["worker"], r["summary"], r["id"], r["id"]))
    return OK


def cmd_answer(args) -> int:
    approve = args.command == "approve"
    approver = None
    if approve:
        req = next((r for r in workers.approvals() if r["id"] == args.id), None)
        if req and not workers.human_terminal():
            # The agent asks and runs it; their reply is the decision (user
            # decision, 2026-10-01). Denying narrows, so anyone may. Only the
            # session that started the worker asks about it, as MCP
            # `xsm_approve` does (workers.answer_asked).
            caller_ref = (registry.me() or {}).get("ref")
            parent = (workers.load(req.get("worker") or "") or {}).get("parent_ref")
            if not parent:
                print("refused: request %s belongs to a worker no session started; it waits for "
                      "the person who started it" % req["id"], file=sys.stderr)
                return REFUSED
            if not caller_ref or caller_ref != parent:
                print("refused: request %s belongs to a worker another session started; only "
                      "that session asks its user about it" % req["id"], file=sys.stderr)
                return REFUSED
            why = _person_or_refuse("approving worker %s's request [%s]: %s" % (
                req["worker"], req["id"], req["summary"]), "xsm_approve",
                ask=("approve", req["id"]))
            if why:
                print("refused: %s" % why, file=sys.stderr)
                return REFUSED
            approver = _by()
        if req and workers.human_terminal():
            # Separate read and write handles: a tty opened "r+" in text mode
            # is not seekable and Python refuses it.
            with open("/dev/tty", "w") as tty_out, open("/dev/tty") as tty_in:
                tty_out.write("%s asks: %s\nType yes to approve: " % (req["worker"], req["summary"]))
                tty_out.flush()
                if tty_in.readline().strip().lower() != "yes":
                    print("not approved")
                    return REFUSED
    try:
        req = workers.answer(args.id, approve, args.reason, approver=approver)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    print("%s %s: %s" % (req["status"], req["id"], req["summary"]))
    return OK


def cmd_post(args) -> int:
    from . import channel
    me = registry.me()
    here = _here(args, me)
    try:
        where = channel.resolve(here, args.channel)
        author = channel.author_here(me)
        if args.tag == "decision" and author.get("kind") != "human":
            author = _decided_by_reply(me, "recording as a decision in %s: %s" % (
                where[0], (args.text or "")[:120]), "xsm_decide",
                ("decide", consent.digest(where[1], args.text)))
            if author is None:
                return REFUSED
        rec = channel.post(where, author, args.text, args.tag, args.reply_to)
    except channel.ChannelError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    print("posted %s to %s as %s" % (rec["id"], where[0], channel.label(author)))
    return OK


def _decided_by_reply(me: dict | None, what: str, mcp_tool: str, ask: tuple) -> dict | None:
    """A person's decision made through their reply (user decision,
    2026-10-01): the author is the person, with their words. None after
    printing the ask when there is no reply yet."""
    why = _person_or_refuse(what, mcp_tool, ask=ask)
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return None
    return {"kind": "human", "name": os.environ.get("USER") or "person", "via": "verdict",
            "verdict": _verdict, "asked_by": (me or {}).get("ref"),
            "runtime": (me or {}).get("runtime")}


def cmd_channel(args) -> int:
    from . import channel
    me = registry.me()
    if args.action == "list":
        here = _here(args, me)
        mine = {key for _, key in channel.memberships(here)}
        for name, key in channel.memberships(here):
            n = len(channel.read(key))
            print("%s  %d post(s)%s" % (name, n, "" if n else "  (empty)"))
        others = [c for c in channel.all_channels() if c[0] not in mine]
        if others:
            print("(%d other channel(s) this folder is not in)" % len(others))
        return OK
    try:
        where = channel.resolve(_here(args, me), args.channel)
    except channel.ChannelError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    rows = channel.read(where[1])
    if args.action == "export":
        text = channel.export_markdown(where[0], rows, args.tag or "decision")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("wrote %s (%d post(s)); review it, then commit it yourself"
                  % (args.out, len([r for r in rows if r.get("tag") == (args.tag or "decision")])))
        else:
            sys.stdout.write(text)
        return OK
    text = channel.render(rows, tag=args.tag, limit=args.limit)
    print(text or "(no posts in %s)" % where[0])
    return OK


def cmd_doc(args) -> int:
    from . import channel, doc
    try:
        if args.action == "add":
            body = open(args.file, encoding="utf-8").read() if args.file else (args.text or "")
            me = registry.me()
            author = channel.author_here(me)
            if "endorsed" in (args.tag or []) and author.get("kind") != "human":
                author = _decided_by_reply(me, "endorsing in %s: %s" % (args.doc, body[:120]),
                                           "xsm_doc_endorse",
                                           ("endorse", consent.digest(args.doc, body, args.parent)))
                if author is None:
                    return REFUSED
            node = doc.add(args.doc, author, body, args.tag or ["result"], args.parent or [],
                           approved=("verdict: %s" % author["verdict"])
                           if author.get("via") == "verdict" else None)
            print("added node %s [%s] to %s" % (node["id"], ", ".join(node["tags"]),
                                                 os.path.basename(doc.nodes_dir(args.doc))))
        elif args.action == "render":
            doc.render(args.doc)
            print("rendered %s from %d node(s)" % (args.doc, len(doc.read(args.doc))))
        elif args.action == "log":
            print(doc.log(doc.read(args.doc)) or "(no nodes)")
        elif args.action == "next":
            if args.json:
                print(json.dumps(doc.next_json(args.doc, args.limit), ensure_ascii=False, indent=1))
            else:
                print(doc.next_text(args.doc, args.limit))
        elif args.action == "leaves":
            print("\n".join(doc._one_line(n) for n in doc.leaves(doc.read(args.doc))) or "(no nodes)")
        elif args.action == "show":
            node = next((n for n in doc.read(args.doc) if n["id"] == args.node), None)
            if not node:
                raise doc.DocError("no node %s" % args.node)
            print(doc._serialize(node))
    except (doc.DocError, channel.ChannelError, OSError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    return OK


def cmd_remote(args) -> int:
    from . import remote
    try:
        if args.action == "add":
            if not workers.human_terminal():
                g = workers.use_grant(args.grant, registry.me(), "remote:%s" % args.host,
                                      _here(args), ["remote"])
            if not args.host or not args.project:
                raise remote.RemoteError("usage: xsm remote add <ssh host> --project <name> "
                                         "[--remote-project <name>] [--reach-me-as <name>]")
            entry = remote.add(args.host, args.project, args.remote_project, args.reach_me_as,
                               args.remote_xsm, here=_here(args))
            print("paired %s: project %s here <-> %s there; both directions reach"
                  % (entry["peer"], entry["local_project"], entry["remote_project"]))
        elif args.action == "accept":
            if not os.environ.get("SSH_CONNECTION"):
                raise remote.RemoteError("accept runs on the far side of `xsm remote add`, over SSH")
            print(json.dumps(remote.accept(args.peer, args.reach_as, args.project,
                                           args.remote_project, args.key)))
        elif args.action == "list":
            rows = remote.pairings()
            for p in rows:
                print("%s (ssh %s): %s here <-> %s there" % (p["peer"], p["host"],
                                                           p["local_project"], p["remote_project"]))
            if not rows:
                print("no paired remotes")
        elif args.action == "remove":
            done = remote.remove(args.host)
            print("removed %s: pairing %s, key %s, told peer %s" % (
                args.host, done["pairing"], done["key"], done["told_peer"]))
        elif args.action == "sessions":
            reply = remote.call(args.host, {"op": "sessions"})
            for s in reply.get("sessions") or []:
                print("%s@%s@%s [%s] %s" % (s["name"], s["alias"], args.host, s["ref"], s["runtime"]))
            if not reply.get("sessions"):
                print("no live sessions in the paired project on %s" % args.host)
    except (remote.RemoteError, workers.WorkerError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    return OK


def cmd_mcp(args) -> int:
    from . import mcp
    return mcp.main()


def cmd_prune(args) -> int:
    removed = housekeeping.prune(dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    lines = removed.get("telemetry") or {}
    print("%s %d session pointer(s), %d ledger record(s), %d held message(s), %d inbox copy(ies), "
          "%d telemetry line(s), %d ask file(s), %d held send(s)" % (
              verb, len(removed["sessions"]), len(removed["ledger"]), len(removed["held"]),
              len(removed.get("inbox") or []), sum(lines.values()),
              len(removed.get("asked") or []), len(removed.get("outbox") or [])))
    for name in removed["sessions"]:
        print("  session %s" % name)
    for name, count in sorted(lines.items()):
        print("  %s: %d line(s) already exported" % (name, count))
    return OK


class _Version(argparse.Action):
    """`xsm --version`: which xsm this is and where it runs from. Git is asked
    here, not on every command."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS,
                         help="the version, the git describe of a checkout, and this CLI's path")

    def __call__(self, parser, namespace, values, option_string=None):
        print(install.cli_text(install.cli_info()))
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xsm", description="cross-session messaging")
    p.add_argument("--version", action=_Version)
    p.add_argument("--xsm-home", help="state directory (default ~/.xsm or $XSM_HOME)")
    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("list", help="registered sessions")
    ls.add_argument("action", nargs="?", choices=["clear"],
                    help="clear: forget stopped (ended/stale) sessions now; with -a in every project")
    ls.add_argument("-a", "--all", action="store_true",
                    help="every project, plus stopped and unregistered sessions "
                         "(default: live sessions this folder can talk to)")
    ls.add_argument("--dir", help="list for this folder instead of this session's")
    ls.add_argument("--runtime", choices=["claude", "codex"])
    ls.add_argument("--home", help="alias or path")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--compact", action="store_true", help="short lines, no padding (for agents)")
    ls.add_argument("--table", action="store_true",
                    help="a Markdown table, for a session's TUI to draw (`/xsm <command>` uses it)")
    ls.set_defaults(func=cmd_list)

    who = sub.add_parser("who", help="identity of the session running this command")
    who.add_argument("--table", action="store_true", help="a Markdown table (for a session's TUI)")
    who.add_argument("--json", action="store_true")
    who.set_defaults(func=cmd_who)

    for verb, helptext, func in (
            ("join", "put this project in a named xsm project (both sides must join)", cmd_join),
            ("leave", "take this project out of a named xsm project", cmd_leave)):
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("project")
        sp.add_argument("--dir", help="the folder to speak for (default: this session's)")
        sp.set_defaults(func=func)
    sp = sub.add_parser("spawn", help="start a worker session, optionally with a task")
    sp.add_argument("runtime", choices=["claude", "codex"])
    sp.add_argument("--name")
    sp.add_argument("--model")
    sp.add_argument("--effort", help="reasoning effort (claude --effort, codex model_reasoning_effort)")
    sp.add_argument("--dir", help="working folder (default: this session's)")
    sp.add_argument("--home", help="CONFIG_DIR / CODEX_HOME (default: this session's, else env)")
    sp.add_argument("--task", help="send this as a task once the worker is up")
    sp.add_argument("--task-wait", type=float, default=30.0)
    sp.add_argument("--once", action="store_true", help="stop the worker when its answer arrives")
    sp.add_argument("--background", action="store_true",
                    help="run in the detached tmux session %s even from inside tmux"
                    % workers.BACKGROUND_SESSION)
    sp.add_argument("--approval-timeout", type=int, default=workers.APPROVAL_TIMEOUT)
    sp.add_argument("--wait", type=float, default=90.0, help="seconds to wait for it to register")
    sp.add_argument("--full-access", action="store_true",
                    help="no sandbox, no approvals (Codex --dangerously-bypass-approvals-and-sandbox, "
                         "Claude bypassPermissions); needs --grant from an agent")
    sp.add_argument("--trust-hooks", action="store_true",
                    help="Codex pane worker: --dangerously-bypass-hook-trust; needs --grant from an agent")
    sp.add_argument("--grant", help="the id xsm_grant returned after your user allowed it")
    sp.add_argument("--retry-of", help="the task id this one tries again: it joins that task's "
                    "lineage even if you reworded it")
    sp.add_argument("--max-depth", type=int, help="worker levels this worker's subtree may use "
                    "(default: XSM_MAX_DEPTH or config max_depth, 1; a worker can only lower it)")
    sp.set_defaults(func=cmd_spawn)
    wk = sub.add_parser("workers", help="workers xsm started, and what one's screen says")
    wk.add_argument("action", nargs="?", default="list", choices=["list", "read"])
    wk.add_argument("name", nargs="?", help="the worker to read")
    wk.add_argument("--lines", type=int, default=80,
                    help="how far back into its screen to read (max %d)" % workers.MAX_READ_LINES)
    wk.add_argument("--policy", action="store_true",
                    help="what a background worker may do without asking")
    wk.set_defaults(func=cmd_workers)
    rp = sub.add_parser("reap", help=argparse.SUPPRESS)
    rp.add_argument("--after-pid", type=int)
    rp.set_defaults(func=cmd_reap)
    for verb, helptext, func in (("stop", "stop a worker and remove its records", cmd_stop),
                                 ("attach", "go to a worker's tmux pane", cmd_attach),
                                 ("worker-finish", argparse.SUPPRESS, cmd_worker_finish)):
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("name")
        if verb == "stop":
            sp.add_argument("--internal", action="store_true", help=argparse.SUPPRESS)
        sp.set_defaults(func=func)
    po = sub.add_parser("post", help="post to this project's channel (the shared record)")
    po.add_argument("text")
    po.add_argument("--tag", default="note", help="note, question, proposal, result, hypothesis, "
                    "decision (your user's)")
    po.add_argument("--reply-to")
    po.add_argument("--channel", help="a named project (default: this project)")
    po.add_argument("--dir")
    po.set_defaults(func=cmd_post)
    ch = sub.add_parser("channel", help="read, list or export channels")
    ch.add_argument("action", nargs="?", default="show", choices=["show", "list", "export"])
    ch.add_argument("--channel")
    ch.add_argument("--tag")
    ch.add_argument("--limit", type=int)
    ch.add_argument("--out", help="export: write the markdown here")
    ch.add_argument("--dir")
    ch.set_defaults(func=cmd_channel)
    dc = sub.add_parser("doc", help="shared documents as immutable nodes (add, render, log, "
                        "next, leaves, show)")
    dc.add_argument("action", choices=["add", "render", "log", "next", "leaves", "show"])
    dc.add_argument("doc", help="the document, e.g. docs/research/cache.md")
    dc.add_argument("node", nargs="?", help="show: the node id")
    dc.add_argument("--tag", action="append", help="setup, result, insight, hypothesis, "
                    "verification, report, wip; endorsed is a person's")
    dc.add_argument("--parent", action="append", help="a node this builds on or revises")
    dc.add_argument("--text")
    dc.add_argument("--file")
    dc.add_argument("--json", action="store_true", help="next: the candidates as JSON")
    dc.add_argument("--limit", type=int, default=0, help="next: show at most this many")
    dc.set_defaults(func=cmd_doc)
    rm = sub.add_parser("remote", help="pair with another machine over two-way SSH (add, list, "
                        "remove, sessions)")
    rm.add_argument("action", choices=["add", "accept", "list", "remove", "sessions"])
    rm.add_argument("host", nargs="?", help="ssh host (add), or the paired peer (remove, sessions)")
    rm.add_argument("--project", help="the project here to pair")
    rm.add_argument("--remote-project", help="the project there (default: same name)")
    rm.add_argument("--reach-me-as", help="the name the other machine uses to ssh back here")
    rm.add_argument("--remote-xsm", help="path of bin/xsm on the other machine (default: same as here)")
    rm.add_argument("--grant", help="from an agent: the id xsm_grant returned")
    rm.add_argument("--peer")
    rm.add_argument("--reach-as")
    rm.add_argument("--key")
    rm.set_defaults(func=cmd_remote)
    mc = sub.add_parser("mcp", help=argparse.SUPPRESS)
    mc.set_defaults(func=cmd_mcp)
    ap = sub.add_parser("approvals", help="permission requests waiting for a person")
    ap.set_defaults(func=cmd_approvals)
    for verb in ("approve", "deny"):
        sp = sub.add_parser(verb, help="%s a worker's permission request (approve is your "
                            "user's decision)" % verb)
        sp.add_argument("id")
        sp.add_argument("--reason")
        sp.set_defaults(func=cmd_answer)

    for verb, helptext in (
            ("link", "link this project folder with another, both ways, until unlinked (your user "
                     "decides); no folder: list links"),
            ("unlink", "take a link away (anyone)")):
        lk = sub.add_parser(verb, help=helptext)
        if verb == "link":
            lk.add_argument("folder", nargs="?")
        else:
            lk.add_argument("folder")
        lk.add_argument("--dir", help="this side's folder (default: this session's)")
        lk.set_defaults(func=cmd_link)
    rc = sub.add_parser("reach", help="let one session talk with the sessions of another folder "
                                      "while it runs (your user decides); no folder: list them")
    rc.add_argument("folder", nargs="?")
    rc.add_argument("--session", help="the session to allow (default: the one running this)")
    rc.add_argument("--drop", action="store_true", help="take the reach away (all, if no folder)")
    rc.set_defaults(func=cmd_reach)
    for verb, helptext in (("block", "stop one session from sending or receiving"),
                           ("unblock", "lift a block (your user decides)")):
        bp = sub.add_parser(verb, help=helptext)
        bp.add_argument("ref")
        bp.set_defaults(func=cmd_block)
    at = sub.add_parser("attempts", help="how often a task has been handed to a worker, and "
                                         "how it went (clear is your user's decision)")
    at.add_argument("action", nargs="?", default="list", choices=["list", "show", "clear"])
    at.add_argument("key", nargs="?")
    at.add_argument("--json", action="store_true")
    at.set_defaults(func=cmd_attempts)
    fw = sub.add_parser("frameworks", help="whether xsm starts workers inside Orca or herdr "
                                           "(ignore is your user's decision)")
    fw.add_argument("action", nargs="?", default="list", choices=["list", "ignore", "respect"])
    fw.add_argument("name", nargs="?")
    fw.set_defaults(func=cmd_frameworks)
    pj = sub.add_parser("projects", help="named xsm projects and their member folders")
    pj.add_argument("--table", action="store_true", help="a Markdown table (for a session's TUI)")
    pj.add_argument("--dir", help="mark membership relative to this folder")
    pj.set_defaults(func=cmd_projects)

    homes = sub.add_parser("homes", help="declared CONFIG_DIR / CODEX_HOME list")
    homes.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    homes.add_argument("path", nargs="?")
    homes.add_argument("--runtime", choices=["claude", "codex"])
    homes.add_argument("--alias")
    homes.set_defaults(func=cmd_homes)

    prune = sub.add_parser("prune", help="remove what has outlived its retention window "
                                          "(also runs on its own at most hourly)")
    prune.add_argument("--dry-run", action="store_true")
    prune.set_defaults(func=cmd_prune)

    snd = sub.add_parser("send", help="send a message to another session")
    snd.add_argument("target", nargs="?",
                     help="name, name@home, name [ref], ref:xxxxxx, claude:ID, codex:ID")
    snd.add_argument("--text")
    snd.add_argument("--text-file", help="file path, or - for stdin")
    snd.add_argument("--kind", choices=list(envelope.KINDS), default="note")
    snd.add_argument("--reply-to", help="message id being answered")
    snd.add_argument("--outcome", choices=list(envelope.OUTCOMES),
                     help="on a reply that closes a task: how the task ended")
    snd.add_argument("--resend", metavar="ID",
                     help="send the message with this id again, unchanged: same target, same text "
                          "and kind, and only while it is queued, unknown or error. The receiver "
                          "drops an id it already has, so it cannot run twice")
    snd.add_argument("--held", metavar="ID",
                     help="send the message a refused send kept under this id (out of scope: it "
                          "waits for your user's yes to connect, then goes with it)")
    snd.add_argument("--priority", choices=["next", "now", "later"], default="next")
    snd.add_argument("--wait", type=float, default=0.0,
                     help="seconds to wait for the receiver's own record of delivery")
    snd.add_argument("--json", action="store_true")
    snd.set_defaults(func=cmd_send)

    ib = sub.add_parser("inbox", help="messages waiting for this Codex session, read mid-turn")
    ib.add_argument("--wait", type=float, default=0.0,
                    help="block until a message arrives, at most this many seconds (max %d); "
                         "returns the moment one does, and 0 either way" % inbox.MAX_WAIT)
    ib.set_defaults(func=cmd_inbox)

    st = sub.add_parser("status", help="delivery state of one message")
    st.add_argument("msg_id")
    st.add_argument("--wait", type=float, default=0.0)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    lg = sub.add_parser("ledger", help="recent messages and their delivery state")
    lg.add_argument("--table", action="store_true", help="a Markdown table (for a session's TUI)")
    lg.add_argument("--last", type=int, default=20)
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--compact", action="store_true")
    lg.add_argument("--mine", action="store_true",
                    help="only messages to or from the session running this")
    lg.set_defaults(func=cmd_ledger)

    mt = sub.add_parser("metrics", help="what xsm's own telemetry has recorded here")
    mt.add_argument("--json", action="store_true")
    mt.set_defaults(func=cmd_metrics)

    ox = sub.add_parser("otlp-export", help="send that telemetry to an OTLP collector "
                                            "(OTEL_EXPORTER_OTLP_ENDPOINT)")
    ox.add_argument("--endpoint", help="OTLP/HTTP base url; default http://localhost:4318")
    mode = ox.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="send what is pending and exit (default)")
    mode.add_argument("--follow", action="store_true",
                      help="keep sending every --interval seconds until interrupted")
    ox.add_argument("--interval", type=float, default=5.0)
    ox.add_argument("--json", action="store_true")
    ox.set_defaults(func=cmd_otlp_export)

    hd = sub.add_parser("held", help="messages this machine refused and kept")
    hd.add_argument("--table", action="store_true", help="a Markdown table (for a session's TUI)")
    hd.add_argument("action", nargs="?", default="list",
                    choices=["list", "show", "drop", "deliver"],
                    help="deliver: hand one to this session, on your user's yes")
    hd.add_argument("id", nargs="?")
    hd.set_defaults(func=cmd_held)

    ins = sub.add_parser("install", help="add the xsm hooks to a home (merges, never overwrites)")
    ins.add_argument("--claude-home", action="append")
    ins.add_argument("--codex-home", action="append")
    ins.add_argument("--python", help="interpreter for the hooks: an absolute path, or a version "
                                     "like 3.12 resolved via `uv python find`")
    ins.add_argument("--dry-run", action="store_true")
    ins.add_argument("--statusline", action="store_true",
                     help="also show peers in Claude's statusLine (never replaces an existing one)")
    ins.add_argument("--no-commands", action="store_true",
                     help="hooks only: do not link the skill")
    ins.add_argument("--no-mcp", action="store_true", help="do not register the xsm MCP server")
    ins.add_argument("--refresh", action="store_true",
                     help="re-write what xsm installed in every home it knows")
    ins.add_argument("--force", action="store_true",
                     help="install into a home that already has the xsm plugin")
    ins.add_argument("--dev", action="store_true",
                     help="run the hooks, the MCP server and `xsm` from this checkout instead of a "
                          "copy under ~/.xsm/runtime (for developing xsm; same as the policy "
                          "runtime=checkout)")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall", help="remove only the hook groups xsm added")
    un.add_argument("--claude-home", action="append")
    un.add_argument("--codex-home", action="append")
    un.set_defaults(func=cmd_uninstall)

    doc = sub.add_parser("doctor", help="what is installed, what is running, what is not covered")
    doc.add_argument("--table", action="store_true", help="a Markdown table (for a session's TUI)")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=cmd_doctor)

    sl = sub.add_parser("statusline", help="one line for Claude's statusLine (no model call)")
    sl.add_argument("--base", help=argparse.SUPPRESS)      # the settings file whose own
                                                            # statusLine runs first
    sl.set_defaults(func=cmd_statusline)

    stest = sub.add_parser("selftest", help="check what the hook does with a peer message when it "
                                           "breaks")
    stest.set_defaults(func=cmd_selftest)
    for name in ASKING:
        sub.choices[name].add_argument(
            "--reply", metavar="TEXT",
            help="your user's answer, in their own words, when xsm's hook did not record it "
                 "(it is shown to you first, as a recorded one is)")
    return p


# The commands that ask your user, and so take their words with --reply when the
# hook that keeps replies did not (consent.supplied).
ASKING = ("join", "leave", "spawn", "post", "doc", "remote", "approve", "link", "reach", "unblock",
          "attempts", "frameworks", "send", "held")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.xsm_home:
        os.environ["XSM_HOME"] = os.path.expanduser(args.xsm_home)
        paths.HOME = os.environ["XSM_HOME"]
    try:
        paths.ensure_home()
    except OSError as err:
        # A sandboxed or read-only shell cannot make the state folder, and every
        # command died here with a traceback (audit, 2026-10-01). What only
        # reads still works; what writes says why, below.
        if not paths.blocked_write(err):
            raise
    consent.supplied = consent.used_via = None
    if getattr(args, "reply", None):
        if config.policy("reply_flag"):
            consent.supplied = args.reply
        else:
            print("xsm: --reply is switched off here (reply_flag), so it is ignored",
                  file=sys.stderr)
    if args.command not in ("hook", "statusline", "prune", "reap", "mcp", "worker-finish"):
        housekeeping.maybe_prune()
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    # Long-running (mcp) or redrawn on every prompt (statusline): a span
    # each would be noise, or would never close. And the two that read the
    # telemetry itself: each export would leave a span for the next export to
    # ship, and metrics would count its own calls.
    try:
        if telemetry is None or args.command in ("mcp", "statusline",
                                                 "metrics", "otlp-export"):
            code = args.func(args)
        else:
            code = _traced(telemetry, args)
    except OSError as err:
        if not paths.blocked_write(err):
            raise
        print(paths.sandbox_blocked(err), file=sys.stderr)
        return REFUSED
    _inbox_notice(args.command)
    return code


def _inbox_notice(command: str) -> None:
    """A Codex session mid-turn does not know anything arrived; every xsm
    command it runs tells it. On stderr, so `--json` output stays parseable."""
    if command in ("hook", "mcp", "statusline", "worker-finish"):
        return
    thread = os.environ.get("CODEX_THREAD_ID")
    text = inbox.notice(thread) if thread and command != "inbox" else ""
    # Either runtime's session: a Claude session learns here that a
    # SendMessage it sent was held on the other side (issue #8).
    from . import bounce
    held = bounce.notice(thread or os.environ.get("CLAUDE_CODE_SESSION_ID"))
    for line in (text, held):
        if line:
            print(line, file=sys.stderr)


class _StderrTail:
    """Passes stderr through and keeps its last line: a refusal's reason is
    printed there, and it is what a span of a failed command should say."""

    def __init__(self, inner):
        self.inner, self.tail = inner, ""

    def write(self, text):
        line = text.strip()
        if line:
            self.tail = line.splitlines()[-1][:300]
        return self.inner.write(text)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _traced(telemetry, args) -> int:
    """One span per command. Before this only send and receive were traced,
    and the S10 pilot ran three sessions for half an hour on doc, list and
    post — several hundred calls, some of them refused, and not one span."""
    attrs = {"xsm.cli.command": args.command}
    if isinstance(getattr(args, "action", None), str):
        attrs["xsm.cli.action"] = args.action
    with telemetry.span("xsm.cli.%s" % args.command, attrs) as span:
        tail = _StderrTail(sys.stderr)
        sys.stderr = tail
        try:
            code = args.func(args)
        finally:
            sys.stderr = tail.inner
        if span is not None:
            span.set_attribute("xsm.cli.exit", code)
            if code:
                span.set_status("ERROR", tail.tail or "exit %s" % code)
        return code
