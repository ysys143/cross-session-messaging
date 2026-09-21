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
import sys

from . import config, envelope, housekeeping, install, ledger, paths, registry, resolve, send

OK, REFUSED, UNCONFIRMED, USAGE = 0, 2, 3, 4


def _rows(args) -> list:
    rows = registry.records()
    if getattr(args, "all", False):
        rows += registry.unregistered()
    if getattr(args, "runtime", None):
        rows = [r for r in rows if r.get("runtime") == args.runtime]
    if getattr(args, "home", None):
        rows = [r for r in rows if args.home in (r.get("alias"), r.get("home"))]
    if not getattr(args, "all", False):
        rows = [r for r in rows if r.get("state") != "stale"]
    return rows


def cmd_list(args) -> int:
    registry.adopt_open_codex()
    me = registry.me()
    rows = _rows(args)
    for row in rows:
        row["inbound"] = registry.inbound_setting(row.get("home", "")) if \
            row.get("runtime") == "claude" else None
        row["native"] = send.native_forecast(me, row)[0] if me and row.get("ref") != me.get("ref") \
            else "n/a"
        scope, reason = (None, "self") if me and row.get("ref") == me.get("ref") \
            else config.scope_for(me, row) if me else (None, "no registered session here")
        row["scope"] = scope
        row["scope_reason"] = reason
        row["addressable"] = bool(row.get("registered") and row.get("state") == "live" and
                                  (scope or reason == "self"))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return OK
    if not rows:
        print("no sessions registered. Install the hooks first: xsm install --help")
        return OK
    if args.compact:
        # For a model to copy back verbatim: no alignment padding (every space
        # is a token), home shortened to ~, and only the flags that change
        # whether a message would arrive.
        home = os.path.expanduser("~")
        for r in rows:
            flags = [f for f in (
                "you" if me and r.get("ref") == me.get("ref") else "",
                "" if r.get("registered") else "unregistered",
                "out-of-scope" if me and not r.get("scope") and r.get("scope_reason") != "self" else "",
                "would-be-held" if r.get("native") == "hold" else "",
                r.get("state") if r.get("state") != "live" else "") if f]
            cwd = (r.get("cwd") or "").replace(home, "~", 1)
            if r.get("why"):
                flags.append(r["why"])
            print("%s@%s [%s] %s%s" % (r.get("name"), r.get("alias"), r.get("ref"), cwd,
                                       (" (" + ", ".join(flags) + ")") if flags else ""))
        return OK
    if not me:
        # Run from a plain terminal there is no "us" to be in scope with, and
        # saying "out-of-scope" about every row would read as a verdict.
        print("(this terminal is not a registered session, so scope is not shown)")
    width = max(len("%s@%s" % (r.get("name"), r.get("alias"))) for r in rows)
    for r in rows:
        addr = "%s@%s" % (r.get("name"), r.get("alias"))
        flags = [] if r.get("registered") else ["unregistered"]
        if me and not r.get("scope") and r.get("scope_reason") != "self":
            flags.append("out-of-scope")
        if me and r.get("ref") == me.get("ref"):
            flags.append("you")
        if r.get("native") == "hold":
            flags.append("would be held")
        print("%-*s  [%s]  %-6s %-7s %-9s %s%s" % (
            width, addr, r.get("ref"), r.get("runtime"), r.get("state"),
            r.get("permission_mode") or "mode?", r.get("cwd") or "",
            ("  (" + ", ".join(flags) + ")") if flags else ""))
    return OK


def cmd_who(args) -> int:
    me = registry.me()
    if not me:
        print("this session is not registered (no hook record for this cwd)", file=sys.stderr)
        return REFUSED
    if args.json:
        print(json.dumps(me, ensure_ascii=False, indent=1))
        return OK
    print("%s@%s [%s] %s %s" % (me["name"], me["alias"], me["ref"],
                                me["runtime"], me.get("cwd") or ""))
    if me.get("name_source") and me["name_source"] != "user":
        print("this name was %s, not chosen by your user; it can change. "
              "The stable address is ref:%s" % (me["name_source"], me["ref"]))
    return OK


def _here(args) -> str:
    """The folder a project command speaks for: --dir, else the session running
    it (its own cwd, which the shell may have left), else this shell."""
    if getattr(args, "dir", None):
        return os.path.realpath(os.path.expanduser(args.dir))
    me = registry.me()
    return (me and me.get("cwd")) or os.getcwd()


def _home_tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path.startswith(home) else path


