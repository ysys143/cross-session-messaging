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
import subprocess

from . import paths

CONFIG = "config.json"
HOMES = "homes.json"

DEFAULT_CONFIG = {
    "strict_peers": True,     # envelope without an xsm header is refused on receive
    "same_repo_scope": True,  # the default rule below
    "scopes": [],             # explicit cross-repo scopes
}


def load() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(paths.read_json(paths.path(CONFIG), {}) or {})
    return cfg


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
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    root = out.stdout.strip()
    return os.path.realpath(root) if out.returncode == 0 and root else None


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
    return None, reason + _half_joined(a, b, cfg)


def _half_joined(a: dict, b: dict, cfg: dict) -> str:
    """When one side has joined a project the other has not, say which and what
    would open it — otherwise the refusal reads as if joining had no effect."""
    def joined(session):
        return {s.get("id") for s in cfg.get("scopes", [])
                if any(m.get("root") and member_matches(m, session) for m in s.get("members", []))}
    ja, jb = joined(a), joined(b)
    notes = []
    for mine, other in ((ja - jb, b), (jb - ja, a)):
        for name in sorted(mine):
            notes.append("%s has not joined project %s (run /xsm-join %s there)"
                         % (project_root(other.get("cwd") or "/"), name, name))
    return "; " + "; ".join(notes) if notes else ""


def default_project(cwd: str):
    """The project every session belongs to without joining anything: the git
    repository it started in, or that folder when there is none. Returns
    (scope_id, root)."""
    root = git_root(cwd)
    if root:
        return "repo:" + os.path.basename(root), root
    folder = os.path.realpath(cwd)
    return "dir:" + os.path.basename(folder), folder


def _scope_for(a: dict, b: dict, cfg: dict):
    # The default project comes first. Named projects are joined *in addition*
    # to it, so two sessions of one repository keep talking under their
    # repository's scope even after that repository joins a named project.
    default = cfg.get("same_repo_scope", True)
    ra, rb = git_root(a.get("cwd") or ""), git_root(b.get("cwd") or "")
    ca, cb = os.path.realpath(a.get("cwd") or "a"), os.path.realpath(b.get("cwd") or "b")
    if default and ra and ra == rb:
        return "repo:" + os.path.basename(ra), "same git repository"
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


def join(name: str, cwd: str):
    """Add cwd's project folder to project `name`. Returns (scope, added)."""
    if not PROJECT_NAME_RE.match(name or ""):
        raise ValueError("project names are letters, digits, '.', '_' and '-' (at most 64)")
    root = project_root(cwd)
    raw = _raw()
    scopes = raw.setdefault("scopes", [])
    scope = next((s for s in scopes if s.get("id") == name), None)
    if scope is None:
        scope = {"id": name, "members": []}
        scopes.append(scope)
    elif scope.get("members") and not any(m.get("root") for m in scope["members"]):
        # A hand-written scope with the same id: joining would quietly widen it.
        raise ValueError("scope %r is written by hand in config.json; edit it there" % name)
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
