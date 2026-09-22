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
import shlex
import shutil
import subprocess
import sys
import time

from . import config, paths

MARKER = "#xsm-hook"
FILE_MARKER = "<!-- xsm-managed -->"        # commands we wrote, and may remove
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SessionEnd lets a clean exit read as `ended` rather than `stale`. Codex has
# no SessionEnd event, so a stopped Codex session always reads as stale.
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")
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
    """The absolute `xsm` a slash command should run. The launcher in the repo
    works wherever it is called from, so commands never depend on PATH."""
    return os.path.join(REPO, "bin", "xsm")


def command_files() -> list:
    pattern = os.path.join(REPO, "commands", "xsm-*.md")
    return sorted(glob.glob(pattern))


PLUGIN_COMMANDS = os.path.join(REPO, ".claude-plugin", "commands")


def plugin_command(source: str) -> str:
    """The same command, for the plugin. A plugin's files are copied as they
    are, so there is no install step to put an absolute launcher in: the
    plugin's own bin/ is on PATH while it is enabled, so `xsm` is enough.
    The name loses its prefix because Claude namespaces a plugin's commands
    itself: /xsm:list, not /xsm:xsm-list."""
    body = open(source, encoding="utf-8").read().replace("{{XSM}}", "xsm")
    return body.replace("/xsm-", "/xsm:")        # usage lines inside the body


def plugin_command_files() -> dict:
    """{path under .claude-plugin/commands: contents}"""
    return {os.path.join(PLUGIN_COMMANDS, os.path.basename(s)[len("xsm-"):]): plugin_command(s)
            for s in command_files()}


def write_plugin_commands() -> list:
    written = []
    os.makedirs(PLUGIN_COMMANDS, exist_ok=True)
    for target, body in plugin_command_files().items():
        if _read_text(target) != body:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(body)
        written.append(target)
    return written


def plugin_installed(home: str) -> str | None:
    """The xsm plugin's version in this Claude home, or None.

    Both ways of installing exist (user decision, 2026-09-23): a plugin, and
    `xsm install` writing into settings.json. Both in one home would run every
    hook twice — the second run of the receive gate would see its own receipt
    and refuse the message as a duplicate — so each install path checks for
    the other."""
    state = paths.read_json(os.path.join(os.path.expanduser(home), "plugins",
                                         "installed_plugins.json"), {}) or {}
    for key, entries in (state.get("plugins") or {}).items():
        if key == PLUGIN_NAME or key.startswith(PLUGIN_NAME + "@"):
            for entry in entries if isinstance(entries, list) else [entries]:
                return (entry or {}).get("version") or "installed"
    return None


def install_commands(home: str) -> list:
    """Write the slash commands into a Claude home with the launcher path filled
    in. Written rather than symlinked because the path has to be substituted."""
    target_dir = os.path.join(home, "commands")
    os.makedirs(target_dir, exist_ok=True)
    remove_orphans(home, "claude")
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


def _frontmatter(text: str) -> tuple:
    """(fields, body) of a command file's --- block; values stay strings."""
    fields, body = {}, text
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        for line in head.splitlines():
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields, body


