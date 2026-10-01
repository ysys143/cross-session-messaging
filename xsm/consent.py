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
agent to ask its user in plain words. The person's latest message in that
session is kept on the request, verbatim, as the verdict. The agent reads it
in two steps: the first run after a reply refuses and shows it, the next run
goes ahead and uses the request up, keeping the verdict with what it changed. The
second run must come a moment after the first (SHOW_DELAY), and an ask older
than ASK_MAX is gone however much the person has said since.
xsm does not judge the words; it keeps them as the record of who decided what.
Like typed consent this is inside the uid boundary (ADR-0009): a peer message
without an envelope looks like typing.

The agent may ask with Claude Code's AskUserQuestion tool instead of in plain
words (2026-10-01: its answer comes back as a tool result, so no prompt hook saw
it and the agent fell back to asking the person to type it in chat). A PostToolUse
hook matched on that tool keeps the chosen answers, with the questions, as the
verdict, exactly as a typed reply would be. What the hook receives was read from
a transcript: the tool's own result is {"questions", "answers": {question:
label or the person's own words}, "annotations": {question: {"notes"?}}}.

An MCP form tool that gets no answer (no form, a decline the client never showed,
a dismissal) records the ask itself (`ask`), under the key its CLI command will
use, and tells the agent to ask now. Telling it to run the command and then ask
made the person say yes twice: their first "yes" had no ask to attach to
(2026-10-01, Codex).

An ask leaves files behind: the pending file holds up to VERDICT_MAX characters of
the person's words, and flock's file stays. Both go when the ask is used or has
expired (while the lock is held, so a waiter on the old lock starts over on the
new one), and `prune` sweeps the ones nobody came back to.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import time

from . import envelope, paths

