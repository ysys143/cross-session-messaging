"""Installing the hooks into a home without disturbing what is already there.

These files are shared: a Claude settings.json or a Codex hooks.json often
already holds hooks from other tools and from the person themselves. So the
installer never rewrites a hook it did not write. It appends
one group per event, marks the command with a trailing `#xsm-hook`, and
removes exactly the marked groups on uninstall. Every write is preceded by a
timestamped backup and followed by a re-parse.

The interpreter is pinned to an absolute path at install time. A hook that
starts under the wrong python silently stops gating (S8-g2).
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import config, paths

MARKER = "#xsm-hook"
FILE_MARKER = "<!-- xsm-managed -->"        # command files earlier versions wrote
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SessionEnd lets a clean exit read as `ended` rather than `stale`. Codex has
# no SessionEnd event, so a stopped Codex session always reads as stale.
# UserPromptExpansion fires only for a slash command the person typed, never
# for a peer message, so it is where a typed `/xsm link` is taken as consent
# (consent.py, 2026-09-28). PostToolUse is installed for one tool only: the
# person's answer to AskUserQuestion is a tool result, no prompt (2026-10-01).
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "UserPromptExpansion", "PostToolUse",
                 "SessionEnd")
MATCHERS = {"PostToolUse": "AskUserQuestion"}
# No PermissionRequest for Codex: Codex has no such hook event to relay a
# question through, so a background Codex worker runs with approvals off
# inside its sandbox instead. Background Claude workers get theirs from their
# own --settings.
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit")
TIMEOUTS = {"PermissionRequest": 660}


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
    # Only a non-default state directory is written into the command. Writing
    # the default too made the command depend on whether the installing shell
    # happened to export XSM_HOME, so a reinstall rewrote every hook.
    if os.environ.get("XSM_HOME") and not _is_default_home(os.environ["XSM_HOME"]):
        parts.insert(0, "XSM_HOME=%s" % os.environ["XSM_HOME"])
    return "%s %s" % (" ".join(parts), MARKER)


def _is_default_home(value: str) -> bool:
    return os.path.realpath(os.path.expanduser(value)) == \
        os.path.realpath(os.path.expanduser("~/.xsm"))


def _same_command(have: str, want: str) -> bool:
    """Equal, or equal once a prefix naming the default state directory is
    dropped — installs made before that prefix stopped being written."""
    def norm(cmd: str) -> str:
        first, _, rest = (cmd or "").partition(" ")
        if first.startswith("XSM_HOME=") and _is_default_home(first[len("XSM_HOME="):]):
            return rest
        return cmd or ""
    return norm(have) == norm(want)


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
    """The absolute `xsm` for what xsm writes into another program's config (a
    worker's allow-list, a statusLine). The launcher in the repo works wherever
    it is called from, so those never depend on PATH."""
    return os.path.join(REPO, "bin", "xsm")


# A version folder of the xsm plugin. Links into one are the only ones the
# installer moves on its own: a link to a checkout was made by a person, and a
# refresh run from the plugin must not pull it away from their working copy.
PLUGIN_CACHE = r"/plugins/cache/xsm/xsm/[^/]+/"


def install_cli() -> str:
    """Link the launcher on PATH, replacing only a link into an older plugin version."""
    target = os.path.expanduser("~/.local/bin/xsm")
    state = "linked"
    if os.path.islink(target):
        source = os.path.realpath(target)
        if source == os.path.realpath(launcher()):
            return "current"
        repo = os.path.dirname(os.path.dirname(source))
        if not re.search(PLUGIN_CACHE + "bin/xsm$", source) or (
                os.path.exists(source) and not os.path.isfile(os.path.join(repo, "xsm", "install.py"))):
            return "foreign"
        # Every Codex home's session start runs this, and so does the Claude
        # plugin's copy of the code: a home still on an older version must not
        # take the one link back from a newer one (review, 2026-09-29).
        mine = re.search(PLUGIN_CACHE, os.path.realpath(REPO) + "/")
        if os.path.exists(source) and mine and \
                version_key(os.path.basename(repo)) > version_key(os.path.basename(os.path.realpath(REPO))):
            return "newer"
        os.unlink(target)
        state = "replaced"
    elif os.path.lexists(target):
        return "foreign"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.symlink(launcher(), target)
    return state


def version_key(version: str) -> list:
    """Sorts "0.4.10" after "0.4.9", which text order does not."""
    return [(0, int(x)) if x.isdigit() else (1, x) for x in re.split(r"[.-]", version)]


def plugin_installed(home: str) -> str | None:
    """The xsm plugin's version in this Claude home, or None.

    Both ways of installing exist (user decision, 2026-09-23): a plugin, and
    `xsm install` writing into settings.json. Both in one home would run every
    hook twice — the second run of the receive gate would see its own receipt
    and refuse the message as a duplicate — so each install path checks for
    the other."""
    entry = _plugin_entry(home)
    if entry is not None:
        return entry.get("version") or "installed"
    codex = codex_plugin(home)
    return codex["version"] if codex else None


def codex_plugin(home: str) -> dict | None:
    """{"key", "version", "root"} for the xsm plugin enabled in a Codex home, or None.

    Codex records an installed plugin as [plugins."<name>@<marketplace>"] in
    config.toml and copies it to plugins/cache/<marketplace>/<name>/<version>
    (Codex 0.158, `codex plugin add`, measured 2026-09-29)."""
    home = os.path.expanduser(home)
    for key, enabled in _codex_plugin_entries(_read_text(os.path.join(home, "config.toml")) or ""):
        name, _, market = key.partition("@")
        if name != PLUGIN_NAME or not market or not enabled:
            continue
        versions = sorted(glob.glob(os.path.join(home, "plugins", "cache", market,
                                                 PLUGIN_NAME, "*", ".codex-plugin")),
                          key=lambda p: version_key(os.path.basename(os.path.dirname(p))))
        if not versions:
            continue
        root = os.path.dirname(versions[-1])
        return {"key": key, "version": os.path.basename(root), "root": root}
    return None


def _codex_plugin_entries(text: str) -> list:
    """[(plugin key, enabled)] from a Codex config.toml. tomllib where there is
    one; without it (3.9) only the form Codex writes is read, and an `enabled`
    this reader cannot parse counts as off, as in codex_hook_states()."""
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            plugins = tomllib.loads(text).get("plugins") or {}
        except tomllib.TOMLDecodeError:
            return []
        return [(k, v.get("enabled", True) is not False) for k, v in plugins.items()
                if isinstance(v, dict)]
    out = []
    for match in re.finditer(r'^\[plugins\.(?:"([^"]+)"|\'([^\']+)\')\]\s*(?:#.*)?$', text, re.M):
        body = text[match.end():].split("\n[", 1)[0]
        switch = re.findall(r"^\s*[\"']?enabled[\"']?\s*=\s*(\S+)", body, re.M)
        out.append((match.group(1) or match.group(2), all(v == "true" for v in switch)))
    return out


def codex_direct_mcp(home: str) -> bool:
    """Whether `xsm install` left its MCP server in this Codex home's config.toml.
    `codex mcp get xsm` cannot tell: it shows the plugin's server as well."""
    text = _read_text(os.path.join(os.path.expanduser(home), "config.toml")) or ""
    return bool(re.search(r'^\[mcp_servers\.(?:%s|"%s")\]' % (MCP_NAME, MCP_NAME), text, re.M))