def codex_command_skill(source: str) -> tuple:
    """(SKILL.md, agents/openai.yaml) for one slash command, in Codex's terms.

    Codex has no slash commands of its own; a user types `$name` and gets a
    skill (measured 2026-09-22: `$xsm` worked, `$xsm-list` did not exist). Two
    things do not carry over. Claude runs `!`cmd`` before the model sees the
    prompt; Codex does not, so the command is spelled out for the model to run.
    And `disable-model-invocation` is `policy.allow_implicit_invocation: false`
    in Codex, so the model never reaches for these on its own."""
    name = os.path.basename(source)[:-3]
    fields, body = _frontmatter(open(source, encoding="utf-8").read())
    body = body.replace(FILE_MARKER, "").strip()
    xsm = launcher()
    shell = [line[2:-1] for line in body.splitlines()
             if line.startswith("!`") and line.endswith("`")]
    if shell:
        # A Markdown table is passed through bare so the TUI draws it; anything
        # else goes in a code block so its spacing survives.
        tables = any("--table" in c for c in shell)
        wrap = ("as it is, not in a code block, so the tables show as tables" if tables
                else "inside one code block")
        commands = [c.replace("{{XSM}}", xsm) for c in shell]
        block = body[body.index("<<<") + 3:body.index(">>>")].strip("\n") \
            if "<<<" in body and ">>>" in body else ""
        layout = [ln for ln in block.splitlines() if ln.strip()]
        if len(commands) == 1 and len(layout) == 1:
            text = ("This is a display command. There is nothing to decide.\n\n"
                    "Run this shell command, exactly as written:\n\n    %s\n\n"
                    "Then reply with its output, copied exactly, %s. Nothing before it, "
                    "nothing after it. Do not translate, reword, summarise or explain it, and "
                    "run nothing else.\n" % (commands[0], wrap))
        else:
            # Headings between the outputs stay; each command's place is marked.
            n = iter(range(1, len(commands) + 1))
            template = "\n".join("[output of command %d]" % next(n)
                                  if ln.startswith("!`") and ln.endswith("`") else ln
                                  for ln in block.splitlines())
            # No <<< >>> around the layout: Codex copied the markers into
            # its reply (2026-09-23).
            text = ("This is a display command. There is nothing to decide.\n\n"
                    "Run these shell commands, exactly as written:\n\n%s\n\n"
                    "Then reply with the layout below, each [output of command N] replaced by "
                    "that command's output copied exactly, %s. Nothing before it, nothing after "
                    "it. Do not translate, reword, summarise or explain it, and run nothing "
                    "else.\n\n## Layout\n\n%s\n" % (
                        "\n".join("%d. `%s`" % (i + 1, c) for i, c in enumerate(commands)),
                        wrap, template))
    else:
        text = body.replace("{{XSM}}", xsm).replace("Bash command", "shell command")
        text = text.replace("/" + name, "$" + name)
        text = text.replace("Arguments: $ARGUMENTS",
                            "Arguments: the words after `$%s` in the user's message." % name)
        if "send <TARGET>" in text:
            text += ("\nIf the shell command fails with `sandbox-blocked`, call the MCP tool "
                     "`xsm_send` with the same target and text instead, and reply with its "
                     "result the same way.\n")
    description = fields.get("description", name)
    if fields.get("argument-hint"):
        description += " (usage: $%s %s)" % (name, fields["argument-hint"])
    skill = "---\nname: %s\ndescription: %s\n---\n\n%s\n%s\n" % (
        name, description, text.rstrip(), FILE_MARKER)
    yaml = ("interface:\n  display_name: \"%s\"\n  short_description: \"%s\"\n"
            "policy:\n  allow_implicit_invocation: false\n" % (
                name, fields.get("description", name).replace('"', "'")))
    return skill, yaml


def install_codex_commands(home: str) -> list:
    """The slash commands as skills in a Codex home, one directory each.
    A directory that is not ours (no marker in its SKILL.md) is left alone."""
    written = []
    remove_orphans(home, "codex")
    for source in command_files():
        name = os.path.basename(source)[:-3]
        target = os.path.join(home, "skills", name)
        existing = _read_text(os.path.join(target, "SKILL.md"))
        if os.path.islink(target) or (existing is not None and FILE_MARKER not in existing) \
                or (existing is None and os.path.exists(target)):
            continue
        skill, yaml = codex_command_skill(source)
        os.makedirs(os.path.join(target, "agents"), exist_ok=True)
        for rel, text in (("SKILL.md", skill), (os.path.join("agents", "openai.yaml"), yaml)):
            if _read_text(os.path.join(target, rel)) != text:
                with open(os.path.join(target, rel), "w", encoding="utf-8") as fh:
                    fh.write(text)
        written.append(target)
    return written


def remove_codex_commands(home: str) -> int:
    removed = 0
    for source in command_files():
        target = os.path.join(home, "skills", os.path.basename(source)[:-3])
        body = _read_text(os.path.join(target, "SKILL.md"))
        if body is not None and FILE_MARKER in body and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    return removed


