"""Installing the hooks into a home without disturbing what is already there.

These files are crowded: a Claude settings.json on this machine already holds
Orca, cctrace and the user's own prompt hooks, and ~/.codex/hooks.json holds
Orca's. So the installer never rewrites a hook it did not write. It appends
one group per event, marks the command with a trailing `#xsm-hook`, and
removes exactly the marked groups on uninstall. Every write is preceded by a
timestamped backup and followed by a re-parse.

The interpreter is pinned to an absolute path at install time. A hook that
starts under the wrong python silently stops gating (S8-g2).
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys

from . import config, paths

MARKER = "#xsm-hook"
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit")


def hook_command(runtime: str, event: str) -> str:
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "hooks", "xsm-hook.py")
    parts = [sys.executable, entry]
    if os.environ.get("XSM_HOME"):
        parts.insert(0, "XSM_HOME=%s" % os.environ["XSM_HOME"])
    return "%s %s" % (" ".join(parts), MARKER)


def _is_ours(group: dict) -> bool:
    return any(MARKER in (h.get("command") or "") for h in group.get("hooks", []))


def _settings_file(home: str, runtime: str) -> str:
    return os.path.join(home, "settings.json" if runtime == "claude" else "hooks.json")


def _backup(target: str) -> str:
    """Copy the file aside under a name nothing else will take.

    Second resolution is not enough: an install followed by an uninstall in the
    same second would overwrite the first copy, losing the pre-install state.
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = "%s.xsm-backup-%s" % (target, stamp)
    suffix = 1
    while os.path.exists(candidate):
        candidate = "%s.xsm-backup-%s-%d" % (target, stamp, suffix)
        suffix += 1
    shutil.copy2(target, candidate)
    return candidate


def plan(home: str, runtime: str) -> dict:
    """What install would change. Used by --dry-run and by doctor."""
    home = os.path.realpath(os.path.expanduser(home))
    target = _settings_file(home, runtime)
    data = paths.read_json(target)
    if data is None and os.path.exists(target):
        return {"home": home, "runtime": runtime, "file": target, "error": "file is not valid JSON"}
    data = data or {}
    hooks = data.get("hooks", {}) if isinstance(data.get("hooks"), dict) else {}
    events = CLAUDE_EVENTS if runtime == "claude" else CODEX_EVENTS
    actions = []
    for event in events:
        groups = hooks.get(event, [])
        ours = [g for g in groups if _is_ours(g)]
        want = hook_command(runtime, event)
        have = ours and ours[0]["hooks"][0].get("command") == want
        actions.append({"event": event, "others": len(groups) - len(ours),
                        "action": "keep" if have else ("replace" if ours else "add"),
                        "command": want})
    return {"home": home, "runtime": runtime, "file": target, "exists": os.path.exists(target),
            "actions": actions}


def apply(home: str, runtime: str) -> dict:
    result = plan(home, runtime)
    if result.get("error"):
        return result
    target, data = result["file"], (paths.read_json(result["file"]) or {})
    if os.path.exists(target):
        result["backup"] = _backup(target)
    hooks = data.setdefault("hooks", {})
    for action in result["actions"]:
        event = action["event"]
        groups = [g for g in hooks.get(event, []) if not _is_ours(g)]
        groups.append({"hooks": [{"type": "command", "command": action["command"], "timeout": 10}]})
        hooks[event] = groups
    paths.write_json(target, data, mode=0o644)
    if paths.read_json(target) is None:                # re-parse or roll back
        if result.get("backup"):
            shutil.copy2(result["backup"], target)
        result["error"] = "wrote invalid JSON; restored the backup"
        return result
    config.add_home(home, runtime)
    result["installed"] = True
    return result


def remove(home: str, runtime: str) -> dict:
    home = os.path.realpath(os.path.expanduser(home))
    target = _settings_file(home, runtime)
    data = paths.read_json(target)
    if data is None:
        return {"home": home, "file": target, "error": "nothing to remove or unreadable file"}
    backup = _backup(target)
    removed = 0
    hooks = data.get("hooks", {}) if isinstance(data.get("hooks"), dict) else {}
    for event in list(hooks):
        kept = [g for g in hooks[event] if not _is_ours(g)]
        removed += len(hooks[event]) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    paths.write_json(target, data, mode=0o644)
    return {"home": home, "file": target, "backup": backup, "removed": removed}


def diff(home: str, runtime: str) -> str:
    result = plan(home, runtime)
    if result.get("error"):
        return "%s: %s" % (result["file"], result["error"])
    lines = ["%s (%s)" % (result["file"], "exists" if result["exists"] else "will be created")]
    for action in result["actions"]:
        lines.append("  %-16s %-7s (untouched groups: %d)" %
                     (action["event"], action["action"], action["others"]))
        if action["action"] != "keep":
            lines.append("    + %s" % action["command"])
    return "\n".join(lines)


def doctor() -> dict:
    """Facts a person can act on, not a verdict."""
    from . import adapters, registry            # imported here to keep hook startup lean

    version = sys.version_info
    decisions = paths.read_jsonl("decisions.jsonl", limit=200)
    errors = [d for d in decisions if "internal error" in (d.get("reason") or "")]
    report = {
        "xsm_home": paths.HOME,
        "interpreter": sys.executable,
        "interpreter_ok": version >= (3, 9),
        "codex_binary": adapters.codex_bin(),
        "homes": config.homes(),
        "installs": [plan(h["path"], h["runtime"]) for h in config.homes()],
        "sessions": {"registered": len(registry.records()),
                     "live": len([r for r in registry.records() if r["state"] == "live"]),
                     "unregistered": len(registry.unregistered())},
        "decisions_seen": len(decisions),
        "hook_errors_recent": len(errors),
        "held": len(os.listdir(paths.path(paths.HELD))) if os.path.isdir(paths.path(paths.HELD)) else 0,
        "limits": [
            "A peer message without the xsm envelope cannot be told apart from your own typing "
            "inside a hook, so it passes the gate (S8-g2). Set crossSessionInbound to \"hold\" "
            "if you need every peer message to stop for review — that holds xsm messages too.",
            "This gate records consent and scope. It does not stop an agent that can edit "
            "~/.xsm or your settings directly (S8-c).",
        ],
    }
    return report
