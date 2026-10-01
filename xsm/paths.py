"""State locations and small file helpers.

Everything xsm knows lives under XSM_HOME (default ~/.xsm), never inside a
CONFIG_DIR: one registry has to be visible from every profile and repo
(S1, S9 E3). Writes are atomic so a hook that dies mid-write cannot leave a
half-written record behind.
"""
from __future__ import annotations

import errno
import json
import os
import tempfile
import time

HOME = os.path.expanduser(os.environ.get("XSM_HOME", "~/.xsm"))

SESSIONS = "sessions"
HELD = "held"
LEDGER = "ledger"
MCP = "mcp"                 # one beacon per running xsm MCP server (Codex thread liveness)
ATTEMPTS = "attempts"       # one file per task lineage: how often it has been tried, and how it went


def path(*parts: str) -> str:
    return os.path.join(HOME, *parts)


# What to do when a write is refused. The MCP server runs outside a shell's
# sandbox, which is why it exists, so from a sandboxed shell it is the way
# round; the server itself replaces this (mcp.main).
SANDBOX_HINT = ("use the xsm MCP tools instead (xsm_send for a message, same target, kind and "
                "text; also xsm_inbox, xsm_post, xsm_channel): their server runs outside the "
                "sandbox")


def blocked_write(err: BaseException) -> bool:
    """Whether an OSError is the access a sandbox or a read-only folder refuses
    (EPERM, EACCES, EROFS), as opposed to a bug or a missing file."""
    return isinstance(err, PermissionError) or getattr(err, "errno", None) in (
        errno.EPERM, errno.EACCES, errno.EROFS)


def sandbox_blocked(err: BaseException) -> str:
    """The one line a refused write becomes. It starts with `sandbox-blocked`,
    the name SKILL.md tells an agent to look for, and says what to do next."""
    where = getattr(err, "filename", None) or HOME
    why = getattr(err, "strerror", None) or str(err)
    return "sandbox-blocked: xsm cannot use %s (%s); %s" % (where, why, SANDBOX_HINT)


def ensure_home() -> None:
    for sub in ("", SESSIONS, HELD, LEDGER, MCP, ATTEMPTS):
        os.makedirs(path(sub), mode=0o700, exist_ok=True)


def read_json(p: str, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def indent_of(p: str, default: int = 1) -> int:
    """The indentation a file already uses, so rewriting it does not reformat
    everything. A settings file the user edits by hand should come back from
    `xsm uninstall` byte-for-byte, not merely equal after parsing."""
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                stripped = line.lstrip(" ")
                if stripped and stripped != line:
                    return len(line) - len(stripped)
    except OSError:
        pass
    return default


def write_json(p: str, data, mode: int = 0o600, indent: int | None = None) -> None:
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    if indent is None:
        indent = indent_of(p)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".xsm-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(name: str, entry: dict) -> None:
    """Append one audit line. Never raises: audit must not break a hook."""
    entry = dict(entry)
    entry.setdefault("t", time.time())
    try:
        ensure_home()
        with open(path(name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_jsonl(name: str, limit: int | None = None) -> list:
    try:
        with open(path(name), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
