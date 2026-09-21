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
import time

from . import config, envelope, housekeeping, install, ledger, paths, registry, resolve, send, \
    workers

OK, REFUSED, UNCONFIRMED, USAGE = 0, 2, 3, 4


def _here(args, me=None) -> str:
    """The folder a command speaks for: --dir, else the session running it
    (its own cwd, which the shell may have left), else this shell. A command
    that changes or writes on a folder's behalf takes --dir only from a person:
    otherwise one agent could speak for another project (ADR-0009)."""
    if getattr(args, "dir", None):
        if args.command in ("join", "leave", "post") and not workers.human_terminal():
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


def cmd_list(args) -> int:
    registry.adopt_open_codex()
    me = registry.me()
    if getattr(args, "action", None) == "clear":
        return cmd_list_clear(args, me)
    rows = _rows(args, me)
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
        if args.all:
            print("no sessions registered. Install the hooks first: xsm install --help")
        else:
            others = len([r for r in registry.records() if r.get("state") == "live"])
            print("no live sessions in this project (%s)%s" % (
                config.default_project(_here(args, me))[0],
                "; xsm list -a shows %d elsewhere" % others if others else ""))
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


def _home_tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path.startswith(home) else path


def _print_members(scope: dict, root: str) -> None:
    for m in scope.get("members", []):
        mine = os.path.realpath(m.get("root", "")) == root
        print("  %s%s" % (_home_tilde(m.get("root", "")), "  (this project)" if mine else ""))


def _person_or_refuse(what: str, mcp_tool: str) -> str | None:
    """Changing who may talk to whom is the user's decision (ADR-0009)."""
    if workers.human_terminal():
        return None
    return ("%s is your user's decision: ask them with the %s MCP tool (it shows them a form), "
            "or they run it in a terminal" % (what, mcp_tool))


def cmd_join(args) -> int:
    why = _person_or_refuse("joining a project", "xsm_join")
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
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
    why = _person_or_refuse("leaving a project", "xsm_join (with leave)")
    if why:
        print("refused: %s" % why, file=sys.stderr)
        return REFUSED
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


def cmd_block(args) -> int:
    if args.command == "unblock":
        why = _person_or_refuse("lifting a block", "no")
        if why:
            print("refused: lifting a block needs a person at a terminal", file=sys.stderr)
            return REFUSED
        changed = config.unblock(args.ref)
    else:
        changed = config.block(args.ref)
    print("%s %s" % (args.command + "ed" if changed else "no change for", args.ref))
    return OK


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
        if not args.no_mcp:
            outcome = install.install_mcp(home, runtime)
            print("  MCP server: %s" % {
                "added": "registered (xsm_post, xsm_channel, xsm_decide)",
                "current": "already registered",
                "replaced": "re-registered with the current command",
            }.get(outcome, outcome))
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
        if install.remove_mcp(home, runtime):
            print("%s: removed the MCP server" % home)
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
    try:
        worker = workers.spawn(args.runtime, name=args.name, model=args.model, effort=args.effort,
                               cwd=args.dir, home=args.home, once=args.once,
                               headless=args.headless, approval_timeout=args.approval_timeout,
                               wait=args.wait, caller=caller, max_depth=args.max_depth,
                               full_access=args.full_access, trust_hooks=args.trust_hooks,
                               grant=args.grant)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    where = "tmux pane %s" % worker["pane"] if worker.get("pane") else "headless"
    print("started %s (%s, %s%s) [%s] in %s" % (
        worker["name"], worker["runtime"], where,
        ", model %s" % worker["model"] if worker.get("model") else "", worker.get("ref"),
        worker["cwd"]))
    if args.task:
        # The id is on record before the task leaves, so the answer can never
        # arrive ahead of it (a `once` worker stops only on that exact answer).
        task_id = envelope.new_id()
        worker["task_id"] = task_id
        workers.save(worker)
        result = send.send("ref:%s" % worker["ref"], args.task, sender=caller, kind="task",
                           wait=args.task_wait, msg_id=task_id)
        print("task %s: %s%s" % (result.msg_id or "-", result.status,
                                 ": " + result.reason if result.reason else ""))
    if worker["mode"] == "headless":
        print("watch it: xsm attach %s   stop it: xsm stop %s" % (worker["name"], worker["name"]))
    else:
        print("stop it: xsm stop %s" % worker["name"])
    if worker.get("once"):
        print("it stops by itself once its answer to the task reaches this session")
    return OK


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
    for name, why in workers.reap():
        print("stopped %s: %s" % (name, why))
    rows = workers.all_workers()
    if not rows:
        print("no workers")
        return OK
    for w in rows:
        print("%s [%s] %s %s %s %s depth %s/%s%s%s%s" % (
            w["name"], w.get("ref"), w["runtime"], w["mode"], workers.state(w),
            w.get("model") or "-", w.get("depth", 1), w.get("max_depth", 1),
            "  once" if w.get("once") else "", "  FULL-ACCESS" if w.get("full_access") else "",
            "  hooks-untrusted" if w.get("trust_hooks") else ""))
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
    if approve:
        req = next((r for r in workers.approvals() if r["id"] == args.id), None)
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
        req = workers.answer(args.id, approve, args.reason)
    except workers.WorkerError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    print("%s %s: %s" % (req["status"], req["id"], req["summary"]))
    return OK


