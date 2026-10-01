"""User configuration: declared homes and communication scope.

JSON, not TOML, on purpose: a hook may run under /usr/bin/python3 (3.9), which
has no tomllib, and a hook that cannot import its own config is a hook that
silently stops gating (S8-g2).

Scope default (user decision, 2026-09-20): two sessions may talk when their
cwds sit in the same git repository. Anything wider has to be written down in
config.json.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import time

from . import paths

CONFIG = "config.json"
HOMES = "homes.json"

DEFAULT_CONFIG = {
    # True: every peer message without an xsm header is held. False: Claude's
    # own messages pass unless xsm knows the sender and it is out of scope, or
    # they come from off this machine (ADR-0013, amended 2026-09-30).
    "strict_peers": False,
    "same_repo_scope": True,  # the default rule below
    "scopes": [],             # explicit cross-repo scopes
    "max_depth": 1,           # worker levels below a top-level session (1: workers spawn none)
    "max_workers": 4,         # workers one session may have running at once
    "deny": [],               # session refs that may neither send nor receive (ADR-0009)
    "ignore_frameworks": [],  # frameworks inside which xsm still starts workers: orca, herdr, all
    "telemetry_retention_days": 7,   # spans and metric points (ADR-0011); 0 keeps them forever
    "codex_wake": True,       # ask Codex's daemon to start a queued message (ADR-0002 appendix)
}


def load() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(paths.read_json(paths.path(CONFIG), {}) or {})
    return cfg


# What a hold that was opened (user decision, 2026-10-01: a conversation the
# person wants between their own sessions is never blocked by a rule or a
# failure they cannot see) does by default, and how to close it again. Each
# is a key of config.json, and XSM_<NAME in capitals> overrides it, so a
# machine can be put back the way it was without editing a file.
POLICY_DEFAULTS = {
    "fail_open": True,             # false: a peer message is blocked when the hook breaks (S8-g2)
    "remote_native": "pass",       # "hold": Claude messages from off this machine are held
    "stale_sender": "pass",        # "hold": a message whose sender has exited is held
    "reply_from_request": True,    # false: the person's own request is not read as their yes
    "reply_flag": True,            # false: `--reply "<their words>"` is not accepted
}
_FALSE = ("0", "false", "no", "off")
_TRUE = ("1", "true", "yes", "on")


def policy(name: str, default=None, cfg: dict | None = None):
    """The value of one policy switch: the environment (XSM_<NAME>), then
    config.json, then `default` (POLICY_DEFAULTS' when none is given). A value
    of the wrong kind is ignored. Never raises: a hook asks."""
    if default is None:
        default = POLICY_DEFAULTS.get(name)
    raw = os.environ.get("XSM_" + name.upper())
    if raw is None or not raw.strip():
        try:
            raw = (cfg if cfg is not None else load()).get(name)
        except Exception:                          # noqa: BLE001 - a broken file is no policy
            raw = None
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower() if raw is not None else ""
        return True if text in _TRUE else False if text in _FALSE else default
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return default


def policy_report() -> dict:
    """Every switch as it stands now, for `xsm doctor`."""
    return {name: policy(name) for name in POLICY_DEFAULTS}


def config_hash() -> str:
    raw = json.dumps(load(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def homes() -> list:
    """Declared homes. The registry unions these with the homes hook records
    report, so a profile at a non-default path is never guessed by globbing."""
    return paths.read_json(paths.path(HOMES), []) or []


def add_home(home_path: str, runtime: str, alias: str | None = None) -> dict:
    home_path = os.path.realpath(os.path.expanduser(home_path))
    entry = {"path": home_path, "runtime": runtime,
             "alias": alias or alias_of(home_path)}
    current = [h for h in homes() if h.get("path") != home_path]
    current.append(entry)
    paths.write_json(paths.path(HOMES), sorted(current, key=lambda h: h["path"]), mode=0o644)
    return entry


def remove_home(home_path: str) -> bool:
    home_path = os.path.realpath(os.path.expanduser(home_path))
    current = homes()
    kept = [h for h in current if h.get("path") != home_path]
    if len(kept) == len(current):
        return False
    paths.write_json(paths.path(HOMES), kept, mode=0o644)
    return True


def alias_of(home_path: str) -> str:
    """~/.claude-3 -> claude-3, ~/.codex -> codex."""
    return os.path.basename(home_path.rstrip("/")).lstrip(".")


def git_root(cwd: str) -> str | None:
    """The top of the working tree `cwd` is in: each linked worktree has its
    own. Folders are linked, reached and joined by this."""
    return git_repo(cwd)[0]


def git_repo(cwd: str) -> tuple:
    """(working-tree top, repository) for `cwd`, or (None, None) outside git.

    The repository is the git common dir, the .git every linked worktree of
    one clone shares. Comparing working-tree tops called a worktree Orca or
    `claude --worktree` made and its main checkout "different git
    repositories", so the default same-repository scope failed for the very
    sessions it was for (issue #8 follow-up, 2026-10-01). Separate clones of
    one remote keep separate common dirs and stay apart."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel",
                              "--git-common-dir"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None, None
    lines = out.stdout.strip().splitlines()
    if out.returncode != 0 or len(lines) != 2 or not lines[0]:
        return None, None
    # An older git prints the common dir relative to `cwd`.
    common = lines[1] if os.path.isabs(lines[1]) else os.path.join(cwd, lines[1])
    return os.path.realpath(lines[0]), os.path.realpath(common)


def repo_name(common_dir: str) -> str:
    """A repository's name from its common dir: the folder that holds `.git`
    (the main checkout), or the bare repository's own name."""
    if os.path.basename(common_dir) == ".git":
        return os.path.basename(os.path.dirname(common_dir))
    name = os.path.basename(common_dir)
    return name[:-len(".git")] if name.endswith(".git") else name


def _expand(pattern: str) -> str:
    return os.path.realpath(os.path.expanduser(pattern)) if "*" not in pattern \
        else os.path.expanduser(pattern)


def member_matches(member: dict, session: dict) -> bool:
    if member.get("runtime") and member["runtime"] != session.get("runtime"):
        return False
    if member.get("home") and member["home"] not in (session.get("alias"), session.get("home")):
        return False
    root = member.get("root")
    if root:
        # A project joined with `xsm join`: the folder itself and everything
        # under it. A cwd glob cannot say this — `root*` would also match
        # `root-other`.
        cwd = os.path.realpath(session.get("cwd") or "")
        root = os.path.realpath(os.path.expanduser(root))
        if cwd != root and not cwd.startswith(root.rstrip("/") + "/"):
            return False
    pattern = member.get("cwd")
    if not pattern:
        return True
    cwd = os.path.realpath(session.get("cwd") or "")
    return fnmatch.fnmatch(cwd, _expand(pattern))


def scope_for(a: dict, b: dict, cfg: dict | None = None):
    """Returns (scope_id, reason). scope_id is None when the two sessions may
    not talk; reason explains the verdict either way."""
    cfg = cfg or load()
    scope, reason = _scope_for(a, b, cfg)
    if scope:
        return scope, reason
    link = _folder_link(a, b, cfg)
    if link:
        return link, "two folders your user linked"
    link = _worker_link(a, b)
    if link:
        return link, "a worker and the session that started it"
    link = _reach_link(a, b, cfg)
    if link:
        return link, "a reach your user allowed"
    return None, reason + _link_hint(a, b) + _half_joined(a, b, cfg) + _reach_hint(a, b)


def _folder_link(a: dict, b: dict, cfg: dict) -> str | None:
    """Two project folders a person linked (`xsm link`): every session under
    one and every session under the other, both ways, until unlinked."""
    for ln in cfg.get("links") or []:
        ra, rb = ln.get("a"), ln.get("b")
        if not (ra and rb):
            continue
        for x, y in ((a, b), (b, a)):
            if member_matches({"root": ra}, x) and member_matches({"root": rb}, y):
                return link_id(ra, rb)
    return None


def link_id(root_a: str, root_b: str) -> str:
    """The same id from either side: the two roots' basenames, in the order
    of their full paths."""
    pair = sorted(os.path.realpath(os.path.expanduser(r)) for r in (root_a, root_b))
    return "link:" + "+".join(os.path.basename(r.rstrip("/")) for r in pair)


def _link_hint(a: dict, b: dict) -> str:
    if not (a.get("cwd") and b.get("cwd")):
        return ""
    there = project_root(b["cwd"])
    # The agent asks and the agent runs it (user decision, 2026-10-01): the
    # command takes its user's reply as the verdict, the MCP tool a form.
    return ("; to connect the two folders, the session at %s asks its user and runs "
            "`xsm link %s` (or the xsm_link MCP tool)" % (project_root(a["cwd"]),
                                                       shlex.quote(there)))


def _reach_link(a: dict, b: dict, cfg: dict) -> str | None:
    """One session allowed by a person to talk with the sessions of one other
    folder, both ways, for as long as it runs (`xsm reach`). The folder's
    other sessions and the rest of the reaching session's project follow the
    usual rules; the id names the reaching session, so both sides compute it.
    The holder is matched on its full identity and run, not its ref
    (reach_holds): a ref is 24 bits and collided within ~11k synthetic tries,
    and a resumed session id comes back under a new pid (2026-09-28). Whether
    the holder still runs is not asked here: send refuses a target that is
    ended or stale, receive refuses such a sender, and SessionEnd plus
    housekeeping drop its reaches."""
    for r in cfg.get("reaches") or []:
        for mine, other in ((a, b), (b, a)):
            if reach_holds(r, mine) and member_matches({"root": r.get("root")}, other):
                return "reach:%s" % r["ref"]
    return None


def reach_holds(r: dict, session: dict) -> bool:
    """Whether `session` is the very run that reach `r` was granted to: same
    runtime, home and session id, and the same pid and process start time.
    `claude --resume` reuses the session id under a new pid, and the hourly
    prune used to be all that stood between a resume and a revived reach
    (2026-09-28). A reach stored before this binding has no session id and
    holds for nobody; granting it again stores the binding."""
    if not (r.get("session_id") and session.get("session_id")):
        return False
    if (session.get("runtime"), str(session.get("session_id"))) != \
            (r.get("runtime"), str(r.get("session_id"))):
        return False
    if os.path.realpath(session.get("home") or "") != os.path.realpath(r.get("home") or ""):
        return False
    if "pid" not in session or "lstart" not in session:
        # A probe that is not a registry record (a listing row, a test dict):
        # the pointer on disk says which run this session id is on now.
        from . import registry          # lazy: registry imports this module
        session = registry.by_session(r["runtime"], str(r["session_id"])) or {}
    return session.get("pid") == r.get("pid") and session.get("lstart") == r.get("lstart")


def _reach_hint(a: dict, b: dict) -> str:
    if not (a.get("ref") and b.get("cwd")):
        return ""
    return ("; or, to let only this session reach %s, it asks its user and runs `xsm reach %s` "
            "(or the xsm_reach MCP tool)" % (project_root(b["cwd"]),
                                            shlex.quote(project_root(b["cwd"]))))


def _worker_link(a: dict, b: dict) -> str | None:
    """A worker and the session that spawned it may always talk. A worker in a
    folder outside its parent's scope exists only because a person allowed it
    (an outside_scope grant, or the spawn ran at their terminal), yet the task
    and every reply used to be refused by this very check, so the worker came
    up and never heard a word (2026-09-28). The link covers that pair only:
    the worker and the other sessions of its folder follow the usual rules.
    Open policy question (2026-09-28): the link is matched on refs alone, so
    it still holds when the worker's process is gone and someone resumes the
    same child session id by hand. Whether a worker/parent relation should
    span a resume of the same conversation, as a reach deliberately does not,
    is undecided; the behaviour is left as it is until it is."""
    refs = {a.get("ref"), b.get("ref")}
    if None in refs or len(refs) != 2:
        return None
    from . import workers           # lazy: workers imports this module
    for w in workers.all_workers():
        if {w.get("ref"), w.get("parent_ref")} == refs:
            return "worker:%s" % w.get("name")
    return None


def joined_projects(session: dict, cfg: dict | None = None) -> set:
    """The ids of the named projects the session's folder has joined."""
    cfg = cfg or load()
    return {s.get("id") for s in cfg.get("scopes", [])
            if any(m.get("root") and member_matches(m, session) for m in s.get("members", []))}


def _half_joined(a: dict, b: dict, cfg: dict) -> str:
    """When one side has joined a project the other has not, say which and what
    would open it — otherwise the refusal reads as if joining had no effect."""
    ja, jb = joined_projects(a, cfg), joined_projects(b, cfg)
    notes = []
    for mine, other in ((ja - jb, b), (jb - ja, a)):
        for name in sorted(mine):
            notes.append("%s has not joined project %s (a session there asks its user and "
                         "runs `xsm join %s`)" % (project_root(other.get("cwd") or "/"), name,
                                                  name))
    return "; " + "; ".join(notes) if notes else ""


def default_project(cwd: str):
    """The project every session belongs to without joining anything: the git
    repository it started in, or that folder when there is none. Returns
    (scope_id, root)."""
    root, common = git_repo(cwd)
    if root:
        return "repo:" + repo_name(common), root
    folder = os.path.realpath(cwd)
    return "dir:" + os.path.basename(folder), folder


def _scope_for(a: dict, b: dict, cfg: dict):
    # The default project comes first. Named projects are joined *in addition*
    # to it, so two sessions of one repository keep talking under their
    # repository's scope even after that repository joins a named project.
    default = cfg.get("same_repo_scope", True)
    (ta, ra), (tb, rb) = git_repo(a.get("cwd") or ""), git_repo(b.get("cwd") or "")
    ca, cb = os.path.realpath(a.get("cwd") or "a"), os.path.realpath(b.get("cwd") or "b")
    if default and ra and ra == rb:
        return "repo:" + repo_name(ra), ("same git repository" if ta == tb else
                                         "same git repository (linked worktree)")
    if default and not ra and not rb and ca == cb:
        return "dir:" + os.path.basename(ca), "same directory"
    for scope in cfg.get("scopes", []):
        members = scope.get("members", [])
        if any(member_matches(m, a) for m in members) and \
           any(member_matches(m, b) for m in members):
            return scope.get("id", "unnamed"), "explicit scope"
    if not default:
        return None, "no explicit scope and the same-repo default is off"
    if not ra and not rb:
        return None, "different directories, neither in a git repository, and no explicit scope"
    if ra and rb:
        return None, "different git repositories and no explicit scope"
    inside, outside = (ca, cb) if ra else (cb, ca)
    return None, ("%s is in a git repository and %s is not, and no explicit scope covers both"
                  % (inside, outside))


# --- projects: scopes declared from a session with `xsm join` ------------------
#
# A project is a named scope whose members are folders. Joining adds the
# caller's project folder (its git root, or the folder itself outside a repo).
# Two folders talk only when each has joined the same name, so consent is
# given once per side: one side joining cannot pull the other in.

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def project_root(cwd: str) -> str:
    return git_root(cwd) or os.path.realpath(cwd)


def _raw() -> dict:
    return paths.read_json(paths.path(CONFIG), {}) or {}


def _save(raw: dict) -> None:
    paths.write_json(paths.path(CONFIG), raw, mode=0o644)


def projects() -> list:
    """Scopes that `xsm join` manages, i.e. those whose members are folders."""
    return [s for s in load().get("scopes", []) if any(m.get("root") for m in s.get("members", []))]


def check_join(name: str, raw: dict | None = None) -> dict | None:
    """Raise ValueError when `name` cannot be joined; returns its scope, if any.
    Checked before a typed consent is used up, so a bad name does not cost the
    person their command (2026-09-28)."""
    if not PROJECT_NAME_RE.match(name or ""):
        raise ValueError("project names are letters, digits, '.', '_' and '-' (at most 64)")
    raw = _raw() if raw is None else raw
    scope = next((s for s in raw.get("scopes") or [] if s.get("id") == name), None)
    if scope and scope.get("members") and not any(m.get("root") for m in scope["members"]):
        # A hand-written scope with the same id: joining would quietly widen it.
        raise ValueError("scope %r is written by hand in config.json; edit it there" % name)
    return scope


def join(name: str, cwd: str):
    """Add cwd's project folder to project `name`. Returns (scope, added)."""
    raw = _raw()
    scope = check_join(name, raw)
    root = project_root(cwd)
    if scope is None:
        scope = {"id": name, "members": []}
        raw.setdefault("scopes", []).append(scope)
    if any(os.path.realpath(m.get("root", "")) == root for m in scope["members"]):
        return scope, False
    scope["members"].append({"root": root})
    _save(raw)
    return scope, True


def leave(name: str, cwd: str) -> bool:
    root = project_root(cwd)
    raw = _raw()
    for scope in raw.get("scopes", []):
        if scope.get("id") != name:
            continue
        kept = [m for m in scope.get("members", [])
                if not m.get("root") or os.path.realpath(m["root"]) != root]
        if len(kept) == len(scope.get("members", [])):
            return False
        scope["members"] = kept
        if not kept:
            raw["scopes"] = [s for s in raw["scopes"] if s is not scope]
        _save(raw)
        return True
    return False


# --- links: two project folders, both ways, until unlinked ---------------------------
#
# User decision (2026-09-28): one side is enough. A tester could not connect two
# repositories because a project needed `xsm join` from both folders, each
# through a form Codex may decline without showing it. A link is written once,
# from either folder, by a person (their terminal, or the command they typed in
# the session: see consent.py). It widens, so only a person adds one; it
# narrows when dropped, so anyone may drop it.

def links() -> list:
    return list(load().get("links") or [])


def check_link(folder_a: str, folder_b: str) -> tuple:
    """The two project roots, or ValueError when they cannot be linked. Checked
    before a typed consent is used up: linking a folder not created yet used
    the consent, and the retry after creating it asked with a form
    (review, 2026-09-28)."""
    ra = project_root(os.path.expanduser(folder_a))
    rb = project_root(os.path.expanduser(folder_b))
    for folder, root in ((folder_a, ra), (folder_b, rb)):
        if not os.path.isdir(root):
            raise ValueError("%s is not a folder" % folder)
    if ra == rb:
        raise ValueError("%s and %s are the same project already" % (folder_a, folder_b))
    return ra, rb


def _same_link(entry: dict, ra: str, rb: str) -> bool:
    pair = {os.path.realpath(entry.get("a") or ""), os.path.realpath(entry.get("b") or "")}
    return pair == {ra, rb}


def add_link(folder_a: str, folder_b: str, by: str) -> tuple:
    """Link the projects of two folders. Returns (entry, added)."""
    ra, rb = check_link(folder_a, folder_b)
    raw = _raw()
    rows = raw.setdefault("links", [])
    for entry in rows:
        if _same_link(entry, ra, rb):
            return entry, False
    entry = {"a": ra, "b": rb, "t": time.time(), "by": by}
    rows.append(entry)
    _save(raw)
    return entry, True


def links_covering(root: str) -> list:
    """(linked root, the other root) for each link that covers `root`: its own,
    and those of a folder above it, which cover its sessions too (see
    member_matches). Outside a repository a subfolder is a project of its own,
    and `xsm projects` there said "no links" while its sessions were linked
    (review, 2026-09-28)."""
    root = os.path.realpath(root)
    out = []
    for ln in links():
        a, b = os.path.realpath(ln.get("a") or ""), os.path.realpath(ln.get("b") or "")
        for mine, other in ((a, b), (b, a)):
            if root == mine or root.startswith(mine.rstrip("/") + "/"):
                out.append((mine, other))
                break
    return out


def drop_link(folder_a: str, folder_b: str) -> bool:
    ra = project_root(os.path.expanduser(folder_a))
    rb = project_root(os.path.expanduser(folder_b))
    raw = _raw()
    rows = raw.get("links") or []
    kept = [e for e in rows if not _same_link(e, ra, rb)]
    if len(kept) == len(rows):
        return False
    raw["links"] = kept
    if not kept:
        raw.pop("links")
    _save(raw)
    return True


# --- reaches: one session to one other folder -----------------------------------------
#
# Joining a project opens two folders to each other for good. A reach is
# narrower: one running session and the sessions of one folder, for as long as
# that run of the session lasts: a resume of the same session id does not bring
# it back (2026-09-28). It widens who may talk, so only a person grants it; it
# narrows when dropped, so anyone may drop it (2026-09-28).

def reaches() -> list:
    return list(load().get("reaches") or [])


def check_reach(folder: str) -> str:
    """The folder's project root, or ValueError when there is none. Checked
    before a typed consent is used up, as for a link."""
    root = project_root(os.path.expanduser(folder))
    if not os.path.isdir(root):
        raise ValueError("%s is not a folder" % folder)
    return root


def add_reach(ref: str, folder: str, by: str, session: dict | None = None) -> tuple:
    """Let session `ref` talk with the sessions of `folder`'s project.
    Returns (entry, added).

    The entry is bound to the session's identity and current run (see
    reach_holds), taken from `session` when the caller has its record, else
    from the one running record with that ref. Two running sessions sharing a
    ref cannot be told apart here, so that is refused rather than guessed."""
    root = check_reach(folder)
    if session is None or not session.get("session_id") or "lstart" not in session:
        from . import registry          # lazy: registry imports this module
        running = [rec for rec in registry.records() if rec.get("ref") == ref
                   and rec.get("state") in ("live", "unknown")]
        if session is not None and session.get("session_id"):
            running = [rec for rec in running
                       if str(rec.get("session_id")) == str(session["session_id"])]
        if not running:
            raise ValueError("no running session has ref %s" % ref)
        if len(running) > 1:
            raise ValueError("%d running sessions share ref %s; name one with claude:<session id> "
                             "or codex:<session id>" % (len(running), ref))
        session = running[0]
    elif session.get("state") in ("ended", "stale"):
        raise ValueError("ref %s is not running (%s)" % (ref, session["state"]))
    holder = {k: session.get(k) for k in ("runtime", "session_id", "pid", "lstart")}
    holder["session_id"] = str(holder["session_id"])
    holder["home"] = os.path.realpath(session.get("home") or "")
    raw = _raw()
    rows = raw.setdefault("reaches", [])
    for r in list(rows):
        if r.get("ref") == ref and os.path.realpath(r.get("root", "")) == root:
            if all(r.get(k) == v for k, v in holder.items()):
                return r, False
            if not r.get("session_id") or (r.get("runtime"), r.get("session_id")) == \
                    (holder["runtime"], holder["session_id"]):
                rows.remove(r)          # an unbound or earlier-run row: replaced below
    entry = {"ref": ref, "root": root, "t": time.time(), "by": by}
    entry.update(holder)
    rows.append(entry)
    _save(raw)
    return entry, True


def drop_reach(ref: str, folder: str | None = None, session: dict | None = None) -> int:
    """Remove session `ref`'s reach to `folder`, or all of its reaches. With
    `session`, only the reaches of that session id (in that runtime and home),
    so a session sharing the ref keeps its own."""
    root = project_root(os.path.expanduser(folder)) if folder else None

    def mine(r):
        if r.get("ref") != ref:
            return False
        if root and os.path.realpath(r.get("root", "")) != root:
            return False
        if session is not None:
            return (r.get("runtime"), str(r.get("session_id"))) == \
                (session.get("runtime"), str(session.get("session_id"))) and \
                os.path.realpath(r.get("home") or "") == os.path.realpath(session.get("home") or "")
        return True
    return _drop_reach_rows(mine)


def drop_reaches(entries: list) -> int:
    """Remove exactly these reach entries (as reaches() returned them)."""
    return _drop_reach_rows(lambda r: r in entries)


def _drop_reach_rows(match) -> int:
    raw = _raw()
    rows = raw.get("reaches") or []
    kept = [r for r in rows if not match(r)]
    if len(kept) == len(rows):
        return 0
    raw["reaches"] = kept
    if not kept:
        raw.pop("reaches")
    _save(raw)
    return len(rows) - len(kept)


# --- blocked sessions (ADR-0009) ------------------------------------------------------
#
# One session, not a folder: the way to cut off a misbehaving session inside an
# otherwise allowed project. Blocking only narrows, so anyone may add a ref;
# lifting a block widens again, so only a person may.

def bare_ref(ref) -> str:
    """A session ref as `list` shows it: no `ref:` prefix, no spaces. 0.4.14
    stored `ref:abc123` as typed, which never matched a session (2026-10-01)."""
    ref = str(ref).strip()
    return ref[len("ref:"):].strip() if ref.startswith("ref:") else ref


def blocked() -> set:
    return {bare_ref(r) for r in load().get("deny") or [] if isinstance(r, str)}


def block(ref: str) -> bool:
    raw = _raw()
    refs = raw.setdefault("deny", [])
    if bare_ref(ref) in {bare_ref(r) for r in refs}:
        return False
    refs.append(bare_ref(ref))
    _save(raw)
    return True


def unblock(ref: str) -> bool:
    """Lifts the block, in either form it may have been stored in."""
    raw = _raw()
    refs = raw.get("deny") or []
    kept = [r for r in refs if bare_ref(r) != bare_ref(ref)]
    if len(kept) == len(refs):
        return False
    raw["deny"] = kept
    _save(raw)
    return True


# --- frameworks whose terminals xsm leaves alone -------------------------------------
#
# Inside Orca or herdr, xsm starts and stops no workers: the framework owns them
# (user decision, 2026-09-21). A person can lift that per framework (2026-09-23:
# a Codex session inside Orca is to orchestrate xsm workers itself). Lifting
# widens what agents can do there, so only a person may; restoring narrows, so
# anyone may.

FRAMEWORK_NAMES = ("orca", "herdr")


def ignored_frameworks() -> set:
    return set(load().get("ignore_frameworks") or [])


def set_framework_ignored(name: str, ignore: bool) -> bool:
    if name not in FRAMEWORK_NAMES + ("all",):
        raise ValueError("unknown framework %r: one of %s, or all" % (
            name, ", ".join(FRAMEWORK_NAMES)))
    raw = _raw()
    current = set(raw.get("ignore_frameworks") or [])
    names = set(FRAMEWORK_NAMES) | {"all"} if (name == "all" and not ignore) else {name}
    updated = (current | names) if ignore else (current - names)
    if updated == current:
        return False
    raw["ignore_frameworks"] = sorted(updated)
    _save(raw)
    return True
