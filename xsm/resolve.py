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
            if key == "ref":
                hits = [rec for rec in _pool(True) if rec.get("ref") == value]
            else:
                hits = [rec for rec in _pool(True) if rec.get("runtime") == key
                        and rec.get("session_id") == value]
            if len(hits) > 1:
                # A ref is 24 bits: two sessions can share one (collisions within
                # ~11k synthetic tries, 2026-09-28). Picking the first would send
                # to whichever pointer the filesystem listed first.
                return Resolution("ambiguous", candidates=hits, reason=(
                    "%d sessions share %s; address one by its session id:\n%s" % (
                        len(hits), target, "\n".join(
                            "  %s:%s  (%s@%s, %s)" % (h.get("runtime"), h.get("session_id"),
                                                      h.get("name"), h.get("alias"),
                                                      h.get("state")) for h in hits))))
            if hits:
                # Any state: an address names one session exactly, and the
                # caller decides what a stopped one means (send refuses it).
                return Resolution("resolved", hits[0])
            waiting = [r for r in registry.unregistered()
                       if (r.get("ref") == value if key == "ref"
                           else r.get("runtime") == key and r.get("session_id") == value)
                       and r.get("state") not in ("stale", "ended")]
            if waiting:
                return Resolution("unregistered", candidates=waiting,
                                  reason=_open_unregistered(waiting))
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
        # A session that has not registered may be in a home no hook has reported.
        known |= {r.get("alias") for r in registry.unregistered()}
    if alias and alias not in known:
        name, alias = target, None

    wanted = identity.normalize(name)
    pool = [r for r in _pool(True) if identity.normalize(r.get("name") or "") == wanted]
    if alias:
        pool = [r for r in pool if r.get("alias") == alias]
    if ref:
        pool = [r for r in pool if r.get("ref") == ref]
    live = [r for r in pool if r.get("state") == "live"]
    usable = live or (pool if include_offline else [])
    if not usable:
        # No live registered match. A session that is open but never registered
        # (a Codex thread that has not had its first prompt) is the likelier
        # intent than one that stopped, so it is reported first.
        waiting = [r for r in registry.unregistered()
                   if identity.normalize(r.get("name") or "") == wanted
                   and (not alias or r.get("alias") == alias)
                   and r.get("state") not in ("stale", "ended")]
        if waiting:
            reason = _open_unregistered(waiting)
            if pool:
                reason += "\nstopped sessions with the same name:\n" + \
                          "\n".join(resume_hint(r) for r in pool)
            return Resolution("unregistered", candidates=waiting, reason=reason)
    if not pool:
        return Resolution("not-found", reason="no session named %r" % name,
                          candidates=_live_addresses())
    if not usable:
        return Resolution("offline-only", candidates=pool,
                          reason="only stopped sessions match %r\n%s" % (
                              name, "\n".join(resume_hint(r) for r in pool)))
    if len(usable) > 1:
        return Resolution("ambiguous", candidates=usable,
                          reason="%d sessions match %r" % (len(usable), name))
    return Resolution("resolved", usable[0])


def _open_unregistered(waiting: list) -> str:
    """Sessions that are open and have not registered, and how each gets
    registered (the row's own `why`)."""
    return "%s is open but has not registered with xsm: %s" % (
        ", ".join("%s@%s" % (w.get("name"), w.get("alias")) for w in waiting),
        waiting[0].get("why") or "its hook has not run")


def resume_hint(record: dict) -> str:
    """How a stopped session could take messages again. Resuming keeps the
    session id, so the address and ref come back unchanged."""
    if record.get("end_reason") == "thread_replaced":
        return ("  %s@%s [%s] its Codex TUI (pid %s) opened another thread, which has no address "
                "until its first prompt or /rename; this one reopens with: CODEX_HOME=%s codex "
                "resume %s" % (record.get("name"), record.get("alias"), record.get("ref"),
                               record.get("pid"), record.get("home"), record.get("session_id")))
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