def cmd_pump(args) -> int:
    return workers.pump(args.name)


def cmd_post(args) -> int:
    from . import channel
    me = registry.me()
    here = _here(args, me)
    try:
        where = channel.resolve(here, args.channel)
        author = channel.author_here(me)
        rec = channel.post(where, author, args.text, args.tag, args.reply_to)
    except channel.ChannelError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return REFUSED
    print("posted %s to %s as %s" % (rec["id"], where[0], channel.label(author)))
    return OK


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
            author = channel.author_here(registry.me())
            node = doc.add(args.doc, author, body, args.tag or ["result"], args.parent or [])
            print("added node %s [%s] to %s" % (node["id"], ", ".join(node["tags"]),
                                                 os.path.basename(doc.nodes_dir(args.doc))))
        elif args.action == "render":
            doc.render(args.doc)
            print("rendered %s from %d node(s)" % (args.doc, len(doc.read(args.doc))))
        elif args.action == "log":
            print(doc.log(doc.read(args.doc)) or "(no nodes)")
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
    sp.add_argument("--headless", action="store_true", help="headless even inside tmux")
    sp.add_argument("--approval-timeout", type=int, default=workers.APPROVAL_TIMEOUT)
    sp.add_argument("--wait", type=float, default=90.0, help="seconds to wait for it to register")
    sp.add_argument("--full-access", action="store_true",
                    help="no sandbox, no approvals (Codex --dangerously-bypass-approvals-and-sandbox, "
                         "Claude bypassPermissions); needs --grant from an agent")
    sp.add_argument("--trust-hooks", action="store_true",
                    help="Codex pane worker: --dangerously-bypass-hook-trust; needs --grant from an agent")
    sp.add_argument("--grant", help="the id xsm_grant returned after your user allowed it")
    sp.add_argument("--max-depth", type=int, help="worker levels this worker's subtree may use "
                    "(default: XSM_MAX_DEPTH or config max_depth, 1; a worker can only lower it)")
    sp.set_defaults(func=cmd_spawn)
    wk = sub.add_parser("workers", help="workers xsm started")
    wk.set_defaults(func=cmd_workers)
    rp = sub.add_parser("reap", help=argparse.SUPPRESS)
    rp.add_argument("--after-pid", type=int)
    rp.set_defaults(func=cmd_reap)
    for verb, helptext, func in (("stop", "stop a worker and remove its records", cmd_stop),
                                 ("attach", "watch a headless worker and talk to it", cmd_attach),
                                 ("pump", argparse.SUPPRESS, cmd_pump)):
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("name")
        if verb == "stop":
            sp.add_argument("--internal", action="store_true", help=argparse.SUPPRESS)
        sp.set_defaults(func=func)
    po = sub.add_parser("post", help="post to this project's channel (the shared record)")
    po.add_argument("text")
    po.add_argument("--tag", default="note", help="note, question, proposal, result, hypothesis, "
                    "decision (a person only)")
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
    dc = sub.add_parser("doc", help="shared documents as immutable nodes (add, render, log, leaves, show)")
    dc.add_argument("action", choices=["add", "render", "log", "leaves", "show"])
    dc.add_argument("doc", help="the document, e.g. docs/research/cache.md")
    dc.add_argument("node", nargs="?", help="show: the node id")
    dc.add_argument("--tag", action="append", help="setup, result, insight, hypothesis, "
                    "verification, report, wip; endorsed is a person's")
    dc.add_argument("--parent", action="append", help="a node this builds on or revises")
    dc.add_argument("--text")
    dc.add_argument("--file")
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
        sp = sub.add_parser(verb, help="%s a worker's permission request (approve needs a terminal)"
                            % verb)
        sp.add_argument("id")
        sp.add_argument("--reason")
        sp.set_defaults(func=cmd_answer)

    for verb, helptext in (("block", "stop one session from sending or receiving"),
                           ("unblock", "lift a block (a person only)")):
        bp = sub.add_parser(verb, help=helptext)
        bp.add_argument("ref")
        bp.set_defaults(func=cmd_block)
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
    ins.add_argument("--no-mcp", action="store_true", help="do not register the xsm MCP server")
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
    if args.command not in ("hook", "statusline", "prune", "pump", "reap", "mcp"):
        housekeeping.maybe_prune()
    return args.func(args)