def _print_members(scope: dict, root: str) -> None:
    for m in scope.get("members", []):
        mine = os.path.realpath(m.get("root", "")) == root
        print("  %s%s" % (_home_tilde(m.get("root", "")), "  (this project)" if mine else ""))


def cmd_join(args) -> int:
    here = _here(args)
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
        print("no other project has joined %s yet. In a session there, run: /xsm-join %s"
              % (args.project, args.project))
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
    here = _here(args)
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


def cmd_projects(args) -> int:
    here = _here(args)
    root = config.project_root(here)
    print("this folder (%s) is in: %s" % (_home_tilde(root), ", ".join(_memberships(here))))
    rows = config.projects()
    if not rows:
        print("no named projects. Join one with: /xsm-join <name>  (or xsm join <name>)")
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
    if args.text_file:
        body = sys.stdin.read() if args.text_file == "-" else open(args.text_file).read()
    if not body:
        print("nothing to send: pass --text or --text-file", file=sys.stderr)
        return USAGE
    result = send.send(args.target, body, kind=args.kind, reply_to=args.reply_to,
                       priority=args.priority, wait=args.wait)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False))
    else:
        print("%s: %s" % (result.status, result.reason or result.msg_id or ""))
        if result.candidates and "resume it with" not in (result.reason or "") \
                and "has not registered" not in (result.reason or ""):
            print("registered sessions right now:" if "no session" in (result.reason or "")
                  else "candidates:")
            print(resolve.describe(result.candidates))
    return {"delivered": OK, "sent-unconfirmed": UNCONFIRMED, "held": REFUSED,
            "blocked": REFUSED, "refused": REFUSED}.get(result.status, USAGE)


def cmd_status(args) -> int:
    state = ledger.wait_for(args.msg_id, args.wait) if args.wait else ledger.status(args.msg_id)
    if not state:
        print("no such message", file=sys.stderr)
        return REFUSED
    print(json.dumps(state, ensure_ascii=False, indent=1) if args.json
          else "%s  %s -> %s  %s" % (state.get("status"), (state.get("from") or {}).get("name"),
                                     (state.get("to") or {}).get("name"), state.get("id")))
    return OK if state.get("status") == "delivered" else UNCONFIRMED


def _mark_undelivered(rows: list) -> list:
    """A message still `queued` whose target has since stopped will never be
    recorded as delivered. Say so instead of leaving it looking pending."""
    live = {r.get("ref") for r in registry.records() if r.get("state") == "live"}
    for row in rows:
        if row.get("status") == "queued" and (row.get("to") or {}).get("ref") not in live:
            row["status"] = "undelivered"
            row["note"] = "target stopped before recording it"
    return rows


def cmd_ledger(args) -> int:
    rows = _mark_undelivered(ledger.recent(args.last))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return OK
    if args.compact:
        for row in rows:
            print("%s %s->%s %s: %s" % (row.get("status"), (row.get("from") or {}).get("name"),
                                        (row.get("to") or {}).get("name"), row.get("id"),
                                        (row.get("preview") or "").replace("\n", " ")[:40]))
        if not rows:
            print("no messages")
        return OK
    for row in rows:
        print("%-16s %-17s %s -> %s  %s" % (
            row.get("id"), row.get("status"), (row.get("from") or {}).get("name"),
            (row.get("to") or {}).get("name"), (row.get("preview") or "").replace("\n", " ")[:50]))
    return OK


