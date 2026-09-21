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
import glob
import json
import os
import shutil
import subprocess
import sys

from . import config, paths

MARKER = "#xsm-hook"
FILE_MARKER = "<!-- xsm-managed -->"        # commands we wrote, and may remove
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit")


INTERPRETER = "interpreter"


def resolve_python(spec: str | None) -> str:
    """The absolute interpreter the hooks will run under.

    A path is taken as given. A version like "3.12" is resolved through
    `uv python find`, which prints a path — we store that path rather than
    calling `uv run` from the hook. Pinning the interpreter keeps the version
    deterministic without putting a launcher, a PATH lookup or a possible
    download in front of the one code path that must never fail to start
    (a hook that cannot start stops gating, S8-g2).
    """
    if not spec:
        return pinned_python()          # an earlier pin stands until it is replaced
    if os.path.isabs(spec) or os.sep in spec:
        return os.path.realpath(os.path.expanduser(spec))
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv):
        out = subprocess.run([uv, "python", "find", spec], capture_output=True, text=True,
                             timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    found = shutil.which(spec) or shutil.which("python%s" % spec)
    if found:
        return os.path.realpath(found)
    raise ValueError("cannot resolve %r to an interpreter; pass an absolute path" % spec)


def pinned_python() -> str:
    return (paths.read_json(paths.path(INTERPRETER), {}) or {}).get("path") or sys.executable


def pin_python(path: str) -> dict:
    version = ""
    try:
        out = subprocess.run([path, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=30)
        version = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    record = {"path": path, "version": version}
    paths.write_json(paths.path(INTERPRETER), record, mode=0o644)
    return record


def hook_command(runtime: str, event: str) -> str:
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "hooks", "xsm-hook.py")
    parts = [pinned_python(), entry]
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


def launcher() -> str:
    """The absolute `xsm` a slash command should run. The launcher in the repo
    works wherever it is called from, so commands never depend on PATH."""
    return os.path.join(REPO, "bin", "xsm")


def command_files() -> list:
    pattern = os.path.join(REPO, "commands", "xsm-*.md")
    return sorted(glob.glob(pattern))


def install_commands(home: str) -> list:
    """Write the slash commands into a Claude home with the launcher path filled
    in. Written rather than symlinked because the path has to be substituted."""
    target_dir = os.path.join(home, "commands")
    os.makedirs(target_dir, exist_ok=True)
    written = []
    for source in command_files():
        body = open(source, encoding="utf-8").read().replace("{{XSM}}", launcher())
        target = os.path.join(target_dir, os.path.basename(source))
        existing = _read_text(target)
        if existing is not None and FILE_MARKER not in existing:
            continue                        # someone else's command of the same name
        if existing != body:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(body)
        written.append(target)
    return written


def skill_state(home: str) -> tuple:
    """(state, detail) for the skill in this home.

    linked        our symlink, in step with the repo
    copy-current  a copied SKILL.md identical to ours
    copy-stale    a copied SKILL.md that has fallen behind
    nested-link   a link made *inside* an existing directory (ln -sfn into a dir)
    foreign       something else lives there; we leave it alone
    absent        nothing there yet
    """
    target = os.path.join(home, "skills", "xsm")
    source = os.path.join(REPO, "skills", "xsm")
    if os.path.islink(target):
        return ("linked" if os.path.realpath(target) == os.path.realpath(source)
                else "foreign"), target
    if not os.path.exists(target):
        return "absent", target
    nested = os.path.join(target, "xsm")
    if os.path.islink(nested) and os.path.realpath(nested) == os.path.realpath(source):
        return "nested-link", nested
    copied = os.path.join(target, "SKILL.md")
    ours = os.path.join(source, "SKILL.md")
    if os.path.isfile(copied):
        try:
            same = open(copied, encoding="utf-8").read() == open(ours, encoding="utf-8").read()
        except OSError:
            same = False
        return ("copy-current" if same else "copy-stale"), copied
    return "foreign", target


def install_skill(home: str) -> tuple:
    """Link the skill so the session knows the commands exist. A link keeps it in
    step with the repo; anything already there that is not ours is left be."""
    state, detail = skill_state(home)
    if state != "absent":
        return state, detail
    target = os.path.join(home, "skills", "xsm")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.symlink(os.path.join(REPO, "skills", "xsm"), target)
    return "linked", target


def statusline_command() -> str:
    return "%s statusline %s" % (launcher(), MARKER)


def install_statusline(home: str) -> str:
    """Opt-in. Claude has one statusLine per home, so an existing one is never
    replaced — the user would lose theirs without asking."""
    target = _settings_file(home, "claude")
    data = paths.read_json(target, {}) or {}
    current = data.get("statusLine")
    if current and MARKER not in json.dumps(current):
        return "kept-existing"
    want = {"type": "command", "command": statusline_command()}
    if current == want:
        return "already"
    if os.path.exists(target):
        _backup(target)
    data["statusLine"] = want
    paths.write_json(target, data, mode=0o644)
    return "installed"


def remove_statusline(home: str) -> bool:
    target = _settings_file(home, "claude")
    data = paths.read_json(target)
    if not data or MARKER not in json.dumps(data.get("statusLine") or {}):
        return False
    _backup(target)
    del data["statusLine"]
    paths.write_json(target, data, mode=0o644)
    return True


def remove_commands(home: str) -> int:
    removed = 0
    for source in command_files():
        target = os.path.join(home, "commands", os.path.basename(source))
        body = _read_text(target)
        if body is not None and FILE_MARKER in body:
            os.unlink(target)
            removed += 1
    return removed


def remove_skill(home: str) -> bool:
    target = os.path.join(home, "skills", "xsm")
    source = os.path.join(REPO, "skills", "xsm")
    if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
        os.unlink(target)
        return True
    return False


def _read_text(p: str):
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


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
        # "keep" means exactly one marked group with exactly the right command:
        # a duplicate or a stale path still needs replacing.
        have = len(ours) == 1 and ours[0]["hooks"][0].get("command") == want
        actions.append({"event": event, "others": len(groups) - len(ours),
                        "action": "keep" if have else ("replace" if ours else "add"),
                        "command": want})
    return {"home": home, "runtime": runtime, "file": target, "exists": os.path.exists(target),
            "actions": actions}


def apply(home: str, runtime: str) -> dict:
    result = plan(home, runtime)
    if result.get("error"):
        return result
    if all(a["action"] == "keep" for a in result["actions"]) and result["exists"]:
        # Installing twice must not leave a trail of identical backups.
        result["installed"] = True
        result["unchanged"] = True
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
        "interpreter": pinned_python(),
        "interpreter_pinned": paths.read_json(paths.path(INTERPRETER)) is not None,
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
