"""A command the person typed into their own session counts as their consent.

User decision, 2026-09-28: a tester could not connect two repositories, because
that took an MCP form on both sides and Codex under approval_policy "never"
declines a form without showing it. Typing `/xsm link <folder>` (Codex:
`$xsm link <folder>`) is already the person saying yes; asking them again in a
form only gave that decline a chance to overrule them.

Where it is read differs by runtime, because a peer message can look exactly
like typing (2026-09-28, Claude Code 2.1.283 and its hooks documentation):

- Claude: only the UserPromptExpansion hook, which fires when the person types
  a slash command and never for a peer message (peer text arrives as plain
  text, and commands in it do not run). UserPromptSubmit is not used: a peer
  message written to the inbox socket without the wrapper reaches it with the
  same fields as the person's own "/xsm link x".
- Codex: UserPromptSubmit, for a prompt starting with `$xsm <verb>` that is not
  an xsm envelope or header. A `codex queue` item, which is how xsm itself
  delivers to Codex, is also raw user input there, so any process of the same
  OS user that can run `codex queue` could write one. That is inside xsm's
  trust boundary, the uid (ADR-0009): consent records are not security.

xsm's own workers are typed into through tmux, so a worker never records
consent. Consent is one file per session, ~/.xsm/asked/<ref>.json, good for
TTL seconds and for one use by the same verb on the same target.

The agent can also ask (user decision, 2026-10-01: "the user is asked, the
agent does the typing"; and "store the user's approval message as the
verdict"). When the agent runs `xsm link|join|leave|reach` with no consent,
the CLI records the request (~/.xsm/asked/<ref>.pending.json) and tells the
agent to ask its user in plain words. The next message the person types in
that session is kept on the request, verbatim, as the verdict. The agent reads
it, and if it is a yes runs the command again, which uses the request up and
keeps the verdict with what it changed. xsm does not judge the words; it keeps
them as the record of who decided what. Like typed consent this is inside the
uid boundary (ADR-0009): a peer message without an envelope looks like typing.
"""
from __future__ import annotations

import os
import re
import shlex
import time

from . import envelope, paths