def _plugin_entry(home: str) -> dict | None:
    state = paths.read_json(os.path.join(os.path.expanduser(home), "plugins",
                                         "installed_plugins.json"), {}) or {}
    for key, entries in (state.get("plugins") or {}).items():
        if key == PLUGIN_NAME or key.startswith(PLUGIN_NAME + "@"):
            for entry in entries if isinstance(entries, list) else [entries]:
                return entry if isinstance(entry, dict) else {}
    return None


def _matchers(groups) -> set:
    return {g["matcher"] for g in groups if isinstance(g, dict) and g.get("matcher")} \
        if isinstance(groups, list) else set()


def plugin_missing_hooks(home: str) -> list:
    """The hook events this checkout's hooks/hooks.json has and the installed
    plugin's copy lacks, and for an event with matchers, `Event(matcher)` for
    each the copy lacks. A version string does not tell: the plugin cache kept
    a hooks.json without UserPromptExpansion, so a typed /xsm link was never
    recorded, while doctor and --refresh said the plugin kept itself up to
    date (review, 2026-09-28). Empty when there is no plugin or its folder
    cannot be read."""
    path = (_plugin_entry(home) or {}).get("installPath")
    if not path or not os.path.isdir(path):
        return []
    want = (paths.read_json(os.path.join(REPO, "hooks", "hooks.json"), {}) or {}).get("hooks") or {}
    have = (paths.read_json(os.path.join(path, "hooks", "hooks.json"), {}) or {}).get("hooks") or {}
    out = []
    for event, groups in want.items():
        wanted = _matchers(groups)
        if not wanted:
            if event not in have:
                out.append(event)
            continue
        out += ["%s(%s)" % (event, m) for m in sorted(wanted - _matchers(have.get(event)))]
    return out


def plugin_outdated_note(missing: list) -> str:
    return ("the installed plugin is older than this checkout (no %s hook): run /plugin update "
            "xsm@xsm, then start a new session" % ", ".join(missing))


def leftovers(home: str) -> list:
    """Files an earlier `xsm install` left in a home that has since moved to
    the plugin. The plugin carries its own skill, so these are dead weight — and
    a personal skills/xsm takes the bare `/xsm` from the plugin's. In a Codex
    home the hook groups and the MCP entry are left over too, and those run a
    second time beside the plugin's: the second receive gate sees the first
    one's receipt and refuses the message (review, 2026-09-29)."""
    if not plugin_installed(home):
        return []
    skill = os.path.join(home, "skills", "xsm")
    out = [skill] if os.path.lexists(skill) else []
    if codex_plugin(home):
        hooks = os.path.join(home, "hooks.json")
        data = paths.read_json(hooks, {}) or {}
        if any(_is_ours(g) for groups in (data.get("hooks") or {}).values()
               if isinstance(groups, list) for g in groups):
            out.append(hooks)
        if codex_direct_mcp(home):
            out.append(os.path.join(home, "config.toml") + " [mcp_servers.%s]" % MCP_NAME)
    return out


def clear_codex_leftovers(home: str) -> list:
    """Remove what leftovers() finds in a Codex home on the plugin; what was removed."""
    done = []
    if not codex_plugin(home):
        return done
    found = leftovers(home)
    if os.path.join(home, "hooks.json") in found and remove(home, "codex").get("removed"):
        done.append("hook groups")
    if codex_direct_mcp(home) and remove_mcp(home, "codex"):
        done.append("MCP server")
    skill = os.path.join(home, "skills", "xsm")
    state, _ = skill_state(home)
    if state in ("linked", "link-stale") or (os.path.islink(skill) and re.search(
            PLUGIN_CACHE, os.path.realpath(skill) + "/")):
        os.unlink(skill)
        done.append("skill link")
    elif remove_skill(home):
        done.append("skill copy")
    return done


