"""Turning a name into one session (ADR-0008 draft B).

Names are not unique. Claude's own uniqueness check is off on this account and
Codex never had one, so two sessions can share a name inside one home, let
alone across homes. The rule is therefore: qualify, and when a name still
matches more than one live session, fail with the candidates rather than
guess. An error is cheaper than a message delivered to the wrong session
(S5-2 showed how quiet a misdelivery can be).
"""
from __future__ import annotations

import re

from . import config, identity, registry

QUALIFIED_RE = re.compile(r"^(?P<name>.+?)(?:@(?P<alias>[^@\s]+))?(?:\s*\[(?P<ref>[0-9a-f]{6})\])?$")


class Resolution:
    def __init__(self, status: str, record=None, candidates=None, reason: str = ""):
        self.status = status              # resolved | ambiguous | not-found | offline-only
        self.record = record
        self.candidates = candidates or []
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.status == "resolved"


def _pool(include_offline: bool) -> list:
    rows = registry.records()
    return rows if include_offline else [r for r in rows if r.get("state") != "stale"]


def _live_addresses(limit: int = 8) -> list:
    """Addresses worth suggesting when a name did not match: a caller working
    from a list it fetched minutes ago should not have to fetch it again."""
    rows = registry.records()
    live = [r for r in rows if r.get("state") == "live"]
    ordered = sorted(live or rows, key=lambda r: r.get("updated", 0), reverse=True)
    return ordered[:limit]


def resolve(target: str, include_offline: bool = False) -> Resolution:
    target = (target or "").strip()
    if not target:
        return Resolution("not-found", reason="empty target")

    for prefix in ("claude:", "codex:", "ref:"):
        if target.startswith(prefix):
            key, value = prefix[:-1], target[len(prefix):]
            for rec in _pool(True):
                if key == "ref" and rec.get("ref") == value:
                    return Resolution("resolved", rec)
                if key in ("claude", "codex") and rec.get("runtime") == key \
                        and rec.get("session_id") == value:
                    return Resolution("resolved", rec)
            return Resolution("not-found", reason="no session with %s" % target,
                              candidates=_live_addresses())

    match = QUALIFIED_RE.match(target)
    if not match:
        return Resolution("not-found", reason="unparsable target")
    name, alias, ref = match.group("name").strip(), match.group("alias"), match.group("ref")

    # The last @ is a qualifier only when it names a home we know about;
    # Codex allows @ inside a thread name (S7).
    known = {h.get("alias") for h in config.homes()} | {r.get("alias") for r in _pool(True)}
    if alias and alias not in known:
        name, alias = target, None

    wanted = identity.normalize(name)
    pool = [r for r in _pool(True) if identity.normalize(r.get("name") or "") == wanted]
    if alias:
        pool = [r for r in pool if r.get("alias") == alias]
    if ref:
        pool = [r for r in pool if r.get("ref") == ref]
    if not pool:
        return Resolution("not-found", reason="no session named %r" % name,
                          candidates=_live_addresses())

    live = [r for r in pool if r.get("state") == "live"]
    usable = live or (pool if include_offline else [])
    if not usable:
        return Resolution("offline-only", candidates=pool,
                          reason="only stopped sessions match %r\n%s" % (
                              name, "\n".join(resume_hint(r) for r in pool)))
    if len(usable) > 1:
        return Resolution("ambiguous", candidates=usable,
                          reason="%d sessions match %r" % (len(usable), name))
    return Resolution("resolved", usable[0])


def resume_hint(record: dict) -> str:
    """How a stopped session could take messages again. Resuming keeps the
    session id, so the address and ref come back unchanged."""
    how = "exited cleanly (%s)" % record.get("end_reason") if record.get("state") == "ended" \
        else "stopped without saying goodbye"
    if record.get("runtime") == "codex":
        cmd = "CODEX_HOME=%s codex resume %s" % (record.get("home"), record.get("session_id"))
    else:
        cmd = "CLAUDE_CONFIG_DIR=%s claude --resume %s" % (record.get("home"), record.get("session_id"))
    return "  %s@%s [%s] %s; resume it with: %s" % (
        record.get("name"), record.get("alias"), record.get("ref"), how, cmd)


def describe(candidates) -> str:
    return "\n".join("  %s@%s [%s] %s %s%s" % (
        c.get("name"), c.get("alias"), c.get("ref"), c.get("runtime"),
        "" if c.get("state") == "live" else "(%s) " % c.get("state"), c.get("cwd") or "")
        for c in candidates)