def leftovers(home: str) -> list:
    """Files an earlier `xsm install` left in a Claude home that has since
    moved to the plugin. The plugin carries its own commands and skill, so
    these are dead weight — and a stale copy of them reads as ours."""
    if not plugin_installed(home):
        return []
    found = [p for p in glob.glob(os.path.join(home, "commands", "xsm-*.md"))
             if FILE_MARKER in (_read_text(p) or "")]
    skill = os.path.join(home, "skills", "xsm")
    if os.path.exists(skill):
        found.append(skill)
    return found


def stale_copies(home: str, runtime: str = "claude") -> list:
    """Files we installed into a home that no longer match the repository.
    A copied skill in a second profile sat eight versions behind for a day
    before anyone noticed (2026-09-23), because nothing compared them."""
    out = []
    if runtime == "claude":
        if plugin_installed(home):
            return []                   # the plugin keeps itself current
        for source in command_files():
            target = os.path.join(home, "commands", os.path.basename(source))
            body = _read_text(target)
            if body is not None and FILE_MARKER in body and \
                    body != open(source, encoding="utf-8").read().replace("{{XSM}}", launcher()):
                out.append(target)
        state, detail = skill_state(home)
        if state == "copy-stale":
            out.append(detail)
    else:
        for source in command_files():
            name = os.path.basename(source)[:-3]
            target = os.path.join(home, "skills", name, "SKILL.md")
            body = _read_text(target)
            if body is not None and FILE_MARKER in body and body != codex_command_skill(source)[0]:
                out.append(target)
    return out


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


def install_skill(home: str, refresh: bool = False) -> tuple:
    """Link the skill so the session knows the commands exist. A link keeps it in
    step with the repo; anything already there that is not ours is left be.

    With `refresh`, a copy that has fallen behind is rewritten: it is our file,
    and telling a person to run `cp` is how two profiles ended up eight
    versions behind (2026-09-23)."""
    state, detail = skill_state(home)
    if state == "copy-stale" and refresh:
        with open(detail, "w", encoding="utf-8") as fh:
            fh.write(open(os.path.join(REPO, "skills", "xsm", "SKILL.md"), encoding="utf-8").read())
        return "copy-current", detail
    if state != "absent":
        return state, detail
    target = os.path.join(home, "skills", "xsm")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.symlink(os.path.join(REPO, "skills", "xsm"), target)
    return "linked", target


def codex_trust(home: str, approvals: bool = False) -> dict:
    """Whether Codex has trusted the xsm hook groups in this home.

    Codex records trust in config.toml as
    [hooks.state."<abs path to hooks.json>:<event>:<group>:<hook>"] trusted_hash = …
    (found by reading real configs). An untrusted hook simply never runs, which
    from the outside looks exactly like a session that never registered.
    Returns {event: True/False} for the events that carry our groups.
    """
    home = os.path.realpath(os.path.expanduser(home))
    hooks_file = os.path.join(home, "hooks.json")
    data = paths.read_json(hooks_file, {}) or {}
    config_text = _read_text(os.path.join(home, "config.toml")) or ""
    snake = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit"}
    if approvals:
        snake["PermissionRequest"] = "permission_request"
    out = {}
    for event, key in snake.items():
        for index, group in enumerate((data.get("hooks") or {}).get(event, [])):
            if _is_ours(group):
                marker = '[hooks.state."%s:%s:%d:0"]' % (hooks_file, key, index)
                at = config_text.find(marker)
                out[event] = at >= 0 and "trusted_hash" in config_text[at:at + 400].split("[", 2)[1]
    return out


STATUSLINE_BASE = "statusline-base"      # the statusLine a user had before xsm's


def statusline_command(base: str | None = None) -> str:
    """`xsm statusline`, or with `base` (a settings file) the composed form:
    that file's original statusLine first, then xsm's one line under it."""
    extra = " --base %s" % shlex.quote(base) if base else ""
    return "%s statusline%s %s" % (launcher(), extra, MARKER)


def _base_path(target: str) -> str:
    import hashlib
    key = hashlib.sha256(os.path.realpath(target).encode()).hexdigest()[:16]
    return paths.path(STATUSLINE_BASE, key + ".json")


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