def stale_copies(home: str, runtime: str = "claude") -> list:
    """Files we installed into a home that no longer match the repository.
    A copied skill in a second profile sat eight versions behind for a day
    before anyone noticed (2026-09-23), because nothing compared them."""
    if plugin_installed(home):
        return []                       # the plugin keeps itself current
    state, detail = skill_state(home)
    return [detail] if state in ("copy-stale", "link-stale") else []


def _skill_tree() -> dict:
    """{path relative to skills/xsm: text} for every file the skill ships.
    SKILL.md alone is not the skill any more: it points at references/, and a
    copy without them sends the model to a file that is not there."""
    source = os.path.join(REPO, "skills", "xsm")
    out = {}
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            out[os.path.relpath(full, source)] = _read_text(full)
    return out


def skill_state(home: str) -> tuple:
    """(state, detail) for the skill in this home.

    linked        our symlink, in step with the repo
    link-stale    a link into another version of the xsm plugin
    copy-current  a copied skill directory whose files all match ours
    copy-stale    a copied skill directory that has fallen behind
    nested-link   a link made *inside* an existing directory (ln -sfn into a dir)
    foreign       something else lives there; we leave it alone
    absent        nothing there yet
    """
    target = os.path.join(home, "skills", "xsm")
    source = os.path.join(REPO, "skills", "xsm")
    if os.path.islink(target):
        if os.path.realpath(target) == os.path.realpath(source):
            return "linked", target
        if not re.search(PLUGIN_CACHE + "skills/xsm$", os.path.realpath(target)):
            return "foreign", target
        header = (_read_text(os.path.join(target, "SKILL.md")) or "").splitlines()[:20]
        return ("link-stale" if not os.path.exists(target) or "name: xsm" in header
                else "foreign"), target
    if not os.path.exists(target):
        return "absent", target
    nested = os.path.join(target, "xsm")
    if os.path.islink(nested) and os.path.realpath(nested) == os.path.realpath(source):
        return "nested-link", nested
    if os.path.isfile(os.path.join(target, "SKILL.md")):
        # Files the copy has and we do not are left out of the comparison: they
        # are not ours to judge, and counting them would keep it stale forever.
        same = all(_read_text(os.path.join(target, rel)) == text
                   for rel, text in _skill_tree().items())
        return ("copy-current" if same else "copy-stale"), target
    return "foreign", target


def install_skill(home: str, refresh: bool = False) -> tuple:
    """Link the skill so the session knows the commands exist. A link keeps it in
    step with the repo; anything already there that is not ours is left be.

    With `refresh`, a copy that has fallen behind is rewritten, every file of
    it: it is ours, and telling a person to run `cp` is how two profiles ended
    up eight versions behind (2026-09-23)."""
    state, detail = skill_state(home)
    if state == "link-stale" and refresh:
        os.unlink(detail)
        state = "absent"
    if state == "copy-stale" and refresh:
        for rel, text in _skill_tree().items():
            target = os.path.join(detail, rel)
            if _read_text(target) != text:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(text)
        return "copy-current", detail
    if state != "absent":
        return state, detail
    target = os.path.join(home, "skills", "xsm")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.symlink(os.path.join(REPO, "skills", "xsm"), target)
    return "linked", target


# Codex's label for each event in a hook-state key and in the trust hash
# (codex-rs/hooks/src/lib.rs, hook_event_key_label, Codex 0.158).
CODEX_EVENT_KEYS = {"PreToolUse": "pre_tool_use", "PermissionRequest": "permission_request",
                    "PostToolUse": "post_tool_use", "PreCompact": "pre_compact",
                    "PostCompact": "post_compact", "SessionStart": "session_start",
                    "SessionEnd": "session_end", "UserPromptSubmit": "user_prompt_submit",
                    "SubagentStart": "subagent_start", "SubagentStop": "subagent_stop",
                    "Stop": "stop", "Interrupt": "interrupt"}


def codex_hook_hash(event: str, group: dict, handler: dict) -> str | None:
    """The hash Codex compares with a hook's trusted_hash, for one command hook
    from hooks.json; None for anything we cannot hash the way Codex does.

    Codex hashes the normalized hook, not the file text (Codex 0.158,
    codex-rs/hooks/src/engine/discovery.rs, hook_hash): the event's key label,
    the group's matcher (dropped for events that take none), and this one
    handler with its timeout defaulted — 600 s, or clamped to 1..3 s for
    SessionEnd and Interrupt — commandWindows dropped, and
    additionalContextLimit kept only where the event can emit context and it
    is not the 2500 default. That value goes through TOML to JSON with sorted
    keys and no spaces, then sha256 (codex-rs/config/src/fingerprint.rs,
    version_for_toml). Checked 2026-09-28 against the trusted_hash of 15 hooks
    Codex had recorded in real homes: all equal."""
    if (not isinstance(handler, dict) or handler.get("type") != "command"
            or event not in CODEX_EVENT_KEYS or not isinstance(handler.get("command"), str)):
        return None
    timeout = handler.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                or timeout < 0):
        return None                     # Codex would not load a hooks.json like that
    if event in ("SessionEnd", "Interrupt"):
        timeout = min(max(1 if timeout is None else timeout, 1), 3)
    else:
        timeout = max(600 if timeout is None else timeout, 1)
    normalized = {"type": "command", "command": handler["command"], "timeout": timeout,
                  "async": bool(handler.get("async", False))}
    if handler.get("statusMessage") is not None:
        normalized["statusMessage"] = handler["statusMessage"]
    limit = handler.get("additionalContextLimit")
    if limit is not None and limit != 2500 and event in (
            "PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"):
        normalized["additionalContextLimit"] = limit
    ident = {"event_name": CODEX_EVENT_KEYS[event], "hooks": [normalized]}
    matcher = None if event in ("UserPromptSubmit", "Stop", "Interrupt") else group.get("matcher")
    if matcher is not None:
        ident["matcher"] = matcher
    text = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_STATE_HEADER = re.compile(
    r"""^\[\s*hooks\s*\.\s*state\s*\.\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*\]\s*(?:#.*)?$""")