def cmd_held(args) -> int:
    entries = sorted(glob.glob(paths.path(paths.HELD, "*.json")))
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
    targets = [(h, "claude") for h in (args.claude_home or [])] + \
              [(h, "codex") for h in (args.codex_home or [])]
    if not targets:
        print("name at least one home: --claude-home ~/.claude-3 --codex-home ~/.codex",
              file=sys.stderr)
        return USAGE
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
    failed = False
    for home, runtime in targets:
        if args.dry_run:
            print(install.diff(home, runtime))
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
        if runtime == "claude" and not args.no_commands:
            written = install.install_commands(home)
            state, detail = install.install_skill(home)
            print("  slash commands: %s" % ", ".join(
                "/" + os.path.basename(w)[:-3] for w in written) if written else
                "  slash commands: none written")
            notes = {
                "linked": "linked to the repo",
                "copy-current": "a copy is in place and matches the repo",
                "copy-stale": "a copy has fallen behind; refresh it with\n"
                              "           cp %s %s" % (
                                  os.path.join(install.REPO, "skills", "xsm", "SKILL.md"),
                                  detail),
                "nested-link": "a link sits inside the existing directory (%s);\n"
                               "           remove it: rm %s" % (detail, detail),
                "foreign": "something else is at skills/xsm; left alone",
            }
            print("  skill: %s" % notes.get(state, state))
        if runtime == "claude" and args.statusline:
            outcome = install.install_statusline(home)
            print("  statusLine: %s" % {
                "installed": "set to `xsm statusline` (no model call)",
                "already": "already set",
                "kept-existing": "left alone: this home already has its own statusLine",
            }[outcome])
        if runtime == "codex":
            if not args.no_commands:
                state, _ = install.install_skill(home)
                print("  skill: %s" % {"linked": "linked to the repo",
                                        "copy-current": "a copy is in place and matches the repo",
                                        "copy-stale": "a copy has fallen behind the repo",
                                        "nested-link": "a link sits inside the existing directory",
                                        "foreign": "something else is at skills/xsm; left alone"
                                        }.get(state, state))
            print("  Codex asks you to trust hooks once, at the next session start. "
                  "Until you do, the hook does not run. Codex has no SessionEnd, so a "
                  "stopped Codex session always reads as stale.")
    return USAGE if failed else OK


def cmd_uninstall(args) -> int:
    targets = [(h, "claude") for h in (args.claude_home or [])] + \
              [(h, "codex") for h in (args.codex_home or [])]
    for home, runtime in targets or [(h["path"], h["runtime"]) for h in config.homes()]:
        result = install.remove(home, runtime)
        if runtime == "codex" and install.remove_skill(home):
            print("%s: unlinked the skill" % home)
        if runtime == "claude":
            gone = install.remove_commands(home)
            if gone:
                print("%s: removed %d slash command file(s)" % (home, gone))
            if install.remove_skill(home):
                print("%s: unlinked the skill" % home)
            if install.remove_statusline(home):
                print("%s: removed the xsm statusLine" % home)
        print("%s: removed %s xsm hook group(s)%s" % (
            result.get("file"), result.get("removed", 0),
            "" if not result.get("error") else " (%s)" % result["error"]))
    return OK


def cmd_doctor(args) -> int:
    report = install.doctor()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return OK
    print("state      %s" % report["xsm_home"])
    print("python     %s%s" % (report["interpreter"], "" if report["interpreter_ok"] else "  TOO OLD"))
    print("codex      %s" % (report["codex_binary"] or "not found on PATH"))
    print("sessions   %(registered)d registered, %(live)d live, %(unregistered)d unregistered"
          % report["sessions"])
    print("hooks      %d decision(s) recorded, %d internal error(s)"
          % (report["decisions_seen"], report["hook_errors_recent"]))
    print("held       %d message(s)" % report["held"])
    for plan in report["installs"]:
        if plan.get("error"):
            print("install    %s: %s" % (plan["file"], plan["error"]))
            continue
        states = ", ".join("%s:%s" % (a["event"], a["action"]) for a in plan["actions"])
        print("install    %-45s %s" % (plan["file"], states))
    for home, trust in (report.get("codex_trust") or {}).items():
        if not trust:
            print("codex      %s: xsm hooks not installed" % home)
            continue
        missing = [e for e, ok in trust.items() if not ok]
        print("codex      %s: hooks %s" % (home, "trusted" if not missing else
              "NOT trusted for %s — start codex there and choose 'Trust all and continue'"
              % ", ".join(missing)))
    for note in report["limits"]:
        print("limit      %s" % note)
    return OK


def cmd_selftest(args) -> int:
    """Prove the hook still refuses a peer message when its own code breaks."""
    import subprocess
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "hooks", "xsm-hook.py")
    probe = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "selftest",
                        "cwd": os.getcwd(), "prompt": "<cross-session-message from-mode=\"bypass\">\n"
                                                      "[xsm v1 id=selftest]\nhi\n</cross-session-message>"})
    env = dict(os.environ, XSM_FORCE_ERROR="1")
    out = subprocess.run([sys.executable, entry], input=probe, capture_output=True, text=True, env=env)
    blocked = '"decision": "block"' in out.stdout
    human = subprocess.run([sys.executable, entry], input=json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": "selftest", "cwd": os.getcwd(),
         "prompt": "just me typing"}), capture_output=True, text=True, env=env)
    passed = blocked and not human.stdout.strip()
    print("peer message on a broken hook: %s" % ("blocked (good)" if blocked else "PASSED THROUGH"))
    print("human prompt on a broken hook: %s" % ("passed (good)" if not human.stdout.strip()
                                                 else "BLOCKED"))
    return OK if passed else USAGE


