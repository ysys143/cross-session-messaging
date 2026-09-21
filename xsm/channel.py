"""Channels: the record people and sessions keep together (ADR-0005).

A channel is a scope (ADR-0004): a session writes to and reads from its
default project's channel, or a named project its folder joined. Messages are
records appended to `~/.xsm/channels/<key>/<YYYY-MM>.jsonl`, one `write()`
each — measured safe for 16 concurrent writers at up to 60 KB a record on
local APFS, so there is no lock and no daemon. Channels are not pruned: they
are the record that is meant to last. What goes into the repository is a
summary a person exports (`xsm channel export`), never the raw log.

Delivery is not the channel's job. Posting records; it wakes nobody. Calling a
session is still `xsm send`.

Who wrote a record is decided here, not claimed by the writer: a terminal
without agent markers is a person, a registered session is that session. A
`decision` is a person's: an agent records one only through the MCP tool that
asks the person (elicitation) and stores their answer verbatim.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
import uuid

from . import config, paths

CHANNELS = "channels"
TAGS = ("note", "question", "proposal", "result", "hypothesis", "decision")
MAX_TEXT = 32000


class ChannelError(Exception):
    pass


# --- which channels a folder may use -----------------------------------------------

def _key_for_default(scope_id: str, root: str) -> str:
    # Two repositories can share a basename; the key must not merge them.
    digest = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:6]
    return "%s-%s" % (re.sub(r"[^A-Za-z0-9._-]", "_", scope_id), digest)


def memberships(here: str) -> list:
    """[(name, key)] for every channel this folder may use: its default
    project first, then the named projects it joined."""
    default, root = config.default_project(here)
    out = [(default, _key_for_default(default, root))]
    project_root = config.project_root(here)
    for scope in config.projects():
        if any(os.path.realpath(m.get("root", "")) == project_root
               for m in scope.get("members", [])):
            out.append((scope["id"], "project-%s" % scope["id"]))
    return out


def resolve(here: str, name: str | None = None) -> tuple:
    rows = memberships(here)
    if not name:
        return rows[0]
    for row in rows:
        if row[0] == name:
            return row
    raise ChannelError("this folder is not in %r; its channels: %s"
                       % (name, ", ".join(r[0] for r in rows)))


def all_channels() -> list:
    """Every channel with records, as (key, first line's channel name)."""
    out = []
    for d in sorted(glob.glob(paths.path(CHANNELS, "*"))):
        files = sorted(glob.glob(os.path.join(d, "*.jsonl")))
        name = None
        if files:
            with open(files[0], encoding="utf-8") as fh:
                first = fh.readline()
            try:
                name = json.loads(first).get("channel")
            except ValueError:
                pass
        out.append((os.path.basename(d), name, sum(1 for f in files for _ in open(f))))
    return out


# --- who is writing ----------------------------------------------------------------

def author_here(me: dict | None) -> dict:
    from . import workers
    if workers.human_terminal():
        return {"kind": "human", "name": os.environ.get("USER") or "person"}
    if me:
        return {"kind": "agent", "name": me.get("name"), "alias": me.get("alias"),
                "ref": me.get("ref"), "runtime": me.get("runtime")}
    raise ChannelError("cannot tell who is posting: not a person at a terminal and not a "
                       "registered session")


def label(author: dict) -> str:
    if author.get("kind") == "human":
        via = " via %s" % author["via"] if author.get("via") else ""
        return "%s (person%s)" % (author.get("name"), via)
    return "%s@%s [%s]" % (author.get("name"), author.get("alias"), author.get("ref"))


# --- writing and reading --------------------------------------------------------------

def _dir(key: str) -> str:
    return paths.path(CHANNELS, key)


def read(key: str) -> list:
    rows = []
    for f in sorted(glob.glob(os.path.join(_dir(key), "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass                     # a torn line never hides the others
    return sorted(rows, key=lambda r: r.get("t", 0))


def post(channel: tuple, author: dict, text: str, tag: str = "note",
         reply_to: str | None = None, approved: dict | None = None) -> dict:
    name, key = channel
    text = (text or "").strip()
    if not text:
        raise ChannelError("nothing to post")
    if len(text) > MAX_TEXT:
        raise ChannelError("a post is at most %d characters" % MAX_TEXT)
    if tag not in TAGS:
        raise ChannelError("tag must be one of: %s" % ", ".join(TAGS))
    if tag == "decision" and author.get("kind") != "human":
        raise ChannelError("a decision is a person's. From a session, ask your user with the "
                           "xsm_decide MCP tool; it records their answer as the decision")
    root = None
    if reply_to:
        parent = next((r for r in read(key) if r.get("id") == reply_to), None)
        if not parent:
            raise ChannelError("no post %s in %s" % (reply_to, name))
        root = parent.get("root") or parent["id"]
    record = {"id": uuid.uuid4().hex[:10], "t": time.time(), "channel": name, "author": author,
              "tag": tag, "text": text, "reply_to": reply_to, "root": root}
    if approved:
        record["approved"] = approved
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    os.makedirs(_dir(key), mode=0o700, exist_ok=True)
    month = time.strftime("%Y-%m", time.localtime(record["t"]))
    fd = os.open(os.path.join(_dir(key), month + ".jsonl"),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)                # one write per record: appends do not interleave
    finally:
        os.close(fd)
    return record


# --- showing ------------------------------------------------------------------------

def _stamp(t: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def _line(r: dict, indent: str = "") -> str:
    text = r.get("text", "").replace("\n", "\n" + indent + "    ")
    out = "%s%s %s %s [%s] %s" % (indent, r["id"], _stamp(r["t"]), label(r["author"]),
                                  r.get("tag"), text)
    if r.get("approved"):
        a = r["approved"]
        out += "\n%s    (asked: %s -> answered: %s)" % (indent, a.get("question"), a.get("answer"))
    return out


def render(rows: list, tag: str | None = None, limit: int | None = None) -> str:
    """Threads in order, replies indented under their root. With a tag, a flat
    list of just those posts."""
    if tag:
        picked = [r for r in rows if r.get("tag") == tag]
        picked = picked[-limit:] if limit else picked
        return "\n".join(_line(r) for r in picked)
    roots = [r for r in rows if not r.get("root")]
    roots = roots[-limit:] if limit else roots
    replies = {}
    for r in rows:
        if r.get("root"):
            replies.setdefault(r["root"], []).append(r)
    lines = []
    for r in roots:
        lines.append(_line(r))
        lines.extend(_line(x, "    ") for x in replies.get(r["id"], []))
    return "\n".join(lines)


def export_markdown(name: str, rows: list, tag: str = "decision") -> str:
    """The summary a person commits: the chosen posts, plainly, with the
    question and answer a decision came from."""
    picked = [r for r in rows if r.get("tag") == tag]
    out = ["# %s — %s" % (tag.capitalize() + "s" if tag != "decision" else "Decisions", name), ""]
    for r in picked:
        out.append("## %s — %s" % (time.strftime("%Y-%m-%d", time.localtime(r["t"])),
                                   r["text"].splitlines()[0][:80]))
        out.append("")
        out.append(r["text"])
        out.append("")
        if r.get("approved"):
            a = r["approved"]
            out.append("- Asked: %s" % a.get("question"))
            out.append("- Answer: %s" % a.get("answer"))
        out.append("- By: %s · id `%s`" % (label(r["author"]), r["id"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