_STATE_VALUE = re.compile(
    r"""^(["']?)(enabled|trusted_hash)\1\s*=\s*(true|false|"(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#.*)?$""")


def _toml_string(token: str):
    if token.startswith("'"):
        return token[1:-1]
    try:
        return json.loads(token)            # TOML basic-string escapes are JSON's, bar \U
    except ValueError:
        return None


def codex_hook_states(config_text: str) -> dict:
    """{hook-state key: {"enabled": bool, "trusted_hash": str}} (each field
    only when set) from a Codex config.toml.

    tomllib where there is one (3.11+). The pinned interpreter may be 3.9
    (see config.py), so without it only the form Codex itself writes — one
    [hooks.state."<key>"] table per hook, its keys bare or quoted — is read;
    any other spelling counts as no state, which reads as not trusted rather
    than trusted."""
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            state = (tomllib.loads(config_text).get("hooks") or {}).get("state") or {}
        except (tomllib.TOMLDecodeError, AttributeError):
            return {}
        if not isinstance(state, dict):
            return {}
        return {k.strip(): v for k, v in state.items() if isinstance(v, dict)}
    out, current = {}, None
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            header = _STATE_HEADER.match(line)
            key = _toml_string(header.group(1)) if header else None
            current = out.setdefault(key.strip(), {}) if key else None
            continue
        value = _STATE_VALUE.match(line) if current is not None else None
        if value:
            _quote, field, token = value.groups()
            current[field] = token == "true" if token in ("true", "false") else _toml_string(token)
        elif current is not None and "enabled" in line:
            # A spelling of `enabled` this reader does not know may be a
            # switch-off; `"enabled" = false` read as on before (review,
            # 2026-09-28), and the home's threads were adopted.
            current["enabled"] = False
    return out


def codex_trust(home: str, approvals: bool = False) -> dict:
    """Whether Codex will run the xsm hook groups in this home.

    Codex records trust in config.toml as
    [hooks.state."<abs path to hooks.json>:<event>:<group>:<hook>"]
    trusted_hash = "sha256:…" (enabled = false switches a hook off), and runs
    a hook only when it is not switched off and trusted_hash equals the hash
    of the hook as it is now (Codex 0.158, discovery.rs: hook_enabled,
    hook_trust_status). Until 2026-09-28 the text "trusted_hash" near the key
    was taken as trust, so a hook edited since it was trusted, or switched
    off, read as trusted and adopt_open_codex registered sessions whose hooks
    never run. An untrusted hook simply never runs, which from the outside
    looks exactly like a session that never registered.
    Returns {event: True/False} for the events that carry our groups; False
    wherever the evidence is missing.
    This is the state recorded in the home's config.toml; a session started
    with a profile or `-c hooks.state...` may run with another, unseen here.
    """
    given = os.path.join(os.path.expanduser(home), "hooks.json")
    hooks_file = os.path.join(os.path.realpath(os.path.expanduser(home)), "hooks.json")
    # Codex keys the state by the hooks.json path as it found it; a home
    # reached through a symlink may carry either spelling.
    spellings = [hooks_file] + ([given] if given != hooks_file else [])
    data = paths.read_json(hooks_file, {}) or {}
    states = codex_hook_states(_read_text(os.path.join(os.path.dirname(hooks_file),
                                                       "config.toml")) or "")
    events = ["SessionStart", "UserPromptSubmit"] + (["PermissionRequest"] if approvals else [])
    out = {}
    for event in events:
        for index, group in enumerate((data.get("hooks") or {}).get(event, [])):
            if not _is_ours(group):
                continue
            ok = True
            for at, handler in enumerate(group.get("hooks", [])):
                if MARKER not in (handler.get("command") or ""):
                    continue
                keys = ["%s:%s:%d:%d" % (f, CODEX_EVENT_KEYS[event], index, at) for f in spellings]
                state = next((states[k] for k in keys if k in states), {})
                current = codex_hook_hash(event, group, handler)
                ok = (ok and state.get("enabled") is not False and current is not None
                      and state.get("trusted_hash") == current)
            out[event] = out.get(event, True) and ok
    plugin = codex_plugin(home)
    if plugin:
        # With the plugin, its hooks are the ones that count; hook groups an
        # earlier `xsm install` left are leftovers() and cleared on refresh.
        out = {}
        # The plugin's hooks are keyed by plugin and file, not by path, and
        # hashed the same way (checked against Codex 0.158, 2026-09-29).
        rel = "hooks/codex-hooks.json"
        data = paths.read_json(os.path.join(plugin["root"], rel), {}) or {}
        for event in events:
            for index, group in enumerate((data.get("hooks") or {}).get(event, [])):
                for at, handler in enumerate(group.get("hooks", [])):
                    key = "%s:%s:%s:%d:%d" % (plugin["key"], rel, CODEX_EVENT_KEYS[event], index, at)
                    state = states.get(key, {})
                    current = codex_hook_hash(event, group, handler)
                    out[event] = (out.get(event, True) and state.get("enabled") is not False
                                  and current is not None and state.get("trusted_hash") == current)
    return out


STATUSLINE_BASE = "statusline-base"      # the statusLine a user had before xsm's