ASKED = "asked"
TTL = 600
VERBS = ("link", "join", "leave", "reach")
# A Codex prompt as typed: `$xsm link <folder>`.
CODEX_RE = re.compile(r"\A\s*\$xsm[ \t]+(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The arguments of a Claude slash command: `link <folder>`.
ARGS_RE = re.compile(r"\A\s*(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The plugin's skill may be named with or without its plugin prefix.
CLAUDE_COMMANDS = ("xsm", "xsm:xsm")


def _path(ref: str) -> str:
    return paths.path(ASKED, "%s.json" % ref)


def _pending_path(ref: str) -> str:
    return paths.path(ASKED, "%s.pending.json" % ref)


def _target(args: str) -> str | None:
    """The first word of the typed arguments that is not an option."""
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    return next((w for w in words if not w.startswith("-")), None)


def resolve(verb: str, target: str, cwd: str) -> str:
    """A folder as its canonical project root, relative to the session's
    folder; a project name (join, leave) as it is."""
    if verb not in ("link", "reach"):
        return target
    from . import config            # lazy: config is heavier than this module needs
    return config.project_root(os.path.join(cwd or "/", os.path.expanduser(target)))


def _write(me: dict | None, verb: str, args: str) -> dict | None:
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not _target(args):
        return None
    entry = {"verb": verb, "args": args.strip(), "cwd": me.get("cwd") or "", "t": time.time(),
             "session_id": str(me.get("session_id") or ""), "runtime": me.get("runtime")}
    paths.write_json(_path(me["ref"]), entry, mode=0o600)
    return entry


def record(me: dict | None, data: dict) -> dict | None:
    """Keep the person's typed xsm command, from one hook call, as consent for
    this session. Returns what was written, or None when there is none."""
    event = data.get("hook_event_name")
    if event == "UserPromptExpansion":
        if data.get("expansion_type") != "slash_command" or \
                data.get("command_name") not in CLAUDE_COMMANDS:
            return None
        m = ARGS_RE.match(data.get("command_args") or "")
    elif event == "UserPromptSubmit":
        text = data.get("prompt") or ""
        if envelope.parse(text).peer or envelope.looks_like_peer(text):
            return None
        m = CODEX_RE.match(text) if (me or {}).get("runtime") == "codex" else None
        if not m and not text.lstrip().startswith(("/xsm", "$xsm")):
            note_verdict(me, text)
    else:
        return None
    return _write(me, m.group(1), m.group(2)) if m else None


def request(me: dict | None, verb: str, target: str, here: str | None = None) -> None:
    """Record that the agent asked to `verb` `target` and must ask its user;
    their next typed message becomes the verdict (note_verdict)."""
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not target:
        return
    cwd = me.get("cwd") or ""
    paths.write_json(_pending_path(me["ref"]), {
        "verb": verb, "target": resolve(verb, target, cwd),
        "here": resolve(verb, here, cwd) if here else None, "cwd": cwd, "t": time.time(),
        "session_id": str(me.get("session_id") or ""), "verdict": None}, mode=0o600)


def _fresh_pending(me: dict | None) -> tuple:
    """(path, entry) of this session's unexpired request, or (None, None)."""
    if not me or not me.get("ref"):
        return None, None
    p = _pending_path(me["ref"])
    entry = paths.read_json(p)
    if not isinstance(entry, dict) or time.time() - float(entry.get("t") or 0) > TTL:
        return None, None
    if entry.get("session_id") and me.get("session_id") and \
            entry["session_id"] != str(me["session_id"]):
        return None, None               # a session sharing the ref (24 bits) asked
    return p, entry


def note_verdict(me: dict | None, text: str) -> bool:
    """Keep the person's first message after a request as its verdict."""
    p, entry = _fresh_pending(me)
    if not p or entry.get("verdict") is not None or not text.strip():
        return False
    entry.update({"verdict": text.strip()[:1000], "verdict_t": time.time()})
    paths.write_json(p, entry, mode=0o600)
    return True


def take_verdict(me: dict | None, verb: str, target: str, here: str | None = None) -> str | None:
    """The person's reply to the request for exactly this, used up; None when
    there is no such request or they have not answered yet."""
    p, entry = _fresh_pending(me)
    if not p or entry.get("verdict") is None or entry.get("verb") != verb:
        return None
    cwd = entry.get("cwd") or (me or {}).get("cwd") or ""
    if resolve(verb, target, (me or {}).get("cwd") or cwd) != entry.get("target"):
        return None
    if here and entry.get("here") and resolve(verb, here, cwd) != entry["here"]:
        return None
    try:
        os.unlink(p)
    except OSError:
        return None                     # used by another reader first
    return entry["verdict"]


def take(me: dict | None, verb: str, target: str, here: str | None = None) -> bool:
    """Use up this session's consent for `verb` on `target` (a folder relative
    to the session's folder, or a project name). True only for a fresh,
    matching consent, which is then gone. For link, `here` is the folder being
    linked from, and it must be the session's own. Anything that does not
    match is left as it was."""
    if not me or not me.get("ref") or not target:
        return False
    p = _path(me["ref"])
    entry = paths.read_json(p)
    if not isinstance(entry, dict) or entry.get("verb") != verb:
        return False
    if time.time() - float(entry.get("t") or 0) > TTL:
        return False
    if entry.get("session_id") and me.get("session_id") and \
            entry["session_id"] != str(me["session_id"]):
        return False                    # a session sharing the ref (24 bits) typed it
    typed = _target(entry.get("args") or "")
    cwd = entry.get("cwd") or me.get("cwd") or ""
    if not typed or resolve(verb, typed, cwd) != resolve(verb, target, me.get("cwd") or cwd):
        return False
    if verb == "link" and here is not None and \
            resolve("link", here, cwd) != resolve("link", ".", cwd):
        return False
    try:
        os.unlink(p)
    except OSError:
        return False                    # someone else used it first
    return True