def cmd_statusline(args) -> int:
    """One short line for Claude's statusLine, which runs on every render and
    calls no model. Deliberately cheap: pointer files plus a kill(pid, 0) each,
    no socket probes and no `ps`, so it costs nothing to show continuously."""
    try:
        rows = registry.cheap_records()
    except Exception:                              # a statusline must never break the UI
        print("xsm ?")
        return OK
    # Claude hands a statusLine command the session as JSON on stdin; fall back
    # to the environment when run by hand.
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sys.stdin.isatty():
        try:
            session_id = (json.loads(sys.stdin.read() or "{}") or {}).get("session_id") or session_id
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
        parts.append("as %s" % me.get("name"))
    if held:
        parts.append("%d held" % held)
    print(" · ".join(parts))
    return OK


def cmd_prune(args) -> int:
    removed = housekeeping.prune(dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print("%s %d session pointer(s), %d ledger record(s), %d held message(s)" % (
        verb, len(removed["sessions"]), len(removed["ledger"]), len(removed["held"])))
    for name in removed["sessions"]:
        print("  session %s" % name)
    return OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xsm", description="cross-session messaging")
    p.add_argument("--xsm-home", help="state directory (default ~/.xsm or $XSM_HOME)")
    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("list", help="registered sessions")
    ls.add_argument("--all", action="store_true", help="include stale and unregistered")
    ls.add_argument("--runtime", choices=["claude", "codex"])
    ls.add_argument("--home", help="alias or path")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--compact", action="store_true", help="short lines, no padding (for agents)")
    ls.set_defaults(func=cmd_list)

    who = sub.add_parser("who", help="identity of the session running this command")
    who.add_argument("--json", action="store_true")
    who.set_defaults(func=cmd_who)

    for verb, helptext, func in (
            ("join", "put this project in a named xsm project (both sides must join)", cmd_join),
            ("leave", "take this project out of a named xsm project", cmd_leave)):
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("project")
        sp.add_argument("--dir", help="the folder to speak for (default: this session's)")
        sp.set_defaults(func=func)
    pj = sub.add_parser("projects", help="named xsm projects and their member folders")
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
    snd.add_argument("target", help="name, name@home, name [ref], ref:xxxxxx, claude:ID, codex:ID")
    snd.add_argument("--text")
    snd.add_argument("--text-file", help="file path, or - for stdin")
    snd.add_argument("--kind", choices=list(envelope.KINDS), default="note")
    snd.add_argument("--reply-to", help="message id being answered")
    snd.add_argument("--priority", choices=["next", "now", "later"], default="next")
    snd.add_argument("--wait", type=float, default=0.0,
                     help="seconds to wait for the receiver's own record of delivery")
    snd.add_argument("--json", action="store_true")
    snd.set_defaults(func=cmd_send)

    st = sub.add_parser("status", help="delivery state of one message")
    st.add_argument("msg_id")
    st.add_argument("--wait", type=float, default=0.0)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    lg = sub.add_parser("ledger", help="recent messages and their delivery state")
    lg.add_argument("--last", type=int, default=20)
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--compact", action="store_true")
    lg.set_defaults(func=cmd_ledger)

    hd = sub.add_parser("held", help="messages this machine refused and kept")
    hd.add_argument("action", nargs="?", default="list", choices=["list", "show", "drop"])
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
                     help="hooks only: do not write the slash commands or link the skill")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall", help="remove only the hook groups xsm added")
    un.add_argument("--claude-home", action="append")
    un.add_argument("--codex-home", action="append")
    un.set_defaults(func=cmd_uninstall)

    doc = sub.add_parser("doctor", help="what is installed, what is running, what is not covered")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=cmd_doctor)

    sl = sub.add_parser("statusline", help="one line for Claude's statusLine (no model call)")
    sl.set_defaults(func=cmd_statusline)

    stest = sub.add_parser("selftest", help="check the hook fails closed for peer messages")
    stest.set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.xsm_home:
        os.environ["XSM_HOME"] = os.path.expanduser(args.xsm_home)
        paths.HOME = os.environ["XSM_HOME"]
    paths.ensure_home()
    if args.command not in ("hook", "statusline", "prune"):
        housekeeping.maybe_prune()
    return args.func(args)