def statusline_command(base: str | None = None) -> str:
    """`xsm statusline`, or with `base` (a settings file) the composed form:
    that file's original statusLine first, then xsm's one line under it."""
    extra = " --base %s" % shlex.quote(base) if base else ""
    return "%s statusline%s %s" % (launcher(), extra, MARKER)


def _state_file(kind: str, target: str) -> str:
    """xsm's own note about one settings file, kept under the state folder."""
    import hashlib
    key = hashlib.sha256(os.path.realpath(target).encode()).hexdigest()[:16]
    return paths.path(kind, key + ".json")


def _base_path(target: str) -> str:
    return _state_file(STATUSLINE_BASE, target)


def statusline_base(target: str) -> dict | None:
    """The statusLine this settings file had before xsm composed onto it."""
    return paths.read_json(_base_path(target))


def install_statusline(home: str) -> str:
    """Opt-in. Claude has one statusLine per home, and the user's own — a
    dashboard, Orca's, anything — is never replaced: it is kept, run first
    with the same input, and xsm's line goes under it (user decision,
    2026-09-22). Its refresh interval and padding carry over."""
    target = _settings_file(home, "claude")
    data = paths.read_json(target, {}) or {}
    current = data.get("statusLine")
    if current and MARKER in json.dumps(current):
        return "already"
    if current:
        os.makedirs(paths.path(STATUSLINE_BASE), mode=0o700, exist_ok=True)
        paths.write_json(_base_path(target), current)
        want = {"type": "command", "command": statusline_command(target)}
        for key in ("refreshInterval", "padding"):
            if key in current:
                want[key] = current[key]
        outcome = "composed"
    else:
        want = {"type": "command", "command": statusline_command()}
        outcome = "installed"
    if os.path.exists(target):
        _backup(target)
    data["statusLine"] = want
    paths.write_json(target, data, mode=0o644)
    return outcome


def remove_statusline(home: str) -> bool:
    """Take xsm's line out, and give back the statusLine it was composed onto."""
    target = _settings_file(home, "claude")
    data = paths.read_json(target)
    if not data or MARKER not in json.dumps(data.get("statusLine") or {}):
        return False
    _backup(target)
    original = statusline_base(target)
    if original:
        data["statusLine"] = original
        try:
            os.unlink(_base_path(target))
        except OSError:
            pass
    else:
        del data["statusLine"]
    paths.write_json(target, data, mode=0o644)
    return True


# The tools that put a choice in front of the person: each one shows an
# approval form (or uses the command the person typed) and changes nothing
# without a yes. Claude's auto mode refused the call itself as widening scope,
# so the agent never got to ask and told the person to type /xsm link instead
# (issue #8, measured with Claude Code 2.1.286, 2026-10-01). Allowing the call
# lets the form ask; the form is the consent (user decision, 2026-10-01).
FORM_TOOLS = ("xsm_link", "xsm_reach", "xsm_join", "xsm_approve", "xsm_grant", "xsm_decide",
              "xsm_doc_endorse")
# A direct install registers the server as "xsm"; the plugin's is "xsm" in plugin "xsm".
FORM_TOOL_PREFIXES = ("mcp__xsm__", "mcp__plugin_xsm_xsm__")


# The same refusal hit the other road to a yes: auto mode denied the xsm skill
# and `xsm unblock` as a bypass before xsm could ask the person (issue #9,
# measured 2026-10-01). These commands exist to put a person's decision to
# them: each refuses and tells the agent to ask until the person has replied.
# Allowing the call lets them ask. Commands that mostly ask no one (spawn,
# post, doc add) are left to the classifier (review, 2026-10-01). The plugin's
# skill is named `xsm:xsm` when Claude calls it (the skills documentation:
# plugin skills are namespaced), the directly installed one `xsm`.
ASKING_COMMANDS = ("link", "reach", "join", "leave", "unblock", "approve", "attempts clear",
                   "frameworks ignore", "remote add")
ALLOWED = "allowed"           # one note per settings file: what xsm added to its allow list
NOTE_VERSION = 2              # a note without it predates counting the rules already there
# Rules the eb600e8 build wrote and the list no longer holds. Only xsm ever
# wrote these exact strings, so a home whose note is missing or old loses them.
STALE_RULES = ("Bash(xsm spawn:*)", "Bash(xsm post:*)", "Bash(xsm doc add:*)")


def form_tool_names() -> list:
    return [prefix + tool for prefix in FORM_TOOL_PREFIXES for tool in FORM_TOOLS] + \
        ["Skill(xsm)", "Skill(xsm:xsm)"] + ["Bash(xsm %s:*)" % c for c in ASKING_COMMANDS]


def _allow_list(data) -> list:
    perms = (data or {}).get("permissions")
    allow = perms.get("allow") if isinstance(perms, dict) else None
    return allow if isinstance(allow, list) else []


def settings_invalid(home: str) -> str | None:
    """The settings file of this Claude home when it exists and is not a JSON
    object xsm can read, else None. It is the person's file: xsm leaves it as
    it is (2026-10-01: a refresh replaced one with a file of only `permissions`)
    and says so."""
    target = _settings_file(home, "claude")
    return target if os.path.exists(target) and \
        not isinstance(paths.read_json(target), dict) else None


def missing_form_tools(home: str) -> list:
    """What `allow_form_tools` would add: the rules this Claude home lacks.
    None are named for a file it cannot read: it adds nothing there."""
    if settings_invalid(home):
        return []
    allow = _allow_list(paths.read_json(_settings_file(home, "claude")))
    return [n for n in form_tool_names() if n not in allow]


