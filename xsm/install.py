"""Installing the hooks into a home without disturbing what is already there.

These files are shared: a Claude settings.json or a Codex hooks.json often
already holds hooks from other tools and from the person themselves. So the
installer never rewrites a hook it did not write. It appends
one group per event, marks the command with a trailing `#xsm-hook`, and
removes exactly the marked groups on uninstall. Every write is preceded by a
timestamped backup and followed by a re-parse.

The interpreter is pinned to an absolute path at install time. A hook that
starts under the wrong python silently stops gating (S8-g2). The hook command
of a Claude home is the sh launcher hooks/xsm-hook, which reads that pin and
falls back to the pythons PATH has, and runs from a copy of the checkout under
~/.xsm/runtime (the snapshot below), not from the checkout (2026-10-01).
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

from . import config, paths, policy

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


# How xsm is started from the folder it lives in, whatever the current directory holds
# (2026-10-02). `python -m xsm` puts the current directory first on sys.path, so a run
# inside another xsm checkout (a worktree) ran that checkout's code instead of the
# launcher's. This takes the folder as its first argument, puts it first and drops the
# current directory; `-P` does the second half and is Python 3.11. bin/xsm carries the
# same text (a test keeps the two equal); no quote, `$` or backslash, so sh keeps it whole.
BOOT = ('import os, sys; here = sys.argv.pop(1); cwd = os.path.realpath("."); '
        'sys.path[:] = [here] + [p for p in sys.path if p and os.path.realpath(p) != cwd]; '
        'from xsm.cli import main; sys.exit(main())')


def cli_argv(*args: str) -> list:
    """The command that runs `xsm <args>` from this folder with the pinned python: for
    what xsm starts itself (a detached reaper, a worker's finish)."""
    return [pinned_python(), "-c", BOOT, REPO] + list(args)


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


def _quote(path: str) -> str:
    """The path as a shell word: in double quotes, as hooks/hooks.json writes its
    own, unless it holds a character those do not keep."""
    return '"%s"' % path if not re.search(r'["$`\\]', path) else shlex.quote(path)


def hook_command(runtime: str, event: str, root: str | None = None,
                 existing: str | None = None) -> str:
    """The command a hook group runs. `root` is the folder it runs from (default
    runtime_root()); `existing` is the command already in a Codex hooks.json.

    Claude: the sh launcher, `"<root>/hooks/xsm-hook" #xsm-hook`. It tries the
    pinned python, then the others PATH has, and turns the status 2 a python
    gives for a script it cannot open into 1. Claude Code reads status 2 from a
    hook as "block": macOS privacy protection denied a session's app
    ~/Documents and every prompt was blocked (2026-10-01), and the form before
    this one, `python <path> || exit 1`, still depended on that one python and
    that one path existing (user decision, 2026-10-01: a conversation the person
    wants is never blocked by a component failure).

    Codex: `python <script> #xsm-hook`, as it was. The command is part of what
    Codex's trust hash covers, so a home that has one keeps its script path and
    only a new install chooses (codex_script); the plugin's codex-hooks.json is
    not touched at all."""
    # Only a non-default state directory is written into the command. Writing
    # the default too made the command depend on whether the installing shell
    # happened to export XSM_HOME, so a reinstall rewrote every hook.
    prefix = "XSM_HOME=%s " % os.environ["XSM_HOME"] \
        if os.environ.get("XSM_HOME") and not _is_default_home(os.environ["XSM_HOME"]) else ""
    if runtime == "claude":
        return "%s%s %s" % (prefix, _quote(os.path.join(root or runtime_root(), "hooks",
                                                        "xsm-hook")), MARKER)
    return "%s%s %s %s" % (prefix, pinned_python(), codex_script(existing, root), MARKER)


def codex_script(existing: str | None = None, root: str | None = None) -> str:
    """The hook script of a Codex direct install. A command already there keeps
    its script (changing it would make Codex ask to trust the hooks again); a
    new install runs from ~/.xsm/runtime/current when it runs from a snapshot,
    a path that stays the same when a refresh makes a newer one, so a refresh
    never costs a re-trust."""
    found = re.search(r"(\S+/hooks/xsm-hook\.py)", existing or "")
    if found and os.path.exists(found.group(1)):
        return found.group(1)
    base = root or runtime_root()
    if _under_runtime(base):
        base = runtime_dir(LINK)
    return os.path.join(base, "hooks", "xsm-hook.py")


def hook_form(command: str | None) -> str | None:
    """Which of the commands xsm has written this is: "launcher" (current),
    "guarded" (`python <path> || exit 1`, the form after 0.4.14 and before the
    launcher), "unguarded" (0.4.14 and before: Python's status 2 for a script it
    cannot open blocks the prompt), "other" for anything else marked ours, None
    for a command that is not xsm's."""
    command = command or ""
    if MARKER not in command:
        return None
    if "xsm-hook.py" in command:
        return "guarded" if "|| exit 1" in command else "unguarded"
    return "launcher" if "/hooks/xsm-hook" in command else "other"


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
    worker's allow-list, a statusLine) and onto PATH: the one in the runtime the
    hooks run from (runtime_root). The launcher works wherever it is called
    from, so those never depend on PATH."""
    return os.path.join(runtime_root(), "bin", "xsm")


def cli_path() -> str:
    """The `xsm` of the folder this CLI is running from, which is not
    launcher() while a snapshot is what is installed."""
    return os.path.join(REPO, "bin", "xsm")


# A version folder of the xsm plugin. Links into one are the only ones the
# installer moves on its own: a link to a checkout was made by a person, and a
# refresh run from the plugin must not pull it away from their working copy.
PLUGIN_CACHE = r"/plugins/cache/xsm/xsm/[^/]+/"


def _xsm_checkout(inside: str, depth: int, *tail: str) -> bool:
    """Whether `inside` (a real path) is `<xsm checkout>/<tail>`: the folder `depth`
    levels up holds xsm's own files, `xsm/__init__.py` and a plugin manifest naming
    xsm. Another checkout of xsm, a worktree, a clone elsewhere. A plugin version
    copy has the same files and is the plugin's, so it is not one (2026-10-02)."""
    parts = inside.split(os.sep)
    if len(parts) <= depth or tuple(parts[-len(tail):]) != tail:
        return False
    root = os.sep.join(parts[:-depth]) or os.sep
    if re.search(PLUGIN_CACHE, root + "/"):
        return False
    manifest = paths.read_json(os.path.join(root, ".claude-plugin", "plugin.json"))
    return os.path.isfile(os.path.join(root, "xsm", "__init__.py")) and \
        isinstance(manifest, dict) and manifest.get("name") == PLUGIN_NAME


def _own_link(source: str) -> bool:
    """Whether a link to `source` is one this checkout's install may move to the
    runtime it installs: a link into a snapshot, to the launcher of the checkout it
    was copied from (installing from it is asking for what `xsm` on PATH runs,
    2026-10-01), or to the launcher of any other xsm checkout, a worktree or a clone
    (2026-10-02: installing from one left the CLI on the old code while the hooks
    moved). Not from a plugin copy, which must never pull a link off a person's
    working copy or a snapshot."""
    if runtime_in_place():
        return False
    mine = [os.path.realpath(cli_path())]
    source_checkout = (runtime_current() or {}).get("source")
    if source_checkout:
        mine.append(os.path.realpath(os.path.join(source_checkout, "bin", "xsm")))
    return source in mine or _under_runtime(source) or _xsm_checkout(source, 2, "bin", "xsm")


def remove_cli() -> bool:
    """Unlink ~/.local/bin/xsm when it is a link into the runtime copies xsm made
    under ~/.xsm/runtime, which uninstall leaves behind otherwise (2026-10-02). A
    link to a checkout or to a plugin copy is not ours to remove."""
    target = os.path.expanduser("~/.local/bin/xsm")
    if not os.path.islink(target) or not _under_runtime(os.path.realpath(target)):
        return False
    os.unlink(target)
    return True


def install_cli() -> str:
    """Link the launcher on PATH, replacing a link into an older plugin version,
    and (from a checkout) a link into a snapshot or to the checkout itself."""
    target = os.path.expanduser("~/.local/bin/xsm")
    state = "linked"
    if os.path.islink(target):
        source = os.path.realpath(target)
        if source == os.path.realpath(launcher()):
            return "current"
        if _own_link(source):
            os.unlink(target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.symlink(launcher(), target)
            return "replaced"
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


def plugin_disabled(home: str) -> bool:
    """Whether this Claude home's settings switch the xsm plugin off
    (`enabledPlugins: {"xsm@xsm": false}`): its hooks do not run, so having it
    installed is no consent to register a session."""
    data = paths.read_json(_settings_file(home, "claude"))
    enabled = data.get("enabledPlugins") if isinstance(data, dict) else None
    return isinstance(enabled, dict) and any(
        (key == PLUGIN_NAME or key.startswith(PLUGIN_NAME + "@")) and value is False
        for key, value in enabled.items())


def plugin_root(home: str) -> str | None:
    """The folder of the xsm plugin in this home (`<root>/bin/xsm` runs that
    version by its absolute path), or None."""
    entry = _plugin_entry(home)
    if entry is not None:
        return entry.get("installPath") or None
    codex = codex_plugin(home)
    return codex["root"] if codex else None


def git_describe() -> str | None:
    """`git describe --tags --always --dirty` when this package sits in a git
    checkout of its own; None for a plugin copy, without git, or when git takes
    more than two seconds."""
    if not os.path.exists(os.path.join(REPO, ".git")):
        return None
    try:
        out = subprocess.run(["git", "-C", REPO, "describe", "--tags", "--always", "--dirty"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def cli_info() -> dict:
    """This CLI: {"version", "describe", "path"}. Which version is running is
    not knowable from the name `xsm` on PATH (2026-10-01: a plugin, a checkout
    and sessions started before an update at once); this says."""
    return {"version": plugin_version(), "describe": git_describe(),
            "path": os.path.realpath(cli_path())}


def cli_text(info: dict) -> str:
    """`xsm 0.4.14 (git v0.4.14-5-gabc1234) at /abs/bin/xsm`, for `xsm
    --version` and doctor's `cli` line."""
    return "xsm %s%s at %s" % (info.get("version") or "?",
                               " (git %s)" % info["describe"] if info.get("describe") else "",
                               info["path"])


def xsm_on_path() -> list:
    """Each `xsm` on PATH other than this CLI, first one first: {"version",
    "describe", "path", "via"}, with the version its folder's plugin manifest
    says and `via` the name as PATH has it."""
    seen, found = {os.path.realpath(cli_path())}, []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        via = shutil.which("xsm", path=folder) if folder else None
        real = os.path.realpath(via) if via else None
        if not real or real in seen:
            continue
        seen.add(real)
        manifest = paths.read_json(os.path.join(os.path.dirname(os.path.dirname(real)),
                                                ".claude-plugin", "plugin.json"), {})
        found.append({"version": manifest.get("version") if isinstance(manifest, dict) else None,
                      "describe": None, "path": real, "via": via})
    return found


# What makes xsm run, relative to its folder: the package, the hooks, the skill
# and the launcher. A copy that cannot name its commit is compared by these.
RUNTIME_FILES = ("xsm/*.py", "hooks/*", "skills/**", "bin/xsm")
# A commit id as git prints it. Checked before one goes on a git command line.
_REV = re.compile(r"[0-9a-f]{7,64}")


def _git(folder: str, *args: str):
    """`git -C <folder> <args>`, or None when git is missing or takes more than
    two seconds."""
    try:
        return subprocess.run(["git", "-C", folder] + list(args), capture_output=True,
                              text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None


def git_head(folder: str) -> str | None:
    """The commit a folder's own git checkout is at, or None. The `.git` must be
    in the folder itself: a plugin copy inside a repository (a Claude home kept
    in git) would otherwise report that repository's HEAD."""
    if not os.path.exists(os.path.join(folder, ".git")):
        return None
    out = _git(folder, "rev-parse", "HEAD")
    head = out.stdout.strip() if out is not None and out.returncode == 0 else ""
    return head if _REV.fullmatch(head) else None


def plugin_revision(root: str | None) -> str | None:
    """The commit a plugin copy was made from: the `revision` Codex's marketplace
    install records in `.codex-marketplace-install.json`, else the HEAD of the
    copy's own git clone (a Codex cache is one; measured 2026-10-01), else None.
    A Claude plugin copy has neither."""
    if not root:
        return None
    data = paths.read_json(os.path.join(root, ".codex-marketplace-install.json"), {})
    rev = data.get("revision") if isinstance(data, dict) else None
    return rev if isinstance(rev, str) and _REV.fullmatch(rev) else git_head(root)


def runtime_files(root: str, patterns: tuple = RUNTIME_FILES) -> list:
    """The files of a folder that `patterns` name, sorted."""
    return sorted({p for pattern in patterns
                   for p in glob.glob(os.path.join(root, pattern), recursive=True)
                   if os.path.isfile(p) and "__pycache__" not in p})


def runtime_digest(root: str, patterns: tuple = RUNTIME_FILES) -> str | None:
    """A hash of the names and bytes of `patterns` (RUNTIME_FILES) in a folder,
    or None when there are none or one cannot be read."""
    names = runtime_files(root, patterns)
    if not names:
        return None
    digest = hashlib.sha256()
    for name in names:
        try:
            with open(name, "rb") as fh:
                digest.update(os.path.relpath(name, root).encode() + b"\0" + fh.read() + b"\0")
        except OSError:
            return None
    return digest.hexdigest()


def plugin_older(version: str | None, root: str | None, revision: str | None = None,
                 head: str | None = None) -> str | None:
    """How the plugin copy `version` at `root` stands to this CLI, or None when
    it is the same code or nothing says. `revision` is the copy's commit
    (plugin_revision), `head` this checkout's (git_head).

    A lower version is older. The same version says nothing (a checkout keeps
    its version for many commits), so the commits decide: this checkout's git
    says how many the copy is behind, or that its commit is no ancestor (or
    unknown here: not fetched, so it just differs). A copy with no commit is
    compared by its runtime files. The first cut called every copy "older"
    while this CLI was past its tag, assuming a copy has no history; a Codex
    cache is a clone, and one identical to HEAD was flagged (2026-10-01). Never
    "older" without evidence."""
    mine = plugin_version()
    if not (version and mine and version[:1].isdigit() and mine[:1].isdigit()):
        return None
    if version_key(version) < version_key(mine):
        return "older than this CLI (%s)" % mine
    if version_key(version) > version_key(mine) or not root or \
            os.path.realpath(root) == os.path.realpath(REPO):
        return None
    if head and revision:
        if head.startswith(revision):
            return None
        behind = _git(REPO, "merge-base", "--is-ancestor", revision, head)
        count = _git(REPO, "rev-list", "--count", "%s..%s" % (revision, head)) \
            if behind is not None and behind.returncode == 0 else None
        n = count.stdout.strip() if count is not None and count.returncode == 0 else ""
        if n.isdigit():
            return None if n == "0" else "%s behind this CLI (%s)" % (
                "1 commit" if n == "1" else "%s commits" % n, revision[:7])
        return "differs from this CLI (%s)" % revision[:7]
    theirs, ours = runtime_digest(root), runtime_digest(REPO)
    return "differs from this CLI" if theirs and ours and theirs != ours else None


# --- the runtime the installed hooks run from -------------------------------------------
#
# macOS privacy protection can deny a session's app the folder a checkout lives
# in (~/Documents), and `python .../Documents/.../xsm-hook.py` then answered
# status 2, which Claude Code reads as "block": every prompt of every session
# stopped (2026-10-01). User decision, the same day: the running code moves out
# of the checkout. `xsm install` copies what xsm needs at run time to
# ~/.xsm/runtime/<id>/ and points the hooks, the MCP server and `xsm` on PATH at
# that copy; the checkout is for development (--dev, or policy runtime=checkout).

SNAPSHOTS = "runtime"           # ~/.xsm/runtime/<id>/, current.json, current -> <id>
CURRENT = "current.json"        # which snapshot is installed, and the checkout it came from
LINK = "current"                # a path that stays the same while the snapshot changes
RETIRED = ".retired"            # in a snapshot a newer one replaced: when it was
SNAPSHOT_FILES = RUNTIME_FILES + (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
_ID = re.compile(r"[0-9a-f]{12}")


def runtime_dir(*parts: str) -> str:
    return paths.path(SNAPSHOTS, *parts)


def _under_runtime(path: str) -> bool:
    return (os.path.realpath(path) + os.sep).startswith(os.path.realpath(runtime_dir()) + os.sep)


def runtime_in_place() -> bool:
    """Whether this CLI runs from what the hooks run from already: a snapshot,
    or a copy of the Claude or Codex plugin. There is nothing to copy then."""
    return _under_runtime(REPO) or bool(re.search(PLUGIN_CACHE, os.path.realpath(REPO) + "/"))


def runtime_current() -> dict | None:
    """The installed snapshot, as current.json records it plus its `path`, or
    None when none is installed or its folder is gone."""
    record = paths.read_json(runtime_dir(CURRENT))
    if not isinstance(record, dict) or not isinstance(record.get("id"), str) \
            or not _ID.fullmatch(record["id"]):
        return None
    folder = runtime_dir(record["id"])
    return dict(record, path=folder) if os.path.isdir(folder) else None


def runtime_root(planned: bool = False) -> str:
    """The folder the hooks, the MCP server and `xsm` on PATH run from: this
    checkout for `--dev` (policy runtime=checkout) and when this CLI is itself a
    snapshot or a plugin copy; else the installed snapshot, or this folder until
    one is installed. `planned`: where `xsm install` would put the snapshot of
    this checkout, for a dry run."""
    if runtime_in_place() or policy.get("runtime") == "checkout":
        return REPO
    if planned:
        digest = runtime_digest(REPO, SNAPSHOT_FILES)
        return runtime_dir(digest[:12]) if digest else REPO
    current = runtime_current()
    return current["path"] if current else REPO


def make_snapshot() -> dict:
    """Copy this checkout's runtime to ~/.xsm/runtime/<id>/ (the first 12 hex of
    runtime_digest over SNAPSHOT_FILES, so the same code is the same folder and a
    dirty checkout is not mistaken for its commit), make it the installed one,
    and return what current.json now says. The folder is built beside and renamed
    into place, so a session never sees half of it. OSError when it cannot be
    made; the caller then runs from the checkout as before."""
    digest = runtime_digest(REPO, SNAPSHOT_FILES)
    if not digest:
        raise OSError("no xsm files to copy under %s" % REPO)
    sid, previous = digest[:12], runtime_current()
    dest = runtime_dir(sid)
    os.makedirs(runtime_dir(), mode=0o700, exist_ok=True)
    intact = os.path.isdir(dest) and runtime_digest(dest, SNAPSHOT_FILES) == digest
    if not intact:
        tmp = dest + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        for name in runtime_files(REPO, SNAPSHOT_FILES):
            target = os.path.join(tmp, os.path.relpath(name, REPO))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(name, target)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    record = {"id": sid, "source": REPO, "rev": git_head(REPO), "describe": git_describe(),
              "version": plugin_version()}
    link, tmp_link = runtime_dir(LINK), runtime_dir(LINK + ".new")
    if intact and previous and previous["id"] == sid and \
            all(previous.get(k) == v for k, v in record.items()) and \
            not os.path.exists(os.path.join(dest, RETIRED)) and \
            os.path.islink(link) and os.readlink(link) == sid:
        return previous         # nothing changed: a second refresh rewrites nothing
    if previous and previous["id"] != sid:
        try:                    # sessions that started before now may still run its hooks
            with open(os.path.join(previous["path"], RETIRED), "w") as fh:
                fh.write("%f\n" % time.time())
        except OSError:
            pass
    try:
        os.unlink(os.path.join(dest, RETIRED))
    except OSError:
        pass
    try:
        os.unlink(tmp_link)
    except OSError:
        pass
    os.symlink(sid, tmp_link)
    os.replace(tmp_link, link)
    record["made"] = time.time()
    paths.write_json(runtime_dir(CURRENT), record, mode=0o644)
    return dict(record, path=dest)


def _epoch(lstart: str | None) -> float | None:
    try:
        return time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
    except (TypeError, ValueError):
        return None


def _live_session_starts() -> list:
    """When each live session started: those xsm registered, and every Claude
    session its homes' own sessions/ folder lists (one whose hook has not run is
    in no xsm record, and its hooks are the ones a snapshot may still serve)."""
    from . import identity, registry
    stamps = [rec.get("lstart") for rec in registry.cheap_records()]
    for home in config.homes():
        if home.get("runtime") != "claude":
            continue
        for p in glob.glob(os.path.join(home["path"], "sessions", "*.json")):
            pid = (paths.read_json(p, {}) or {}).get("pid")
            if pid and identity.pid_alive(pid):
                stamps.append(identity.lstart(pid))
    return [_epoch(s) or 0.0 for s in stamps]       # one whose start is unknown counts as old


def _snapshot_ids(text: str) -> set:
    """The snapshot ids a text names as a folder of the runtime directory."""
    found = set()
    for folder in {runtime_dir(), os.path.realpath(runtime_dir())}:
        found |= set(re.findall(re.escape(folder) + r"/([0-9a-f]{12})/", text))
    return found


def _running_from() -> set:
    """The ids of the snapshots a running process was started from: an MCP
    server or a worker has the folder in its command line. All of them when the
    process table cannot be read."""
    try:
        out = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        try:
            return {n for n in os.listdir(runtime_dir()) if _ID.fullmatch(n)}
        except OSError:
            return set()
    return _snapshot_ids(out)


def _configured_for() -> set:
    """The ids of the snapshots a config file or the PATH link names: the
    settings, hooks and MCP entries of every home xsm knows, ~/.claude.json,
    and ~/.local/bin/xsm. A home `xsm install` did not touch this time keeps
    the snapshot it was given."""
    files = [os.path.expanduser("~/.claude.json")]
    for home in config.homes():
        files += [os.path.join(home["path"], name)
                  for name in ("settings.json", "hooks.json", "config.toml", ".claude.json")]
    texts = [_read_text(f) or "" for f in files]
    link = os.path.expanduser("~/.local/bin/xsm")
    texts.append(os.readlink(link) + "/" if os.path.islink(link) else "")
    found = set()
    for text in texts:
        found |= _snapshot_ids(text)
    return found


def prune_snapshots(now: float | None = None) -> list:
    """Remove the snapshots nothing needs; the ids removed. One stays while it is
    the installed one, a config file names it (_configured_for), a process runs
    from it (_running_from), or a session that started before a newer one
    replaced it is still alive (it read its hooks when it started, and never
    reads them again). User decision, 2026-10-01: keep the previous one until no
    running session uses it."""
    now = time.time() if now is None else now
    current = (runtime_current() or {}).get("id")
    try:
        names = sorted(os.listdir(runtime_dir()))
    except OSError:
        return []
    for name in names:                  # a copy that was never finished
        full = runtime_dir(name)
        if name.endswith(".new") and os.path.isdir(full) and now - os.path.getmtime(full) > 600:
            shutil.rmtree(full, ignore_errors=True)
    old = [n for n in names if _ID.fullmatch(n) and n != current
           and os.path.isdir(runtime_dir(n)) and not os.path.islink(runtime_dir(n))]
    if not old:
        return []
    held = _configured_for() | _running_from()
    starts = None
    removed = []
    for name in old:
        if name in held:
            continue
        try:
            with open(runtime_dir(name, RETIRED)) as fh:
                retired = float(fh.read().strip())
        except (OSError, ValueError):
            retired = now               # unknown: any live session may be using it
        starts = _live_session_starts() if starts is None else starts
        if any(start < retired for start in starts):
            continue
        shutil.rmtree(runtime_dir(name), ignore_errors=True)
        removed.append(name)
    return removed


def runtime_status() -> dict:
    """For doctor: the installed snapshot, and whether the checkout it was made
    from has moved on since."""
    current = runtime_current()
    status = {"mode": policy.get("runtime"), "running_from": REPO, "in_place": runtime_in_place(),
              "snapshot": None, "checkout": None}
    if not current:
        return status
    status["snapshot"] = {k: current.get(k) for k in
                          ("id", "path", "source", "rev", "describe", "version", "made")}
    source = current.get("source")
    if not source or os.path.realpath(source) == os.path.realpath(current["path"]):
        return status
    if not os.path.isdir(source):
        status["checkout"] = {"path": source, "gone": True}
        return status
    digest = runtime_digest(source, SNAPSHOT_FILES)
    info = {"path": source, "same": bool(digest) and digest[:12] == current["id"], "ahead": None}
    head, rev = git_head(source), current.get("rev")
    if not info["same"] and head and isinstance(rev, str) and _REV.fullmatch(rev) and head != rev:
        count = _git(source, "rev-list", "--count", "%s..%s" % (rev, head))
        n = count.stdout.strip() if count is not None and count.returncode == 0 else ""
        info["ahead"] = int(n) if n.isdigit() else None
    status["checkout"] = info
    return status


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


def plugin_outdated_note(missing: list, home: str | None = None) -> str:
    """The agent asks its user and runs the update itself (user decision,
    2026-10-01: never send the person off to type a command); `claude plugin
    update` is a shell command, and a home other than ~/.claude needs its
    CLAUDE_CONFIG_DIR."""
    env = "" if not home or os.path.realpath(home) == os.path.realpath(
        os.path.expanduser("~/.claude")) else "CLAUDE_CONFIG_DIR=%s " % shlex.quote(home)
    return ("the installed plugin is older than this checkout (no %s hook): ask your user, then "
            "run `%sclaude plugin marketplace update xsm && %sclaude plugin update xsm@xsm` "
            "yourself; a new session picks it up" % (", ".join(missing), env, env))


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


def skill_source() -> str:
    """The skill folder a link points at or a copy is made from: the installed
    runtime's, since a session that macOS keeps out of the checkout's folder
    could not read a skill that lives there either (2026-10-01)."""
    return os.path.join(runtime_root(), "skills", "xsm")


def _own_skill_link(real: str) -> bool:
    """Whether a link (its real path) was made by an earlier `xsm install`: into a
    snapshot, or to the skill of this checkout, of the one the snapshot was copied
    from, or of any other xsm checkout (2026-10-02: it was called "something else"
    and left on the old code). Not from a plugin copy, which must not take a link
    off a working copy."""
    if _under_runtime(real):
        return True
    if runtime_in_place():
        return False
    mine = [os.path.realpath(os.path.join(REPO, "skills", "xsm"))]
    source = (runtime_current() or {}).get("source")
    if source:
        mine.append(os.path.realpath(os.path.join(source, "skills", "xsm")))
    return real in mine or _xsm_checkout(real, 2, "skills", "xsm")


def _skill_tree() -> dict:
    """{path relative to skills/xsm: text} for every file the skill ships.
    SKILL.md alone is not the skill any more: it points at references/, and a
    copy without them sends the model to a file that is not there."""
    source = skill_source()
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

    linked        our symlink, in step with the runtime
    link-stale    a link into another version of the xsm plugin, or into an earlier
                  snapshot or the checkout (the runtime moved: refresh relinks it)
    copy-current  a copied skill directory whose files all match ours
    copy-stale    a copied skill directory that has fallen behind
    nested-link   a link made *inside* an existing directory (ln -sfn into a dir)
    foreign       something else lives there; we leave it alone
    absent        nothing there yet
    """
    target = os.path.join(home, "skills", "xsm")
    source = skill_source()
    if os.path.islink(target):
        real = os.path.realpath(target)
        if real == os.path.realpath(source):
            return "linked", target
        if not _own_skill_link(real) and not re.search(PLUGIN_CACHE + "skills/xsm$", real):
            return "foreign", target
        header = (_read_text(os.path.join(target, "SKILL.md")) or "").splitlines()[:20]
        return ("link-stale" if not os.path.exists(target) or "name: xsm" in header
                else "foreign"), target
    if not os.path.exists(target):
        return "absent", target
    nested = os.path.join(target, "xsm")
    if os.path.islink(nested) and (os.path.realpath(nested) == os.path.realpath(source)
                                   or _own_skill_link(os.path.realpath(nested))):
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
    os.symlink(skill_source(), target)
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


def refresh_statusline(home: str) -> bool:
    """Point xsm's own statusLine at the launcher of the runtime that is installed
    now. Its command names a path, and the snapshot it named is pruned once
    nothing runs from it: a statusLine left on it would stop drawing, a person's
    own composed under it with it. True when it was rewritten."""
    target = _settings_file(home, "claude")
    data = paths.read_json(target)
    current = data.get("statusLine") if isinstance(data, dict) else None
    command = current.get("command") if isinstance(current, dict) else None
    if not isinstance(command, str) or MARKER not in command:
        return False
    want = statusline_command(target if " --base " in command else None)
    if command == want:
        return False
    _backup(target)
    current["command"] = want
    paths.write_json(target, data, mode=0o644)
    return True


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
                   "frameworks ignore", "remote add", "held deliver")
ALLOWED = "allowed"           # one note per settings file: what xsm added to its allow list
NOTE_VERSION = 2              # a note without it predates counting the rules already there
# Rules the eb600e8 build wrote and the list no longer holds. Only xsm ever
# wrote these exact strings, so a home whose note is missing or old loses them.
STALE_RULES = ("Bash(xsm spawn:*)", "Bash(xsm post:*)", "Bash(xsm doc add:*)")

# Messaging itself, which the lists above left to Claude's classifier: auto mode
# refused `xsm send` as it would any command it cannot tell the person wants,
# and default mode asks, so a message the person wanted between their own
# sessions could stop on a permission. User decision, 2026-10-01 ("a conversation
# the user wants must never be blocked by permissions"): the commands that only
# send, read, list or report are allowed, by the name `xsm` and by the absolute
# path of the runtime (what the receive context and the guide hand an agent
# where `xsm` is not on PATH), and so are the MCP tools that message. This
# reverses the narrowing of 2026-10-01 (review) for messaging only: spawn, `xsm
# post` and `doc add` stay with the classifier. Policy allow_messaging=false
# takes them out again on the next refresh.
MESSAGING_COMMANDS = ("send", "inbox", "list", "who", "held", "ledger", "status", "doctor",
                      "--version")
MESSAGING_TOOLS = ("xsm_send", "xsm_inbox", "xsm_post", "xsm_channel")


def mcp_tool_names() -> list:
    """The MCP form tools in the allow list: all 0.4.13 and 0.4.14 wrote. Only
    xsm's own tools carry these names, so they are xsm's whatever the note says
    (2026-10-01: an old install after an uninstall adds them with no note)."""
    return [prefix + tool for prefix in FORM_TOOL_PREFIXES for tool in FORM_TOOLS]


def messaging_mcp_names() -> list:
    """The MCP messaging tools in the allow list; xsm's own names, like the form tools."""
    return [prefix + tool for prefix in FORM_TOOL_PREFIXES for tool in MESSAGING_TOOLS]


def rule_roots(home: str) -> list:
    """The runtimes whose `<root>/bin/xsm` an agent in this home may be handed: the
    one installed, and the plugin's copy when the home has the plugin."""
    roots = [runtime_root()]
    plugin = plugin_root(home)
    if plugin and plugin not in roots:
        roots.append(plugin)
    return roots


def messaging_rules(home: str) -> list:
    rules = ["Bash(xsm %s:*)" % c for c in MESSAGING_COMMANDS]
    for root in rule_roots(home):
        rules += ["Bash(%s %s:*)" % (os.path.join(root, "bin", "xsm"), c)
                  for c in MESSAGING_COMMANDS]
    return rules + messaging_mcp_names()


def wanted_rules(home: str) -> list:
    """What `allow_form_tools` keeps in this home's allow list."""
    return form_tool_names() + (messaging_rules(home) if policy.get("allow_messaging") else [])


def form_tool_names() -> list:
    return mcp_tool_names() + \
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
    return [n for n in wanted_rules(home) if n not in allow]


def _xsm_rules(note, allow: list) -> list:
    """What xsm put in this allow list. A current note says so itself, and the
    MCP tool names besides (mcp_tool_names: an old install after an uninstall
    puts them back and notes nothing). With no note, or one from before
    NOTE_VERSION (it holds only what its refresh newly added), every known name
    already in the list is xsm's: an earlier version put them there, and they
    look exactly like a person's (2026-10-01: a refresh over such a home left
    about 24 rules behind after uninstall)."""
    held = list(note["added"]) if isinstance(note, dict) and isinstance(note.get("added"), list) \
        else []
    known = (mcp_tool_names() if isinstance(note, dict) and note.get("v") == NOTE_VERSION
             else form_tool_names()) + messaging_mcp_names()
    return held + [n for n in known if n in allow and n not in held]


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
    wanted = wanted_rules(home)
    # What xsm added and this list no longer wants: the absolute path of a runtime
    # a refresh replaced, messaging rules the person switched off. Only a rule
    # xsm added is ever taken out.
    stale = ([] if current else [n for n in STALE_RULES if n in allow]) + \
        [n for n in added if n in allow and n not in wanted and n not in STALE_RULES]
    missing = [n for n in wanted if n not in allow]
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
        added = [n for n in added if n not in stale] + [n for n in missing if n not in added]
    if missing or stale or not current:
        record = dict(note) if isinstance(note, dict) else {}
        record.update(file=target, added=added, v=NOTE_VERSION)
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
        # that holds nothing after xsm's rules went was xsm's to begin with, but
        # an empty `permissions` may be the person's own, and it is left unless
        # the note says xsm made it (2026-10-01).
        created = note.get("created") if isinstance(note, dict) and \
            isinstance(note.get("created"), list) else None
        if not kept and (created is None or "allow" in created):
            del perms["allow"]
        if not perms and "permissions" in (created or []):
            del data["permissions"]
        paths.write_json(target, data, mode=0o644)
    if isinstance(note, dict):
        # Emptied, not deleted: with no note a second uninstall would fall back
        # to the known names and take the rules the user had.
        paths.write_json(note_path, {"file": target, "added": [], "v": NOTE_VERSION})
    return len(kept) != len(allow)


# Claude holds a message from another session for its person when the two
# sessions' permission modes differ (a bypass session writing to one that asks),
# so the sender's "delivered" is "held for a person who may be away". The
# documented answer is the setting `crossSessionInbound` (Claude Code 2.1.224 and
# later; code.claude.com/docs/en/settings-reference#crosssessioninbound):
# "accept" delivers whatever the modes are. User decision, 2026-10-01: install
# sets it, in each Claude home, unless the home already says something: a
# "hold" or "refuse" there is the person's own choice and stays.
INBOUND_KEY = "crossSessionInbound"


def set_inbound(home: str) -> str:
    """Set crossSessionInbound to "accept" in this Claude home's settings, and note
    that xsm did, so uninstall takes out only that: set | already | off (policy
    claude_inbound=leave) | invalid (settings xsm cannot read: untouched) |
    kept:<value> (the home says another value, which is left as it is)."""
    if policy.get("claude_inbound") != "accept":
        return "off"
    if settings_invalid(home):
        return "invalid"
    target = _settings_file(home, "claude")
    data = paths.read_json(target, {}) or {}
    have = data.get(INBOUND_KEY)
    if have is not None:
        return "already" if have == "accept" else "kept:%s" % have
    if os.path.exists(target):
        _backup(target)
    data[INBOUND_KEY] = "accept"
    paths.write_json(target, data, mode=0o644)
    note_path = _state_file(ALLOWED, target)
    note = paths.read_json(note_path)
    # No `v` in a note made here: allow_form_tools reads one without it as an old
    # home and claims the rules an earlier version put there.
    record = dict(note) if isinstance(note, dict) else {"file": target}
    record["inbound"] = "accept"
    paths.write_json(note_path, record)
    return "set"


def remove_inbound(home: str) -> bool:
    """Take out the crossSessionInbound xsm set, and only that one: not a value the
    person has since changed, and not one that was there before xsm."""
    target = _settings_file(home, "claude")
    data = paths.read_json(target)
    note_path = _state_file(ALLOWED, target)
    note = paths.read_json(note_path)
    if not isinstance(data, dict) or not isinstance(note, dict) or not note.get("inbound"):
        return False
    done = data.get(INBOUND_KEY) == note["inbound"]
    if done:
        _backup(target)
        del data[INBOUND_KEY]
        paths.write_json(target, data, mode=0o644)
    note.pop("inbound")
    paths.write_json(note_path, note)
    return done


def inbound_state(home: str) -> str | None:
    """What this Claude home's settings say about crossSessionInbound, for doctor:
    the value, or None when it says nothing."""
    data = paths.read_json(_settings_file(home, "claude"))
    value = data.get(INBOUND_KEY) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


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
    if os.path.islink(target) and (os.path.realpath(target) == os.path.realpath(skill_source())
                                   or _own_skill_link(os.path.realpath(target))):
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


def _command_script(command: str | None) -> str | None:
    """The hook file a marked command runs (the launcher or xsm-hook.py), or None."""
    found = re.search(r"(/[^\s\"']*/hooks/xsm-hook(?:\.py)?)", command or "")
    return found.group(1) if found else None


def plan(home: str, runtime: str, root: str | None = None) -> dict:
    """What install would change. Used by --dry-run and by doctor. `root` is
    the runtime the hooks would run from (default runtime_root())."""
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
        theirs = ours[0]["hooks"][0].get("command") if len(ours) == 1 else None
        want = hook_command(runtime, event, root=root, existing=theirs)
        # "keep" means exactly one marked group with exactly the right command:
        # a duplicate or a stale path still needs replacing.
        have = len(ours) == 1 and _same_command(theirs, want) and \
            ours[0]["hooks"][0].get("timeout", 10) == TIMEOUTS.get(event, 10) and \
            ours[0].get("matcher") == MATCHERS.get(event)
        action = {"event": event, "others": len(groups) - len(ours),
                  "action": "keep" if have else ("replace" if ours else "add"),
                  "command": want}
        if ours:
            # What is there now, for doctor: the form (hook_form) and whether the
            # file it runs is still on disk.
            now = [h.get("command") for g in ours for h in g.get("hooks", [])]
            action["forms"] = sorted({hook_form(c) or "other" for c in now})
            action["gone"] = [s for s in {_command_script(c) for c in now}
                              if s and not os.path.exists(s)]
        actions.append(action)
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


def diff(home: str, runtime: str, root: str | None = None) -> str:
    result = plan(home, runtime, root)
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
    cli = cli_info()
    cli["on_path"] = xsm_on_path()
    cli["revision"] = git_head(REPO)
    plugins = {h["path"]: plugin_installed(h["path"]) for h in homes}
    roots = {h["path"]: plugin_root(h["path"]) for h in homes if plugins[h["path"]]}
    revisions = {home: plugin_revision(root) for home, root in roots.items()}
    report = {
        "xsm_home": paths.HOME,
        "runtime": runtime_status(),
        "policy": policy.report(),
        "inbound": {h["path"]: inbound_state(h["path"]) for h in homes
                    if h.get("runtime") == "claude"},
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
        "cli": cli,
        "plugins": plugins,
        "plugin_roots": roots,
        "plugin_revisions": revisions,
        "plugin_older": {home: plugin_older(plugins[home], root, revisions[home], cli["revision"])
                         for home, root in roots.items()},
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
    """What the MCP server is registered to run: the sh launcher of the runtime,
    which finds an interpreter itself (a pinned python that went away, a brew
    upgrade, no longer leaves the xsm tools gone), like the plugin's."""
    return [os.path.join(runtime_root(), "hooks", "xsm-mcp")]


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
    # The name first: Claude's -e takes every word up to the next option or `--`,
    # so `-e XSM_HOME=x xsm --` read `xsm` as a variable ("Invalid environment
    # variable format: xsm"; `claude mcp add --help` shows the name before -e).
    argv = [_mcp_cli(runtime), "mcp", "add"] + (["--scope", "user"] if runtime == "claude" else []) \
        + [MCP_NAME] + extra + ["--"] + mcp_command()
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
