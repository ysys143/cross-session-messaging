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

from . import config, envelope, install, ledger, paths, registry, resolve, send

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
    width = max(len("%s@%s" % (r.get("name"), r.get("alias"))) for r in rows)
    for r in rows:
        addr = "%s@%s" % (r.get("name"), r.get("alias"))
        flags = [] if r.get("registered") else ["unregistered"]
        if not r.get("scope") and r.get("scope_reason") != "self":
            flags.append("out-of-scope")
        if me and r.get("ref") == me.get("ref"):
            flags.append("you")
        if row.get("native") == "hold":
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
    print(json.dumps(me, ensure_ascii=False, indent=1) if args.json
          else "%s@%s [%s] %s %s" % (me["name"], me["alias"], me["ref"],
                                     me["runtime"], me.get("cwd") or ""))
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
        if result.candidates:
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


def cmd_ledger(args) -> int:
    rows = ledger.recent(args.last)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
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
        print("%-16s %-8s from %-20s %s" % (os.path.basename(p)[:-5], entry.get("runtime"),
                                            entry.get("from"), entry.get("reason")))
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
        print("installed into %s (backup: %s)" % (result["file"], result.get("backup", "none")))
        if runtime == "codex":
            print("  Codex asks you to trust hooks once, at the next session start. "
                  "Until you do, the hook does not run.")
    return USAGE if failed else OK


def cmd_uninstall(args) -> int:
    targets = [(h, "claude") for h in (args.claude_home or [])] + \
              [(h, "codex") for h in (args.codex_home or [])]
    for home, runtime in targets or [(h["path"], h["runtime"]) for h in config.homes()]:
        result = install.remove(home, runtime)
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


def cmd_prune(args) -> int:
    print("removed %d stale pointer(s)" % registry.prune(args.days))
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
    ls.set_defaults(func=cmd_list)

    who = sub.add_parser("who", help="identity of the session running this command")
    who.add_argument("--json", action="store_true")
    who.set_defaults(func=cmd_who)

    homes = sub.add_parser("homes", help="declared CONFIG_DIR / CODEX_HOME list")
    homes.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    homes.add_argument("path", nargs="?")
    homes.add_argument("--runtime", choices=["claude", "codex"])
    homes.add_argument("--alias")
    homes.set_defaults(func=cmd_homes)

    prune = sub.add_parser("prune", help="drop old pointers of sessions that are gone")
    prune.add_argument("--days", type=float, default=14.0)
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
    lg.set_defaults(func=cmd_ledger)

    hd = sub.add_parser("held", help="messages this machine refused and kept")
    hd.add_argument("action", nargs="?", default="list", choices=["list", "show", "drop"])
    hd.add_argument("id", nargs="?")
    hd.set_defaults(func=cmd_held)

    ins = sub.add_parser("install", help="add the xsm hooks to a home (merges, never overwrites)")
    ins.add_argument("--claude-home", action="append")
    ins.add_argument("--codex-home", action="append")
    ins.add_argument("--dry-run", action="store_true")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall", help="remove only the hook groups xsm added")
    un.add_argument("--claude-home", action="append")
    un.add_argument("--codex-home", action="append")
    un.set_defaults(func=cmd_uninstall)

    doc = sub.add_parser("doctor", help="what is installed, what is running, what is not covered")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=cmd_doctor)

    stest = sub.add_parser("selftest", help="check the hook fails closed for peer messages")
    stest.set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.xsm_home:
        os.environ["XSM_HOME"] = os.path.expanduser(args.xsm_home)
        paths.HOME = os.environ["XSM_HOME"]
    paths.ensure_home()
    return args.func(args)