def _xsm_rules(note, allow: list) -> list:
    """What xsm put in this allow list. A current note says so itself. With no
    note, or one from before NOTE_VERSION (it holds only what its refresh newly
    added), the known names already in the list are xsm's: an earlier version
    put them there, and they look exactly like a person's (2026-10-01: a
    refresh over such a home left about 24 rules behind after uninstall)."""
    held = list(note["added"]) if isinstance(note, dict) and isinstance(note.get("added"), list) \
        else []
    if isinstance(note, dict) and note.get("v") == NOTE_VERSION:
        return held
    return held + [n for n in form_tool_names() if n in allow and n not in held]


def allow_form_tools(home: str) -> str:
    """Add the form tools to this Claude home's permissions.allow: added |
    updated (only rules an earlier version added and the list dropped were
    removed) | already | invalid (the file is not JSON xsm can read: untouched).

    What xsm put there is noted (ALLOWED), because a rule the person already
    had looks exactly like one xsm added and `remove_form_tools` must not take
    theirs. The note is made on the first call even when nothing needed adding,
    and a home with no note or an old one is read as described at `_xsm_rules`.
    `created` is what the note says xsm made from nothing (permissions, allow),
    so uninstall can leave the file as it found it."""
    target = _settings_file(home, "claude")
    if settings_invalid(home):
        return "invalid"
    data = paths.read_json(target, {}) or {}
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    allow = perms.get("allow") if isinstance(perms.get("allow"), list) else []
    note_path = _state_file(ALLOWED, target)
    note = paths.read_json(note_path)
    current = isinstance(note, dict) and note.get("v") == NOTE_VERSION
    added = _xsm_rules(note, allow)
    stale = [] if current else [n for n in STALE_RULES if n in allow]
    missing = [n for n in form_tool_names() if n not in allow]
    created = note.get("created") if isinstance(note, dict) and \
        isinstance(note.get("created"), list) else None
    made = [k for k, there in (("permissions", isinstance(data.get("permissions"), dict)),
                               ("allow", isinstance(perms.get("allow"), list))) if not there]
    if created is None and not added:       # nothing of xsm's here yet: what this call makes
        created = made
    elif created is not None:
        created = created + [k for k in made if k not in created]
    if missing or stale:
        if os.path.exists(target):
            _backup(target)
        perms["allow"] = [n for n in allow if n not in stale] + missing
        data["permissions"] = perms
        paths.write_json(target, data, mode=0o644)
        added += [n for n in missing if n not in added]
    if missing or stale or not current:
        record = {"file": target, "added": added, "v": NOTE_VERSION}
        if created is not None:
            record["created"] = created
        paths.write_json(note_path, record)
    return "added" if missing else "updated" if stale else "already"


def remove_form_tools(home: str) -> bool:
    """Take out the entries allow_form_tools put in, and only those (and the
    allow list or permissions it made, if that leaves them empty). A home with
    no note, or an old one, loses the known names as well, and the stale ones."""
    target = _settings_file(home, "claude")
    data = paths.read_json(target)
    if not isinstance(data, dict):
        return False
    allow = _allow_list(data)
    note_path = _state_file(ALLOWED, target)
    note = paths.read_json(note_path)
    names = set(_xsm_rules(note, allow))
    if not (isinstance(note, dict) and note.get("v") == NOTE_VERSION):
        names |= set(STALE_RULES)
    kept = [n for n in allow if n not in names]
    if len(kept) != len(allow):
        _backup(target)
        perms = data["permissions"]
        perms["allow"] = kept
        # `created` unknown (a home from before the note says): an allow list
        # that holds nothing after xsm's rules went was xsm's to begin with.
        created = note.get("created") if isinstance(note, dict) and \
            isinstance(note.get("created"), list) else None
        if not kept and (created is None or "allow" in created):
            del perms["allow"]
        if not perms and (created is None or "permissions" in created):
            del data["permissions"]
        paths.write_json(target, data, mode=0o644)
    if isinstance(note, dict):
        # Emptied, not deleted: with no note a second uninstall would fall back
        # to the known names and take the rules the user had.
        paths.write_json(note_path, {"file": target, "added": [], "v": NOTE_VERSION})
    return len(kept) != len(allow)


def retired_commands(home: str) -> list:
    """The per-command files earlier versions wrote: `commands/xsm-*.md` in a
    Claude home, `skills/xsm-*/` in a Codex one. The commands became arguments
    of the one skill (`/xsm list`, `$xsm list`; 2026-09-27), and a copy left
    behind offers a command whose text nobody maintains — in Codex, eight
    skills beside `xsm` that were never skills.

    Only what carries our marker is ours. A link is someone's own
    arrangement, whatever it points at, and is left alone."""
    out = []
    for p in glob.glob(os.path.join(home, "commands", "xsm-*.md")):
        if not os.path.islink(p) and FILE_MARKER in (_read_text(p) or ""):
            out.append(p)
    for d in glob.glob(os.path.join(home, "skills", "xsm-*")):
        if not os.path.islink(d) and os.path.isdir(d) and \
                FILE_MARKER in (_read_text(os.path.join(d, "SKILL.md")) or ""):
            out.append(d)
    return sorted(out)


def remove_retired(home: str) -> list:
    """Remove what `retired_commands` found; returns what is actually gone."""
    gone = []
    for p in retired_commands(home):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
        except OSError:
            continue
        gone.append(p)
    return gone


