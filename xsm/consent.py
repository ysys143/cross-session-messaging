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
the CLI records the request (~/.xsm/asked/<ref>.<key>.pending.json) and tells the
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

What the person's own request is worth (user decision, 2026-10-01: "if I told you to
connect, follow it"; the ping-pong that asked them to say yes to what they had just
asked for was the first problem). The hook keeps their last RECENT_KEEP prompts of a
session (asked/<ref>.recent.json, never a command or text the harness wrote). When the
agent runs a command with no reply on record, and the person's latest prompt, young
enough and short enough, names the target (the folder by path or last name, the
project, a session in it by name or ref) and says what they want (INTENT_RE: connect,
send, 연결, 보내, ...), that prompt is the reply, already shown: the next run goes ahead.
It is used once. xsm still does not judge the words: the agent reads them in the show
step, and "don't connect repo-b" names the target and is a no it will see. Without a
name or an intent the person is asked as before. Policy `reply_from_request` = false
turns it off.

Each thing asked about has its own request file (asked/<ref>.<digest of verb and
target>.pending.json), so asking about a reach does not erase the reply to a link; a
reply is written to every request the session has open, and the show step guards which
one it answers. The single slot older versions wrote is still read. An ask lives
ASK_MAX; a reply is good for TTL.

When the hook did not record the reply (a session started before an update; a state
folder it could not write) the agent gives it itself, `--reply "<their words>"` on the
asking command (`supplied`): the same show step runs on those words, and the use is
logged (event consent-flag). Policy `reply_flag` = false refuses the flag.

An agent must not approve on its own words (adversarial check, 2026-10-02: two runs of
`--reply "yes please"` linked two folders and logged the person as the one who said it).
So the flag is taken only for words the person wrote: they match (whitespace and case
aside, either contained in the other) a prompt the hook kept for this session, or the
verdict the hook stored on the ask; what is kept is then that prompt, never the agent's
wording. A session the hook has never written for (`recent.json` was never there: an
older hook, a folder it cannot write) has no such record to check against, and the flag
stands as it was. Either way the use is marked agent-supplied (`used_via`, by(), the
decision log): the person did not type it into xsm. Both paths are inside the uid
boundary (ADR-0009): the agent could write the files itself; this only keeps it from
doing so by accident or on a whim.

Words approve once (2026-10-02, live Codex test: the "응" that linked one folder let
`--reply "응"` link the next, though the person was never asked about it). When a verdict
passes, the hook's, the request taken as one, or the flag's, the prompts that say it are
marked `used` in recent.json (`_spend`), and neither the flag nor a request reads a used
prompt. And when the ask for this target was on record before the flag was given, a prompt
from before that ask is not an answer to it; with no ask yet, the prompt may be the
request, said before there was a question.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import sys
import time

from . import envelope, paths

ASKED = "asked"
TTL = 600
# The window slides with every reply, so a person who keeps chatting kept an
# ask alive for hours and an unrelated later message became its verdict. This
# counts from the ask itself (2026-10-01).
ASK_MAX = 1800
# What a verdict keeps of the person's words. A form of several questions had
# its tail cut at 1000 (2026-10-01); answers_text shortens the questions, then
# the notes, and the answers last, each in proportion, so none loses its tail.
VERDICT_MAX = 4000
QUESTION_MAX = 200
# A reply shown to the agent passes no sooner than this after it was shown, on
# a later run: `xsm unblock X || xsm unblock X` on one line showed the reply
# and used it in the same breath, before the agent could read it (2026-10-01).
SHOW_DELAY = 1.0
# How long a run waits for another that holds the lock of an ask; then it goes
# on unlocked, as it does where the folder cannot be written (2026-10-01). A
# hook runs on every prompt of every session under a 10 s limit, so it waits
# a fifth of that.
LOCK_WAIT = 5.0
HOOK_LOCK_WAIT = 1.0
# A lock file with no ask is an orphan only after this long: a run makes the
# lock a moment before it writes the ask, and a sweep in between would take it
# from under the run (2026-10-01).
LOCK_GRACE = 5.0
# The person's last prompts of a session, kept so the request that made the
# agent run a command can stand as the reply to its ask (2026-10-01).
RECENT_KEEP = 3
# How long a `recent.json` stays once its words are gone: it says a hook has written
# in this session, which `--reply` is checked against (2026-10-02).
HOOK_SEEN_MAX = 7 * 86400
# A request is a sentence or two; a long paste that happens to mention a
# folder and "send" is not one.
REQUEST_MAX = 500
VERBS = ("link", "join", "leave", "reach")
# What a consent given with --reply is called in the records (by, decisions.jsonl).
AGENT_SUPPLIED = "agent-supplied"
# What a person says when they want the thing done: to connect, send, let in.
# English words as words (so "unlink" is not "link"), Korean as stems.
INTENT_RE = re.compile(
    r"\b(?:connect|link|join|reach|send|unblock|approve|allow|grant|deliver|release|forward|"
    r"relay|message|tell|talk|ask|notify|leave|remove)\w*|"
    r"연결|가입|합류|참여|보내|보낼|전달|전송|알려|말해|물어|풀어|해제|허용|허락|승인|연락|대화|소통",
    re.I)