def orphaned_commands(home: str, runtime: str = "claude") -> list:
    """Files we installed for a command that no longer exists — a renamed one.
    `/xsm-inbox` became `/xsm-log` (2026-09-23) and the old file would have
    stayed, offering a command whose text we no longer maintain."""
    ours = {os.path.basename(s) for s in command_files()}
    out = []
    if runtime == "claude":
        for p in glob.glob(os.path.join(home, "commands", "xsm-*.md")):
            body = _read_text(p)
            if os.path.basename(p) not in ours and body and FILE_MARKER in body:
                out.append(p)
    else:
        for p in glob.glob(os.path.join(home, "skills", "xsm-*", "SKILL.md")):
            if os.path.basename(os.path.dirname(p)) + ".md" not in ours and \
                    FILE_MARKER in (_read_text(p) or ""):
                out.append(os.path.dirname(p))
    return out


def remove_orphans(home: str, runtime: str = "claude") -> list:
    gone = orphaned_commands(home, runtime)
    for p in gone:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.unlink(p)
            except OSError:
                pass
    return gone


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
    # A copy we wrote (install_skill --refresh writes one where a home cannot
    # take our link). Its first line is ours, so it is not someone else's file.
    state, detail = skill_state(home)
    if state in ("copy-current", "copy-stale") and \
            (_read_text(detail) or "").startswith("---\nname: xsm\n"):
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
        have = len(ours) == 1 and _same_command(ours[0]["hooks"][0].get("command"), want) and \
            ours[0]["hooks"][0].get("timeout", 10) == TIMEOUTS.get(event, 10)
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
        groups.append({"hooks": [{"type": "command", "command": action["command"],
                                  "timeout": TIMEOUTS.get(event, 10)}]})
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
        if action["action"] == "remove":
            lines.append("    - the xsm group (no longer installed for this event)")
        elif action["action"] != "keep":
            lines.append("    + %s" % action["command"])
    return "\n".join(lines)


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
    from . import ledger, registry
    now = time.time() if now is None else now
    waiting = []
    for p in glob.glob(paths.path("approvals", "*.json")):
        req = paths.read_json(p, {}) or {}
        if req.get("status") in (None, "pending"):
            waiting.append({"id": req.get("id"), "worker": req.get("worker"),
                            "tool": req.get("tool"), "waiting_s": int(now - (req.get("t") or now))})
    rows = ledger.recent(200)
    undelivered = [r for r in rows if r.get("status") == "queued" and now - (r.get("t") or now) > 120]
    failures = {}
    for r in rows:
        if r.get("status") == "error":
            failures[(r.get("error") or "unknown").split(":")[0]] = \
                failures.get((r.get("error") or "unknown").split(":")[0], 0) + 1
    replaced = [r for r in registry.records() if r.get("end_reason") == "thread_replaced"]
    return {"approvals": waiting, "undelivered": undelivered, "send_failures": failures,
            "threads_replaced": [{"name": r.get("name"), "ref": r.get("ref")} for r in replaced]}


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
        "codex_binaries": codex_versions(),
        "homes": config.homes(),
        "installs": [plan(h["path"], h["runtime"]) for h in config.homes()],
        "codex_trust": {h["path"]: codex_trust(h["path"], approvals=True) for h in config.homes()
                        if h.get("runtime") == "codex"},
        "sessions": {"registered": len(registry.records()),
                     "live": len([r for r in registry.records() if r["state"] == "live"]),
                     "unregistered": len(registry.unregistered())},
        "decisions_seen": len(decisions),
        "hook_errors_recent": len(errors),
        "held": len(os.listdir(paths.path(paths.HELD))) if os.path.isdir(paths.path(paths.HELD)) else 0,
        "version": plugin_version(),
        "tmux": shutil.which("tmux"),
        "plugins": {h["path"]: plugin_installed(h["path"]) for h in config.homes()
                    if h.get("runtime") == "claude"},
        "stale": {h["path"]: stale_copies(h["path"], h.get("runtime") or "claude")
                  for h in config.homes()},
        "leftovers": {h["path"]: leftovers(h["path"]) for h in config.homes()
                      if h.get("runtime") == "claude"},
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