def remove_skill(home: str) -> bool:
    target = os.path.join(home, "skills", "xsm")
    source = os.path.join(REPO, "skills", "xsm")
    if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
        os.unlink(target)
        return True
    # A copy of our skill (made by hand where a link would not do; install_skill
    # --refresh keeps it current). Its first line is ours, so it is not someone
    # else's file.
    state, detail = skill_state(home)
    if state in ("copy-current", "copy-stale") and \
            (_read_text(os.path.join(detail, "SKILL.md")) or "").startswith("---\nname: xsm\n"):
        shutil.rmtree(target, ignore_errors=True)
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
    if os.path.exists(target) and not isinstance(data, dict):
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
        have = len(ours) == 1 and _same_command(ours[0]["hooks"][0].get("command"), want) and \
            ours[0]["hooks"][0].get("timeout", 10) == TIMEOUTS.get(event, 10) and \
            ours[0].get("matcher") == MATCHERS.get(event)
        actions.append({"event": event, "others": len(groups) - len(ours),
                        "action": "keep" if have else ("replace" if ours else "add"),
                        "command": want})
    # Our groups on events we no longer install (an earlier version's) go.
    for event, groups in hooks.items():
        if event not in events and any(_is_ours(g) for g in groups):
            actions.append({"event": event, "others": len([g for g in groups if not _is_ours(g)]),
                            "action": "remove", "command": None})
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
        if action["action"] == "remove":
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event, None)
            continue
        group = {"hooks": [{"type": "command", "command": action["command"],
                            "timeout": TIMEOUTS.get(event, 10)}]}
        if event in MATCHERS:
            group = dict(matcher=MATCHERS[event], **group)
        groups.append(group)
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
    if not isinstance(data, dict):
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
        if action["action"] == "remove":
            lines.append("    - the xsm group (no longer installed for this event)")
        elif action["action"] != "keep":
            lines.append("    + %s" % action["command"])
    return "\n".join(lines)