# A Codex prompt as typed: `$xsm link <folder>`.
CODEX_RE = re.compile(r"\A\s*\$xsm[ \t]+(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The arguments of a Claude slash command: `link <folder>`.
ARGS_RE = re.compile(r"\A\s*(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The plugin's skill may be named with or without its plugin prefix.
CLAUDE_COMMANDS = ("xsm", "xsm:xsm")
# What the harness writes into a prompt is not the person answering: a
# background task's completion, a hook's or a command's echo, a peer message.
# A task completion was stored as the verdict (measured 2026-10-01). The
# prefixes are the tags Claude Code and xsm use, and Codex's (underscores).
# Any other tag counts when it is closed and written the harness way, with a
# hyphen or underscore in its name; a plain word is a person's markup (`<b>응</b>
# 해줘`, `<yes>진행</yes>`: 2026-10-01 review).
HARNESS_PREFIXES = ("<task-notification", "<system-reminder", "<command-", "<local-command",
                    "<bash-", "<user-prompt-submit-hook", "<cross-session-message",
                    "<turn_aborted", "<user_shell_command", "<subagent_notification",
                    "<environment_context", "<hook_prompt")
TAG_RE = re.compile(r"\A<([a-z][a-z0-9]*[_-][a-z0-9_-]*)[\s>]")
# A slash command (`/clear`, `/model opus`) is not an answer; a path ("/tmp/x") is.
SLASH_RE = re.compile(r"\A/[A-Za-z][\w:.-]*(?:\s|\Z)")


def _path(ref: str) -> str:
    return paths.path(ASKED, "%s.json" % ref)


def _pending_path(ref: str, key: str | None = None) -> str:
    """One file per thing asked about: `<ref>.<digest of verb and target>`. Asking
    about a reach used to erase the reply to a link, because a session had a
    single slot (2026-10-01). With no key it is that single slot, which older
    versions wrote and which is still read."""
    return paths.path(ASKED, "%s.pending.json" % ref if key is None
                      else "%s.%s.pending.json" % (ref, key))


def pending_files(ref: str) -> list:
    """Every ask file of this session: the keyed ones, and an older version's
    single slot."""
    try:
        names = os.listdir(paths.path(ASKED))
    except OSError:
        return []
    return [paths.path(ASKED, n) for n in sorted(names)
            if n.startswith(ref + ".") and n.endswith(".pending.json")]


def _recent_path(ref: str) -> str:
    return paths.path(ASKED, "%s.recent.json" % ref)


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
def _locked(p: str, wait: float | None = None):
    """One reader-writer at a time for a pending file (flock on a sibling
    .lock). A reply written between another run's read and its write was lost,
    and two reruns could each see the other's state as theirs (measured 323 of
    400, 2026-10-01). Without a lock to be had the run goes on unlocked: a hook
    must not fail, or wait for good, on it."""
    fd = _lock(p + ".lock", wait)
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
    """A request lives ASK_MAX from the moment it was made. A person may be
    away for a quarter of an hour before answering, and an ask that was gone
    after TTL had them answer into nothing and be asked again (2026-10-01).
    Once they have replied, the reply is good for TTL, and no longer than
    ASK_MAX after the request."""
    asked = float(entry.get("t") or 0)
    replied = entry.get("verdict_t")
    return now - asked > ASK_MAX or (bool(replied) and now - float(replied) > TTL)


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


def _shrink(texts: list, excess: int, floor: int) -> list:
    """`texts` shorter by `excess` characters in all, as far as `floor` (at
    least 1) allows: each gives up a share in proportion to what it can spare,
    rounded up so the total is not short, and a cut one ends with `…`."""
    spare = [max(len(t) - floor, 0) for t in texts]
    if excess <= 0 or not sum(spare):
        return texts
    take = min(excess, sum(spare))
    cuts = [-(-take * s // sum(spare)) for s in spare]
    return [t[:len(t) - c - 1] + "…" if c else t for t, c in zip(texts, cuts)]


def answers_text(data: dict) -> str | None:
    """The person's choices in an AskUserQuestion result, one `<question> ->
    <answer>` per question (the answer is the option's label, or what they
    wrote under Other), with their notes when they gave any. Not quoted: the
    show step quotes the whole reply, and nested quotes read as `""Q" -> "A""`
    (2026-10-01). From the hook's `tool_response`, which carries the answers
    (Claude Code 2.1.286 also puts them in `tool_input`, so either would do). A
    result that is plain text comes through as it is. Past VERDICT_MAX the
    questions are shortened first, then the notes, and only then the answers,
    so the cut at VERDICT_MAX never takes the last answer whole."""
    out = data.get("tool_response")
    if isinstance(out, list):
        out = " ".join(b.get("text") or "" for b in out if isinstance(b, dict))
    if isinstance(out, str):
        return out.strip() or None
    if not isinstance(out, dict) or not isinstance(out.get("answers"), dict) or not out["answers"]:
        return None
    marks = out.get("annotations") if isinstance(out.get("annotations"), dict) else {}
    asked, answered, noted = [], [], []
    for question, answer in out["answers"].items():
        note = (marks.get(question) or {}).get("notes") if isinstance(marks.get(question), dict) \
            else None
        q = str(question)
        asked.append(q if len(q) <= QUESTION_MAX else q[:QUESTION_MAX - 1] + "…")
        answered.append(str(answer))
        noted.append(note.strip() if isinstance(note, str) else "")

    def said() -> str:
        return "; ".join("%s -> %s%s" % (q, a, " (notes: %s)" % n if n else "")
                         for q, a, n in zip(asked, answered, noted))
    for parts, floor in ((asked, 20), (noted, 20), (answered, 1)):
        parts[:] = _shrink(parts, len(said()) - VERDICT_MAX, floor)
    return said()


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
            remember(me, text)
    else:
        return None
    return _write(me, m.group(1), m.group(2)) if m else None


def _fresh_pending(me: dict | None, p: str) -> tuple:
    """(path, entry) of this session's unexpired request at `p`, or (None,
    None). An expired one is removed here: it holds up to VERDICT_MAX characters
    of the person's words. Called with the lock held."""
    if not me or not me.get("ref"):
        return None, None
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


def note_verdict(me: dict | None, text: str, wait: float | None = None) -> bool:
    """Keep the person's latest message after a request as its verdict. It
    replaces an earlier one (user decision, 2026-10-01): the first message is
    often a question ("does that delete the other records too?") and the yes
    or no comes after it. A new reply has not been shown to the agent yet.

    It goes to every request the session has open: the person may be answering
    any of them, and what each shows the agent is the words, for the agent to
    judge against what it asked. Called from the hook, so the lock is waited
    for HOOK_LOCK_WAIT, not as long as a command would."""
    if not me or not me.get("ref") or not text.strip():
        return False
    wait = HOOK_LOCK_WAIT if wait is None else wait
    noted = False
    for p in pending_files(me["ref"]):  # every prompt comes by here: one listdir
        try:
            with _locked(p, wait):
                _, entry = _fresh_pending(me, p)
                if not entry:
                    continue
                entry.update({"verdict": text.strip()[:VERDICT_MAX], "verdict_t": time.time(),
                              "shown": False})
                entry.pop("old_hook", None)     # this reply came by a hook that marks it
                paths.write_json(p, entry, mode=0o600)
                noted = True
        except OSError:
            continue                    # a folder that cannot be written keeps no reply
    return noted


def remember(me: dict | None, text: str) -> None:
    """Keep the person's last RECENT_KEEP prompts of this session, for
    `_from_request`. Not a command, not text the harness wrote (record only
    passes what could be a reply), and a prompt a second hook registration saw
    a moment ago is the same one."""
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not text.strip():
        return
    now, p = time.time(), _recent_path(me["ref"])
    entry = paths.read_json(p)
    kept = [x for x in (entry or {}).get("prompts", []) if isinstance(x, dict)] \
        if isinstance(entry, dict) and entry.get("session_id") == str(me.get("session_id") or "") \
        else []
    text = text.strip()[:VERDICT_MAX]
    if kept and kept[-1].get("text") == text and now - float(kept[-1].get("t") or 0) < 5:
        return
    kept.append({"text": text, "t": now})
    paths.write_json(p, {"session_id": str(me.get("session_id") or ""), "t": now,
                         "prompts": kept[-RECENT_KEEP:]}, mode=0o600)


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


def _spend(me: dict, verdict: str) -> None:
    """Mark the prompts of this session that say `verdict` as used, so the words that
    approved one thing cannot approve another (2026-10-02: the "응" that linked one
    folder let `--reply "응"` link the next). Called with the ask's lock held, once a
    verdict has passed or been taken for a request; a verdict that is no prompt (a form's
    answer) marks nothing. Best effort: the ask is already used up, and a folder that
    cannot be written keeps no marks."""
    p = _recent_path(me["ref"])
    entry = paths.read_json(p)          # read as late as possible: a hook appends to it unlocked
    if not isinstance(entry, dict) or entry.get("session_id") != str(me.get("session_id") or ""):
        return
    said = _norm(verdict)
    hit = [x for x in entry.get("prompts") or []
           if isinstance(x, dict) and not x.get("used") and _norm(x.get("text")) == said]
    if not said or not hit:
        return
    for x in hit:
        x["used"] = True
    try:
        paths.write_json(p, entry, mode=0o600)
    except OSError:
        pass


# Appended to what the agent is shown with a reply an older hook stored. The
# hooks of a session started before an update keep running the old code, and
# 0.4.14's kept only the first message after an ask (new ones mark theirs
# `shown`, which an old one never writes; 2026-10-01). It is not let go of: the
# first show marks the ask `old_hook`, so every later show carries the note and
# the pass line says it too, for the agent and the log. It does not block (ease
# of connection, and the first message is the person's own words).
OLD_HOOK = ("Note: this session's xsm hooks are older than this xsm command, so only their first "
            "message after the ask was kept. If they said anything else since, it is not here, "
            "and you must not run this again on it: tell them a new session is needed for the "
            "update, or use the MCP form tool if there is one.")


def _take(me: dict, p: str, verb: str, want: str, want_here: str | None) -> tuple:
    """take_or_request's step on the ask at `p`, with its lock held: (reply, go,
    how it was kept). An OSError from it means the reply could not be marked or
    used: the folder cannot be written."""
    p, entry = _fresh_pending(me, p)
    if not p or entry.get("verdict") is None or not _matches(entry, verb, want, want_here):
        return None, False, True
    now = time.time()
    if not entry.get("shown"):
        if "shown" not in entry:
            entry["old_hook"] = True
        entry.update({"shown": True, "shown_t": now})
        paths.write_json(p, entry, mode=0o600)
        return entry["verdict"], False, OLD_HOOK if entry.get("old_hook") else True
    kept = OLD_HOOK if entry.get("old_hook") else True
    if now - float(entry.get("shown_t") or 0) < SHOW_DELAY:
        return entry["verdict"], False, kept
    try:
        os.unlink(p)
    except FileNotFoundError:
        return None, False, True        # used by another reader first
    _drop(p)                            # the lock too
    global used_via
    used_via = entry.get("via")
    _spend(me, entry["verdict"])
    return entry["verdict"], True, kept


def _record(me: dict, p: str, verb: str, want: str, want_here: str | None) -> None:
    """Record the ask at `p`, with the lock held. One already on record for
    exactly this stays as it is, with the time it was made: a run before the
    person was asked pushed ASK_MAX out each time (2026-10-01)."""
    _, entry = _fresh_pending(me, p)
    if entry and _matches(entry, verb, want, want_here):
        return
    paths.write_json(p, {
        "verb": verb, "target": want, "here": want_here, "cwd": me.get("cwd") or "",
        "t": time.time(), "session_id": str(me.get("session_id") or ""),
        "verdict": None}, mode=0o600)


# A reply given on the command line, as `--reply "<their words>"` (set by the CLI
# for one run). The way to be heard when the hook that keeps replies did not run or
# could not write (a session started before an update, a state folder the hook could
# not reach). What it may stand for is `_their_words`: the agent cannot approve on
# words of its own.
supplied: str | None = None
# How the reply the last take_or_request used up reached xsm: "flag" (the agent gave
# it with --reply), "request" (the person's own prompt), or None (the hook kept it
# as their answer to the ask). Reset at the start of every take_or_request.
used_via: str | None = None


def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def _their_words(me: dict, entry: dict | None, text: str, elsewhere: set | None = None) -> str | None:
    """The person's own words that `--reply` text stands for, or None when it
    stands for nothing the person wrote (2026-10-02: an agent could pass
    `--reply "yes please"` twice and approve alone).

    A prompt the hook kept for this session (recent.json: the last RECENT_KEEP, young
    enough, not already used for a verdict or a request) or the verdict the hook stored
    on the ask counts when either contains the other, whitespace and case aside; what is
    returned is that record, so the agent's own wording is never what the verdict says.
    `elsewhere` holds the replies sitting on this session's other open asks: those
    words answer something else. A prompt from before the ask still counts: the agent
    often asks in the conversation first and runs the command after the person said yes,
    and asking them again is the ping-pong the person refused (review of PR #11,
    2026-10-02); words already spent on a verdict never count twice. A session the hook
    never wrote a record for (an older hook, a state folder it cannot write) has nothing
    to check against, and the text is taken as given: `--reply` is how the person is
    heard there. Called with the lock held."""
    now, saved = time.time(), paths.read_json(_recent_path(me["ref"]))
    hooked = isinstance(saved, dict) and saved.get("session_id") == str(me.get("session_id") or "")
    kept = [x["text"] for x in reversed((saved.get("prompts") or []) if hooked else [])
            if isinstance(x, dict) and isinstance(x.get("text"), str) and not x.get("used")
            and now - float(x.get("t") or 0) <= ASK_MAX
            and _norm(x["text"]) not in (elsewhere or set())]
    if entry and entry.get("via") != "flag" and isinstance(entry.get("verdict"), str):
        kept.append(entry["verdict"])       # the hook's, or the person's own request
    want = _norm(text)
    for words in kept:
        said = _norm(words)
        if said and (want in said or said in want):
            return words
    return None if hooked else text


def _supply(me: dict, p: str, verb: str, want: str, want_here: str | None, text: str) -> None:
    """Keep `text`, with the lock held, as the reply to the ask at `p` (the ask
    is made if there is none): what the hook would have done, for words the person
    wrote (`_their_words`). Words that are not theirs are refused in a plain line and
    the run goes on as if no flag was given. The same words given again change
    nothing, so a run that passes `--reply` twice shows the reply once and then goes
    ahead."""
    text = text.strip()[:VERDICT_MAX]
    _, earlier = _fresh_pending(me, p)
    _record(me, p, verb, want, want_here)
    _, entry = _fresh_pending(me, p)
    if not text or not entry or not _matches(entry, verb, want, want_here):
        return
    if entry.get("via") == "flag" and isinstance(entry.get("verdict"), str):
        said, given = _norm(entry["verdict"]), _norm(text)
        if said and (given in said or said in given):
            return          # the same words again: already kept, nothing to warn about
    elsewhere = set()
    for other in pending_files(me["ref"]):
        if other == p:
            continue
        _, them = _fresh_pending(me, other)
        if them and isinstance(them.get("verdict"), str):
            elsewhere.add(_norm(them["verdict"]))
    words = _their_words(me, entry, text, elsewhere)
    if words is None:
        print("xsm: --reply is ignored: it has to be your user's own words from this session, "
              "in answer to your question, and these match nothing they wrote here that has not "
              "already been used. Ask them in plain words; xsm keeps their answer itself, so run "
              "the command again after they reply, without --reply.", file=sys.stderr)
        return
    if entry.get("verdict") == words:
        return
    entry.update({"verdict": words, "verdict_t": time.time(), "shown": False, "via": "flag"})
    entry.pop("old_hook", None)
    paths.write_json(p, entry, mode=0o600)
    paths.append_jsonl("decisions.jsonl", {"event": "consent-flag", "verb": verb, "target": want,
                                           "verdict": words[:200], "via": AGENT_SUPPLIED})


def _mentions(text: str, name: str) -> bool:
    """Whether `text` names `name` as a word or path of its own: not inside a
    longer one ("repo-b" is in "repo-b?" and "../repo-b", not in "repo-b2"), and
    followed by Korean particles as they are written ("repo-b에")."""
    return len(name) >= 3 and re.search(
        r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_-])" % re.escape(name), text, re.I) is not None


def _names_for(verb: str, target: str, want: str) -> set:
    """What a person may call what is being decided: the target as typed and as
    resolved, and for a folder its last name and its `~` form."""
    names = {target, want}
    if verb in ("link", "reach"):
        names |= {os.path.basename(want.rstrip("/")), os.path.basename(target.rstrip("/"))}
        home = os.path.expanduser("~")
        if want.startswith(home + "/"):
            names.add("~" + want[len(home):])
    return {n for n in names if n}


def _session_names(verb: str, want: str) -> set:
    """The names of the sessions a decision is about: the running sessions in a
    folder, the session to unblock, the registered sender of a held message. For
    when `_names_for` finds nothing. Lazy: it reads the registry."""
    from . import config, registry
    names = set()
    if verb in ("link", "reach"):
        root = os.path.realpath(want)
        for rec in registry.records():
            if rec.get("state") in ("live", "unknown") and \
                    config.project_root(rec.get("cwd") or "/") == root:
                names |= {rec.get("name"), rec.get("ref")}
    elif verb == "unblock":
        for rec in registry.records():
            if rec.get("ref") == want:
                names |= {rec.get("name")}
    elif verb == "held-deliver":
        # The registry's own name for the session the header's ref names, when that
        # is one session: the claimed `from` is text the sender wrote (2026-10-02).
        held = paths.read_json(paths.path(paths.HELD, "%s.json" % want))
        ref = ((held or {}).get("header") or {}).get("ref") if isinstance(held, dict) else None
        known = [rec for rec in registry.records() if ref and rec.get("ref") == ref]
        if len(known) == 1:
            names |= {known[0].get("name"), known[0].get("ref")}
    return {n for n in names if n}


# What a hold says when the message lied about who it is from. The person's request
# is never taken as the reply to deliver one of those (2026-10-02).
FORGED = ("not the socket of", "is not registered", "is not a Claude session")


def _forged(held_name: str) -> bool:
    held = paths.read_json(paths.path(paths.HELD, "%s.json" % held_name))
    reason = str(held.get("reason") or "") if isinstance(held, dict) else ""
    return any(mark in reason for mark in FORGED)


def _latest_request(me: dict) -> dict | None:
    """The person's latest prompt of this session, if it is young enough (ASK_MAX)
    and short enough (REQUEST_MAX) to be a request, and was not already taken for
    one."""
    entry = paths.read_json(_recent_path(me["ref"]))
    if not isinstance(entry, dict) or entry.get("session_id") != str(me.get("session_id") or ""):
        return None
    last = (entry.get("prompts") or [None])[-1]
    if not isinstance(last, dict) or last.get("used") or not isinstance(last.get("text"), str) \
            or len(last["text"]) > REQUEST_MAX or time.time() - float(last.get("t") or 0) > ASK_MAX:
        return None
    return last


def _from_request(me: dict, p: str, verb: str, target: str, want: str,
                  want_here: str | None) -> str | None:
    """When there is no reply, the person's own request may be one (A4, user
    decision 2026-10-01: they asked for it; do not ask them to say it again).
    Their latest prompt counts when it names what is being decided (the folder
    by path or last name, the project, a session in it by name or ref) and says
    what they want done (INTENT_RE). That prompt becomes the reply, already
    shown, so the next run goes ahead: the agent still reads the words first, and
    "don't connect repo-b" is a no it will see. It is used once. Conservative on
    purpose: no name or no intent, and the person is asked as before. Switched off
    by the policy reply_from_request. Called with the lock held."""
    from . import config
    if not config.policy("reply_from_request") or (verb == "held-deliver" and _forged(want)):
        return None
    last = _latest_request(me)
    if not last or not INTENT_RE.search(last["text"]):
        return None
    names = _names_for(verb, target, want)
    if not any(_mentions(last["text"], n) for n in names) and \
            not any(_mentions(last["text"], n) for n in _session_names(verb, want)):
        return None
    now = time.time()
    paths.write_json(p, {
        "verb": verb, "target": want, "here": want_here, "cwd": me.get("cwd") or "", "t": now,
        "session_id": str(me.get("session_id") or ""), "verdict": last["text"], "verdict_t": now,
        "shown": True, "shown_t": now, "via": "request"}, mode=0o600)
    _spend(me, last["text"])
    paths.append_jsonl("decisions.jsonl", {"event": "consent-request", "verb": verb,
                                           "target": want, "verdict": last["text"][:200]})
    return last["text"]


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
      it, for a reply an older hook stored (on every show of it). The reply is
      the person's request itself when it named this and said what to do
      (`_from_request`), or the words given with `--reply` (`supplied`).
    - (reply, True, kept): the reply that was shown, is still the latest and was
      shown at least SHOW_DELAY ago, which is used up: the caller goes ahead,
      once, and says so with approved_line. A rerun sooner than that gets the
      reply shown again, so a line that runs the command twice does not pass on
      its own showing.

    One locked step, the read and the write together (_locked): a reply landing
    between a take and a separate request was overwritten unseen (adversarial
    check, 2026-10-01). Each thing asked about has its own request file, so one
    ask does not erase the reply to another."""
    global used_via
    used_via = None
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not target:
        return None, False, False
    want, want_here = _wanted(me, verb, target, here)
    p = _pending_path(me["ref"], digest(verb, want))
    try:
        with _locked(p):
            if supplied:
                _supply(me, p, verb, want, want_here, supplied)
            reply, go, kept = _take(me, p, verb, want, want_here)
            if reply is None and os.path.exists(_pending_path(me["ref"])):
                # The single slot an older version wrote: its reply is still read.
                with _locked(_pending_path(me["ref"])):
                    reply, go, kept = _take(me, _pending_path(me["ref"]), verb, want, want_here)
            if reply is None:
                _record(me, p, verb, want, want_here)
                reply = _from_request(me, p, verb, target, want, want_here)
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
    p = _pending_path(me["ref"], digest(verb, want))
    try:
        with _locked(p):
            _record(me, p, verb, want, want_here)
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
        elif name.endswith(".recent.json"):
            # The person's last prompts: of use to an ask for ASK_MAX, then their words
            # go, and the file stays as the record that a hook wrote here (it is what
            # tells a session with a hook from one without, `_their_words`) until
            # HOOK_SEEN_MAX.
            entry = paths.read_json(p)
            try:
                age = now - float(entry.get("t") or 0) if isinstance(entry, dict) \
                    else now - os.path.getmtime(p)
            except (TypeError, ValueError, OSError):
                continue
            wipe = isinstance(entry, dict) and age <= HOOK_SEEN_MAX and entry.get("prompts")
            if age > HOOK_SEEN_MAX or (age > ASK_MAX and (wipe or not isinstance(entry, dict))):
                if not dry_run:
                    try:
                        if wipe:
                            fresh = paths.read_json(p)      # a prompt may have come since
                            if isinstance(fresh, dict) and fresh.get("t") == entry.get("t"):
                                paths.write_json(p, dict(fresh, prompts=[]), mode=0o600)
                        else:
                            os.unlink(p)
                    except OSError:
                        continue
                removed.append(name)
            continue
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


def how(runtime: str | None) -> str:
    """How the agent may ask. A Claude Code agent may use its question tool: the
    answer it gets is kept as the reply (answers_text); no other runtime's is."""
    return "in plain words, or with your question tool (AskUserQuestion)" if runtime == "claude" \
        else "in plain words"


def asks(what: str, tail: str = "", runtime: str | None = None) -> str:
    """What the agent is told when nobody has been asked yet: ask first, then
    run the command again to be shown the reply, then once more on a yes. The
    order was left implicit and agents reran before asking (2026-10-01)."""
    return ("%s needs your user's yes. First ask them, %s, whether to go ahead, and "
            "wait for their answer. After they answer, run this same command again: xsm keeps "
            "their latest answer in this session as the verdict, and this run shows you that "
            "reply without acting on it. If it is a yes, run the command once more to go ahead; "
            "if it is a no or a question, leave it and answer them. Use --reply only when they "
            "have answered the question you asked them and this run still shows no reply (xsm's "
            "hook did not record it): run it again with --reply \"<their answer, exactly as they "
            "wrote it>\". Never use it before you have asked, or with what they said earlier, "
            "such as their original request.%s"
            % (what, how(runtime), tail))


def shown_refusal(reply: str, what: str, kept=None) -> str:
    """What the agent is told with a reply it has not seen: the words, and
    that this run did not go ahead. `kept` is take_or_request's: a string
    (OLD_HOOK) is added."""
    text = ('your user replied: "%s". If that is a yes to %s, run this same command again to '
            'go ahead; if it is a no or a question, do not: answer them, and their next '
            'message replaces it.' % (reply, what[:1].lower() + what[1:]))
    return text + " " + kept if isinstance(kept, str) else text


def approved_line(verdict: str, kept=None, via: str | None = None) -> str:
    """What a run prints when it goes ahead on a reply. `kept` is take_or_request's:
    a string (OLD_HOOK) says the reply came from an older hook, so the output and
    the log show it too. `via` is used_via: a reply the agent gave with --reply says
    so, because the hook did not keep it."""
    return 'approved on your user\'s reply: "%s"%s%s' % (
        verdict.replace("\n", " ")[:200],
        " (old hooks: their first message after the ask)" if isinstance(kept, str) else "",
        " (given with --reply by the agent, not kept by a hook)" if via == "flag" else "")


def approved(verb: str, target: str, verdict: str, kept=None, name: str | None = None) -> str:
    """A run goes ahead on the person's reply: record it (decisions.jsonl,
    `event: consent`) and return the line the run prints. The CLI commands,
    workers.use_grant and the one-yes send (connect.py) all say it this way. A reply
    the agent gave with --reply is marked `via: agent-supplied` (used_via)."""
    record = {"event": "consent", "verb": verb, "target": target, "verdict": verdict, "by": name}
    if isinstance(kept, str):
        record["old_hooks"] = True              # an older hook kept this reply
    if used_via == "flag":
        record["via"] = AGENT_SUPPLIED
    paths.append_jsonl("decisions.jsonl", record)
    return approved_line(verdict, kept, used_via)


def by(verdict: str | None) -> str:
    """Who decided, as a link, join or reach records it: the person's own reply
    when one was given, else the user. A reply the agent gave with --reply is
    marked, so the record says it was not typed into xsm by them (used_via)."""
    person = os.environ.get("USER") or "person"
    if not verdict:
        return person
    return "%s, replying%s: %s" % (person, " (%s)" % AGENT_SUPPLIED if used_via == "flag" else "",
                                   verdict)


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
                % (how(runtime), command))
    return ("Run `%s` in your shell first: it records that you are asking and tells you what to "
            "ask. Then ask your user, %s, whether to go ahead; after they answer, run it again to "
            "be shown their reply, and once more on a yes." % (command, how(runtime)))


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