ASKED = "asked"
TTL = 600
# The window slides with every reply, so a person who keeps chatting kept an
# ask alive for hours and an unrelated later message became its verdict. This
# counts from the ask itself (2026-10-01).
ASK_MAX = 1800
# What a verdict keeps of the person's words. A form of several questions had
# its tail cut at 1000 (2026-10-01); answers_text shortens the questions first.
VERDICT_MAX = 4000
QUESTION_MAX = 200
# A reply shown to the agent passes no sooner than this after it was shown, on
# a later run: `xsm unblock X || xsm unblock X` on one line showed the reply
# and used it in the same breath, before the agent could read it (2026-10-01).
SHOW_DELAY = 1.0
# How long a run waits for another that holds the lock of an ask; then it goes
# on unlocked, as it does where the folder cannot be written (2026-10-01).
LOCK_WAIT = 5.0
# A lock file with no ask is an orphan only after this long: a run makes the
# lock a moment before it writes the ask, and a sweep in between would take it
# from under the run (2026-10-01).
LOCK_GRACE = 5.0
VERBS = ("link", "join", "leave", "reach")
# A Codex prompt as typed: `$xsm link <folder>`.
CODEX_RE = re.compile(r"\A\s*\$xsm[ \t]+(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The arguments of a Claude slash command: `link <folder>`.
ARGS_RE = re.compile(r"\A\s*(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The plugin's skill may be named with or without its plugin prefix.
CLAUDE_COMMANDS = ("xsm", "xsm:xsm")
# What the harness writes into a prompt is not the person answering: a
# background task's completion, a hook's or a command's echo, a peer message.
# A task completion was stored as the verdict (measured 2026-10-01). The
# prefixes are the tags Claude Code and xsm use, and Codex's (underscores);
# any other lowercase tag counts when it is also closed.
HARNESS_PREFIXES = ("<task-notification", "<system-reminder", "<command-", "<local-command",
                    "<bash-", "<user-prompt-submit-hook", "<cross-session-message",
                    "<turn_aborted", "<user_shell_command", "<subagent_notification",
                    "<environment_context", "<hook_prompt")
TAG_RE = re.compile(r"\A<([a-z][a-z0-9_-]*)[\s>]")
# A slash command (`/clear`, `/model opus`) is not an answer; a path ("/tmp/x") is.
SLASH_RE = re.compile(r"\A/[A-Za-z][\w:.-]*(?:\s|\Z)")


def _path(ref: str) -> str:
    return paths.path(ASKED, "%s.json" % ref)


def _pending_path(ref: str) -> str:
    return paths.path(ASKED, "%s.pending.json" % ref)


def _lock(lock: str, wait: float | None = None):
    """An fd that holds flock on `lock` (made if need be), or None when there is
    none to be had: the folder cannot be written, or a holder kept it for more
    than `wait` seconds (LOCK_WAIT). The file is removed only by a holder, so
    after getting a lock the fd must still be the file at that path; if not,
    start over on the new one, or two holders would each have a lock."""
    deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
    try:
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        while True:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("lock held too long")
                        time.sleep(0.01)
                try:
                    if os.fstat(fd).st_ino == os.stat(lock).st_ino:
                        return fd
                except FileNotFoundError:
                    pass                # its holder removed it
            except OSError:
                os.close(fd)
                raise
            os.close(fd)
    except OSError:
        return None


@contextlib.contextmanager
def _locked(p: str):
    """One reader-writer at a time for a pending file (flock on a sibling
    .lock). A reply written between another run's read and its write was lost,
    and two reruns could each see the other's state as theirs (measured 323 of
    400, 2026-10-01). Without a lock to be had the run goes on unlocked: a hook
    must not fail, or wait for good, on it."""
    fd = _lock(p + ".lock")
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)                # closing releases the lock


def _drop(p: str) -> None:
    """Remove an ask and its lock, while holding that lock."""
    for name in (p, p + ".lock"):
        try:
            os.unlink(name)
        except OSError:
            pass


def _expired(entry: dict, now: float) -> bool:
    """A request without a reply expires TTL after it was made; once the person
    has replied, TTL after their latest reply, but never later than ASK_MAX
    after the request."""
    asked = float(entry.get("t") or 0)
    return now - max(asked, float(entry.get("verdict_t") or 0)) > TTL or now - asked > ASK_MAX


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


def not_a_reply(text: str) -> bool:
    """True for a prompt that cannot be the person's answer to a request: xsm's
    own commands, other slash commands, and text the harness injected."""
    text = text.strip()
    if text.startswith(("$xsm", "/xsm")) or SLASH_RE.match(text):
        return True
    if text.startswith(HARNESS_PREFIXES):
        return True
    tag = TAG_RE.match(text)
    return bool(tag) and "</%s>" % tag.group(1) in text


def answers_text(data: dict) -> str | None:
    """The person's choices in an AskUserQuestion result, one `<question> ->
    <answer>` per question (the answer is the option's label, or what they
    wrote under Other), with their notes when they gave any. Not quoted: the
    show step quotes the whole reply, and nested quotes read as `""Q" -> "A""`
    (2026-10-01). From the hook's `tool_response`, which carries the answers
    (Claude Code 2.1.286 also puts them in `tool_input`, so either would do). A
    result that is plain text comes through as it is. Past VERDICT_MAX the
    questions are shortened, never the answers."""
    out = data.get("tool_response")
    if isinstance(out, list):
        out = " ".join(b.get("text") or "" for b in out if isinstance(b, dict))
    if isinstance(out, str):
        return out.strip() or None
    if not isinstance(out, dict) or not isinstance(out.get("answers"), dict):
        return None
    notes = out.get("annotations") if isinstance(out.get("annotations"), dict) else {}
    rows = []
    for question, answer in out["answers"].items():
        note = (notes.get(question) or {}).get("notes") if isinstance(notes.get(question), dict) \
            else None
        tail = " -> %s" % answer
        if isinstance(note, str) and note.strip():
            tail += " (notes: %s)" % note.strip()
        rows.append((str(question), tail))
    if not rows:
        return None
    room = (VERDICT_MAX - sum(len(t) + 2 for _, t in rows)) // len(rows)
    limit = min(QUESTION_MAX, max(room, 20))
    return "; ".join((q if len(q) <= limit else q[:limit - 3] + "...") + t for q, t in rows)


def record(me: dict | None, data: dict) -> dict | None:
    """Keep the person's typed xsm command, from one hook call, as consent for
    this session; keep their reply to an ask as its verdict. Returns what was
    written as consent, or None when there is none."""
    event = data.get("hook_event_name")
    if event == "PostToolUse":
        # Claude's AskUserQuestion: the answer is a tool result, which no
        # prompt hook sees (2026-10-01). Only the person can have chosen it.
        text = answers_text(data) if data.get("tool_name") == "AskUserQuestion" else None
        if text:
            note_verdict(me, text)
        return None
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
        if not m and not not_a_reply(text):
            note_verdict(me, text)
    else:
        return None
    return _write(me, m.group(1), m.group(2)) if m else None


def _fresh_pending(me: dict | None) -> tuple:
    """(path, entry) of this session's unexpired request, or (None, None). An
    expired one is removed here: it holds up to VERDICT_MAX characters of the
    person's words. Called with the lock held."""
    if not me or not me.get("ref"):
        return None, None
    p = _pending_path(me["ref"])
    entry = paths.read_json(p)
    if not isinstance(entry, dict):
        return None, None
    if _expired(entry, time.time()):
        _drop(p)
        return None, None
    if entry.get("session_id") and me.get("session_id") and \
            entry["session_id"] != str(me["session_id"]):
        return None, None               # a session sharing the ref (24 bits) asked
    return p, entry


def note_verdict(me: dict | None, text: str) -> bool:
    """Keep the person's latest message after a request as its verdict. It
    replaces an earlier one (user decision, 2026-10-01): the first message is
    often a question ("does that delete the other records too?") and the yes
    or no comes after it. A new reply has not been shown to the agent yet."""
    if not me or not me.get("ref") or not text.strip() or \
            not os.path.exists(_pending_path(me["ref"])):       # every prompt comes by here
        return False
    try:
        with _locked(_pending_path(me["ref"])):
            p, entry = _fresh_pending(me)
            if not p:
                return False
            entry.update({"verdict": text.strip()[:VERDICT_MAX], "verdict_t": time.time(),
                          "shown": False})
            paths.write_json(p, entry, mode=0o600)
    except OSError:
        return False                    # a folder that cannot be written keeps no reply
    return True


def _wanted(me: dict, verb: str, target: str, here: str | None) -> tuple:
    """The target and the folder as a request records them. Resolved before the
    lock is taken: for a folder that runs `git rev-parse`, and the other runs
    would wait on it."""
    cwd = me.get("cwd") or ""
    return resolve(verb, target, cwd), resolve(verb, here, cwd) if here else None


def digest(*parts) -> str:
    """A short stable name for what is being decided (a decision's channel and
    text, an endorsement), so the same command run again finds the same ask.
    The CLI keys its ask with it, and so does the MCP tool that names that
    command when its form cannot be answered (ask)."""
    import hashlib
    return hashlib.sha256("\0".join(str(p or "") for p in parts).encode()).hexdigest()[:16]


def _matches(entry: dict, verb: str, want: str, want_here: str | None) -> bool:
    """Whether a pending ask is for exactly this."""
    return entry.get("verb") == verb and want == entry.get("target") and not (
        want_here and entry.get("here") and want_here != entry["here"])


# Appended to what the agent is shown with a reply an older hook stored. The
# hooks of a session started before an update keep running the old code, and
# 0.4.14's kept only the first message after an ask (new ones mark theirs
# `shown`, which an old one never writes; 2026-10-01).
OLD_HOOK = ("Note: this session's xsm hooks are older than this xsm command, so only their first "
            "message after the ask was kept. If they said anything else since, it is not here, "
            "and you must not run this again on it: tell them a new session is needed for the "
            "update, or use the MCP form tool if there is one.")


def _take(me: dict, verb: str, want: str, want_here: str | None) -> tuple:
    """take_or_request's step, with the lock held: (reply, go, how it was
    kept). An OSError from it means the reply could not be marked or used: the
    folder cannot be written."""
    p, entry = _fresh_pending(me)
    if not p or entry.get("verdict") is None or not _matches(entry, verb, want, want_here):
        return None, False, True
    now = time.time()
    if not entry.get("shown"):
        old = "shown" not in entry
        entry.update({"shown": True, "shown_t": now})
        paths.write_json(p, entry, mode=0o600)
        return entry["verdict"], False, OLD_HOOK if old else True
    if now - float(entry.get("shown_t") or 0) < SHOW_DELAY:
        return entry["verdict"], False, True
    try:
        os.unlink(p)
    except FileNotFoundError:
        return None, False, True        # used by another reader first
    _drop(p)                            # the lock too
    return entry["verdict"], True, True


def _record(me: dict, verb: str, want: str, want_here: str | None) -> None:
    """Record the ask, with the lock held. One already on record for exactly
    this stays as it is, with the time it was made: a run before the person
    was asked pushed ASK_MAX out each time (2026-10-01)."""
    _, entry = _fresh_pending(me)
    if entry and _matches(entry, verb, want, want_here):
        return
    paths.write_json(_pending_path(me["ref"]), {
        "verb": verb, "target": want, "here": want_here, "cwd": me.get("cwd") or "",
        "t": time.time(), "session_id": str(me.get("session_id") or ""),
        "verdict": None}, mode=0o600)


def take_or_request(me: dict | None, verb: str, target: str, here: str | None = None) -> tuple:
    """What the person said to the request for exactly this, in two steps
    (user decision, 2026-10-01: xsm does not read the words, so the agent
    reads them before anything is done on them), and when there is nothing to
    take, a record that the agent asked to `verb` `target` and must ask its
    user (their latest answer then becomes the verdict, note_verdict).
    Returns (reply, go, kept):

    - (None, False, kept): no such request or no reply yet. `kept` says that an
      ask is on record. Not kept, the reply cannot be kept here (no registered
      session, a worker, a state folder that cannot be written) and the caller
      does not promise it.
    - (reply, False, kept): a reply the agent has not seen, marked as shown now,
      which the caller refuses with. `kept` is OLD_HOOK, a string to show with
      it, for a reply an older hook stored.
    - (reply, True, kept): the reply that was shown, is still the latest and was
      shown at least SHOW_DELAY ago, which is used up: the caller goes ahead,
      once. A rerun sooner than that gets the reply shown again, so a line that
      runs the command twice does not pass on its own showing.

    One locked step, the read and the write together (_locked): a reply landing
    between a take and a separate request was overwritten unseen (adversarial
    check, 2026-10-01)."""
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not target:
        return None, False, False
    want, want_here = _wanted(me, verb, target, here)
    try:
        with _locked(_pending_path(me["ref"])):
            reply, go, kept = _take(me, verb, want, want_here)
            if reply is None:
                _record(me, verb, want, want_here)
            return reply, go, kept
    except OSError:
        return None, False, False


def ask(me: dict | None, verb: str, target: str, here: str | None = None) -> bool:
    """Record that `verb` on `target` is being put to the person, as the CLI
    does at its first refusal. For a form that could not be answered: the agent
    then asks in plain words, and without this their "yes" has no ask to attach
    to (note_verdict), or attaches to an unrelated one (2026-10-01). The CLI
    command that follows finds this ask by the same key. True when an ask is on
    record, with the same limits as take_or_request."""
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not target:
        return False
    want, want_here = _wanted(me, verb, target, here)
    try:
        with _locked(_pending_path(me["ref"])):
            _record(me, verb, want, want_here)
    except OSError:
        return False
    return True


def _stale(p: str, now: float, typed: bool) -> bool:
    """Whether a file of asked/ is past its window; one that cannot be read or
    makes no sense is judged by its age instead."""
    entry = paths.read_json(p)
    try:
        if isinstance(entry, dict):
            return now - float(entry.get("t") or 0) > TTL if typed else _expired(entry, now)
        return now - os.path.getmtime(p) > ASK_MAX
    except (TypeError, ValueError, OSError):
        return False


def _drop_typed(p: str, now: float) -> bool:
    """Remove a typed consent past TTL. Nothing locks these (a hook writes one
    with a rename, `take` unlinks it), so it is judged where it was moved to: a
    fresh one written since it was last read is put back, not deleted."""
    claim = "%s.%d.prune.json" % (p[:-len(".json")], os.getpid())
    try:
        os.rename(p, claim)
    except OSError:
        return False
    if _stale(claim, now, typed=True):
        os.unlink(claim)
        return True
    try:
        os.link(claim, p)               # not if a newer one has been written
    except OSError:
        pass
    os.unlink(claim)
    return False


def _young(p: str, now: float) -> bool:
    try:
        return now - os.path.getmtime(p) < LOCK_GRACE
    except OSError:
        return False


def prune(now: float | None = None, dry_run: bool = False) -> list:
    """Remove what asks left behind: a pending file past its window (it keeps
    up to VERDICT_MAX characters of the person's words), a lock with no ask
    (not a young one: see LOCK_GRACE), a typed consent past TTL. An ask in use
    is left: it is taken under its lock, without waiting, and judged again once
    held. Returns the names removed (or that would be)."""
    now = time.time() if now is None else now
    folder = paths.path(ASKED)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    removed = []
    for name in names:
        p = os.path.join(folder, name)
        if not os.path.exists(p):
            continue                    # went with the ask before it
        if name.endswith(".pending.json.lock"):
            lock, pending = p, p[:-len(".lock")]
            stale = not os.path.exists(pending) and not _young(p, now)
        elif name.endswith(".pending.json"):
            lock, pending = p + ".lock", p
            stale = _stale(p, now, typed=False)
        elif name.endswith(".json"):
            if _stale(p, now, typed=True) and (dry_run or _drop_typed(p, now)):
                removed.append(name)
            continue
        else:
            continue
        if not stale:
            continue
        if dry_run:
            removed.append(name)
            continue
        fd = _lock(lock, 0)
        if fd is None:
            continue
        try:
            if not os.path.exists(pending) or _stale(pending, now, typed=False):
                _drop(pending)
                removed.append(name)
        finally:
            os.close(fd)
    return removed


def _how(runtime: str | None) -> str:
    """How the agent may ask. A Claude Code agent may use its question tool: the
    answer it gets is kept as the reply (answers_text); no other runtime's is."""
    return "in plain words, or with your question tool (AskUserQuestion)" if runtime == "claude" \
        else "in plain words"


def asks(what: str, tail: str = "", runtime: str | None = None) -> str:
    """What the agent is told when nobody has been asked yet: ask first, then
    run the command again to be shown the reply, then once more on a yes. The
    order was left implicit and agents reran before asking (2026-10-01)."""
    how = _how(runtime)
    return ("%s needs your user's yes. First ask them, %s, whether to go ahead, and "
            "wait for their answer. After they answer, run this same command again: xsm keeps "
            "their latest answer in this session as the verdict, and this run shows you that "
            "reply without acting on it. If it is a yes, run the command once more to go ahead; "
            "if it is a no or a question, leave it and answer them.%s" % (what, how, tail))


def shown_refusal(reply: str, what: str, kept=None) -> str:
    """What the agent is told with a reply it has not seen: the words, and
    that this run did not go ahead. `kept` is take_or_request's: a string
    (OLD_HOOK) is added."""
    text = ('your user replied: "%s". If that is a yes to %s, run this same command again to '
            'go ahead; if it is a no or a question, do not: answer them, and their next '
            'message replaces it.' % (reply, what[:1].lower() + what[1:]))
    return text + " " + kept if isinstance(kept, str) else text


def in_words(command: str, kept: bool, runtime: str | None = None) -> str:
    """What the agent is told when a form could not be answered: ask in plain
    words, and the command is the way to act on the answer. With the ask on
    record (`ask`, `kept`) they ask now and the command shows the reply. Without
    one, the command goes first, because it is what records the ask and an
    answer before it has nothing to attach to (2026-10-01: the person said yes
    twice)."""
    if kept:
        return ("Ask your user now, %s, whether to go ahead. After they answer, run `%s` in your "
                "shell: it shows you their reply without acting on it. If it is a yes, run it once "
                "more to go ahead; if it is a no or a question, leave it and answer them."
                % (_how(runtime), command))
    return ("Run `%s` in your shell first: it records that you are asking and tells you what to "
            "ask. Then ask your user, %s, whether to go ahead; after they answer, run it again to "
            "be shown their reply, and once more on a yes." % (command, _how(runtime)))


def cannot_keep(what: str, form_tool: str | None) -> str:
    """The refusal when no reply can be kept here: say so, and never promise
    what xsm cannot do (issue #9)."""
    way = ("If the %s MCP tool is available, use it: it asks them with a form. If it is "
           "not, tell them plainly that this cannot be decided from here." % form_tool
           if form_tool and form_tool != "no" else
           "There is no form tool for this decision, so tell them plainly that it cannot be "
           "decided from here.")
    return ("%s needs your user's yes, but xsm cannot keep their reply in this session (it is "
            "not a registered session of theirs, or its state cannot be written). %s"
            % (what, way))


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