def orphaned_servers() -> list:
    """[(pid, folder)] for xsm MCP servers running from a folder that no longer
    exists: a plugin update removed the version a still-open session started
    on (issue #6). Their sessions need restarting to get working xsm tools."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        script = next((a for a in parts[1].split() if a.endswith("/hooks/xsm-mcp.py")), None)
        if script and not os.path.exists(script):
            found.append((int(parts[0]), os.path.dirname(os.path.dirname(script))))
    return found


def codex_versions() -> list:
    """[(path, version or the first line of its failure)] for every codex on
    this machine, best first. One that cannot run is worth naming: an npm
    @openai/codex without its platform binary sat first on PATH and would have
    failed every delivery to a Codex session (2026-09-23)."""
    from . import adapters as _adapters
    out = []
    for path in _adapters.codex_bins():
        try:
            ran = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as err:
            out.append((path, "does not run: %s" % err))
            continue
        text = (ran.stdout or ran.stderr or "").strip().splitlines()
        out.append((path, text[0][:120] if text and ran.returncode == 0 else
                    "does not run: %s" % (next((l for l in text if l.strip()), "")[:100])))
    return out


def stuck(now: float | None = None) -> dict:
    """What is waiting on someone right now, from the records themselves.

    Everything here cost a person an investigation once: a worker waiting ten
    minutes on a permission nobody saw, messages queued to a Codex thread its
    TUI had left, sends that failed inside a sandbox. `xsm doctor` said
    nothing about any of them (2026-09-23)."""
    from . import ledger, registry, workers
    now = time.time() if now is None else now
    waiting = []
    for p in glob.glob(paths.path("approvals", "*.json")):
        req = paths.read_json(p, {}) or {}
        if req.get("status") in (None, "pending"):
            # `parent`: a session started the worker, so that session asks its
            # user; a worker a person started from a terminal has none.
            waiting.append({"id": req.get("id"), "worker": req.get("worker"),
                            "tool": req.get("tool"), "waiting_s": int(now - (req.get("t") or now)),
                            "parent": bool((workers.load(req.get("worker") or "") or {})
                                           .get("parent_ref"))})
    rows = ledger.recent(200)
    undelivered = [r for r in rows if r.get("status") == "queued" and now - (r.get("t") or now) > 120]
    uncertain = [r for r in rows if r.get("status") == "unknown"]
    failures = {}
    for r in rows:
        if r.get("status") == "error":
            failures[(r.get("error") or "unknown").split(":")[0]] = \
                failures.get((r.get("error") or "unknown").split(":")[0], 0) + 1
    replaced = [r for r in registry.records() if r.get("end_reason") == "thread_replaced"]
    return {"approvals": waiting, "undelivered": undelivered, "uncertain": uncertain,
            "send_failures": failures,
            "threads_replaced": [{"name": r.get("name"), "ref": r.get("ref")} for r in replaced]}


def doctor() -> dict:
    """Facts a person can act on, not a verdict."""
    from . import adapters, registry            # imported here to keep hook startup lean

    version = sys.version_info
    decisions = paths.read_jsonl("decisions.jsonl", limit=200)
    errors = [d for d in decisions if "internal error" in (d.get("reason") or "")]
    # A deleted home stays in the list until forgotten; reporting what it
    # would need installed invites bringing it back.
    homes = [h for h in config.homes() if os.path.isdir(h["path"])]
    gone = [h["path"] for h in config.homes() if not os.path.isdir(h["path"])]
    report = {
        "xsm_home": paths.HOME,
        "interpreter": pinned_python(),
        "interpreter_pinned": paths.read_json(paths.path(INTERPRETER)) is not None,
        "interpreter_ok": version >= (3, 9),
        "codex_binary": adapters.codex_bin(),
        "codex_binaries": codex_versions(),
        "homes": homes,
        "gone": gone,
        "installs": [plan(h["path"], h["runtime"]) for h in homes],
        "codex_trust": {h["path"]: codex_trust(h["path"], approvals=True) for h in homes
                        if h.get("runtime") == "codex"},
        "sessions": {"registered": len(registry.records()),
                     "live": len([r for r in registry.records() if r["state"] == "live"]),
                     "unregistered": len(registry.unregistered())},
        "decisions_seen": len(decisions),
        "hook_errors_recent": len(errors),
        "held": len(os.listdir(paths.path(paths.HELD))) if os.path.isdir(paths.path(paths.HELD)) else 0,
        "version": plugin_version(),
        "tmux": shutil.which("tmux"),
        "plugins": {h["path"]: plugin_installed(h["path"]) for h in homes},
        "plugin_missing_hooks": {h["path"]: plugin_missing_hooks(h["path"]) for h in homes
                                 if h.get("runtime") == "claude"},
        "allow_missing": {h["path"]: missing_form_tools(h["path"]) for h in homes
                          if h.get("runtime") == "claude"},
        "settings_invalid": {h["path"]: settings_invalid(h["path"]) for h in homes
                             if h.get("runtime") == "claude" and settings_invalid(h["path"])},
        "stale": {h["path"]: stale_copies(h["path"], h.get("runtime") or "claude")
                  for h in homes},
        "leftovers": {h["path"]: leftovers(h["path"]) for h in homes},
        "retired": {h["path"]: retired_commands(h["path"]) for h in homes},
        "xsm_on_path": shutil.which("xsm"),
        "orphaned_servers": orphaned_servers(),
        "strict_peers": bool(config.load().get("strict_peers")),
        "stuck": stuck(),
        "limits": [
            "A peer message without the xsm envelope cannot be told apart from your own typing "
            "inside a hook, so it passes the gate (S8-g2). Set crossSessionInbound to \"hold\" "
            "if you need every peer message to stop for review — that holds xsm messages too.",
            "This gate records consent and scope. It does not stop an agent that can edit "
            "~/.xsm or your settings directly (S8-c).",
        ],
    }
    return report


# --- the MCP server (ADR-0005) ---------------------------------------------------------
#
# Registered through each runtime's own `mcp add`, so xsm never edits
# ~/.claude.json or config.toml by hand. User scope, so every session in the
# home has it.

MCP_NAME = "xsm"
PLUGIN_NAME = "xsm"


def plugin_manifest() -> dict:
    return paths.read_json(os.path.join(REPO, ".claude-plugin", "plugin.json"), {}) or {}


def plugin_version() -> str:
    return plugin_manifest().get("version") or ""


def mcp_command() -> list:
    return [pinned_python(), os.path.join(REPO, "hooks", "xsm-mcp.py")]


def _runtime_env(home: str, runtime: str) -> dict:
    """The environment to run the runtime's own CLI in for this home. The
    default Claude home is not named: with CLAUDE_CONFIG_DIR=~/.claude Claude
    reads and writes ~/.claude/.claude.json, which no ordinary session ever
    reads — `xsm install --statusline`-era MCP registrations landed there and
    `claude mcp get xsm` found nothing (measured 2026-09-22)."""
    env = dict(os.environ)
    if runtime == "claude":
        if os.path.realpath(home) == os.path.realpath(os.path.expanduser("~/.claude")):
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = home
    else:
        env["CODEX_HOME"] = home
    return env


def _mcp_cli(runtime: str) -> str:
    return "claude" if runtime == "claude" else (shutil.which("codex") or "codex")


def mcp_state(home: str, runtime: str) -> str:
    """absent | current | stale"""
    home = os.path.realpath(os.path.expanduser(home))
    try:
        out = subprocess.run([_mcp_cli(runtime), "mcp", "get", MCP_NAME],
                             capture_output=True, text=True, timeout=30,
                             env=_runtime_env(home, runtime))
    except (OSError, subprocess.SubprocessError):
        return "absent"
    text = out.stdout + out.stderr
    if out.returncode != 0 or "No MCP server" in text or "not found" in text.lower():
        return "absent"
    return "current" if all(part in text for part in mcp_command()) else "stale"


def install_mcp(home: str, runtime: str) -> str:
    """added | current | replaced | failed:<why>"""
    home = os.path.realpath(os.path.expanduser(home))
    state = mcp_state(home, runtime)
    if state == "current":
        return "current"
    env = _runtime_env(home, runtime)
    if state == "stale":
        subprocess.run([_mcp_cli(runtime), "mcp", "remove", MCP_NAME] +
                       (["--scope", "user"] if runtime == "claude" else []),
                       capture_output=True, text=True, timeout=30, env=env)
    extra = []
    if os.environ.get("XSM_HOME") and not _is_default_home(os.environ["XSM_HOME"]):
        extra = (["-e"] if runtime == "claude" else ["--env"]) + \
            ["XSM_HOME=%s" % os.environ["XSM_HOME"]]
    argv = [_mcp_cli(runtime), "mcp", "add"] + (["--scope", "user"] if runtime == "claude" else []) \
        + extra + [MCP_NAME, "--"] + mcp_command()
    out = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    if out.returncode != 0:
        return "failed: %s" % (out.stderr or out.stdout).strip()[:200]
    return "replaced" if state == "stale" else "added"


def remove_mcp(home: str, runtime: str) -> bool:
    home = os.path.realpath(os.path.expanduser(home))
    if mcp_state(home, runtime) == "absent":
        return False
    out = subprocess.run([_mcp_cli(runtime), "mcp", "remove", MCP_NAME] +
                         (["--scope", "user"] if runtime == "claude" else []),
                         capture_output=True, text=True, timeout=30,
                         env=_runtime_env(home, runtime))
    return out.returncode == 0
