#!/usr/bin/env python3
"""Spike S7 prototype: resolve a session name across homes and runtimes.

Implements the ADR-0008 option B rules (proposal, not a decision) over the
native stores, read-only. Nothing is sent.

Sources:
  Claude: <CONFIG_DIR>/sessions/<pid>.json for every ~/.claude* directory.
          Liveness = pid alive + inbox socket connect (as Claude does).
  Codex:  $CODEX_HOME/state_5.sqlite threads for every ~/.codex* directory.
          Liveness is unknown here (no live-session registry yet), so Codex
          threads count as "offline" candidates.

Rules:
  - "name@alias" is qualified only when alias is a known home alias
    (the home directory name without the leading dot, e.g. claude-4).
  - Names are compared with Claude's normalization (NFKC, strip control
    characters, trim, lowercase, whitespace runs -> "-").
  - A bare name resolves only when exactly one live candidate matches.
    Offline candidates are considered only when qualified or with
    --include-offline.
  - ref = sha256("runtime:home:id")[:6], independent of the name.

Usage:
    mesh_resolve.py NAME [--include-offline]
"""
import argparse
import glob
import hashlib
import json
import os
import re
import socket
import sqlite3
import unicodedata

HOME = os.path.expanduser("~")


def normalize(name):
    name = unicodedata.normalize("NFKC", name)
    name = "".join(c for c in name
                   if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf"))
    return re.sub(r"\s+", "-", name.strip().lower())


def alias_of(home_dir):
    return os.path.basename(home_dir).lstrip(".")


def ref_of(runtime, home_dir, session_id):
    return hashlib.sha256(f"{runtime}:{home_dir}:{session_id}".encode()).hexdigest()[:6]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def socket_live(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def claude_candidates():
    for home_dir in sorted(glob.glob(os.path.join(HOME, ".claude*"))):
        for path in glob.glob(os.path.join(home_dir, "sessions", "*.json")):
            try:
                rec = json.load(open(path))
            except (OSError, ValueError):
                continue
            if not rec.get("name"):
                continue
            sock = rec.get("messagingSocketPath") or ""
            live = pid_alive(rec["pid"]) and bool(sock) and socket_live(sock)
            yield {"runtime": "claude", "home": home_dir, "alias": alias_of(home_dir),
                   "id": rec.get("sessionId"), "name": rec["name"],
                   "address": f"uds:{sock}", "state": "live" if live else "stale",
                   "ref": ref_of("claude", home_dir, rec.get("sessionId"))}


def codex_candidates():
    for home_dir in sorted(glob.glob(os.path.join(HOME, ".codex*"))):
        db = os.path.join(home_dir, "state_5.sqlite")
        if not os.path.exists(db):
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("select id, name from threads "
                               "where archived = 0 and name is not null and name != ''").fetchall()
        finally:
            con.close()
        for thread_id, name in rows:
            yield {"runtime": "codex", "home": home_dir, "alias": alias_of(home_dir),
                   "id": thread_id, "name": name,
                   "address": f"codex-queue:{home_dir}#{thread_id}", "state": "offline",
                   "ref": ref_of("codex", home_dir, thread_id)}


def resolve(target, include_offline=False):
    candidates = list(claude_candidates()) + list(codex_candidates())
    aliases = {c["alias"] for c in candidates}
    qualifier = None
    base = target
    if "@" in target:
        head, tail = target.rsplit("@", 1)
        if tail in aliases:
            base, qualifier = head, tail
    want = normalize(base)
    matches = [c for c in candidates
               if normalize(c["name"]) == want and (qualifier is None or c["alias"] == qualifier)]
    usable = [c for c in matches
              if c["state"] == "live" or qualifier is not None or include_offline]
    if len(usable) == 1:
        decision = {"result": "resolved", "target": usable[0]}
    elif not usable:
        decision = {"result": "not-found"}
    else:
        decision = {"result": "ambiguous"}
    return {"input": target, "qualifier": qualifier, "normalized": want,
            "matches": matches, **decision}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--include-offline", action="store_true")
    args = parser.parse_args()
    out = resolve(args.name, args.include_offline)
    print(f"input={out['input']} qualifier={out['qualifier']} normalized={out['normalized']}")
    for c in out["matches"]:
        print(f"  {c['name']}@{c['alias']:<9} [{c['ref']}] {c['runtime']:<6} {c['state']:<7} {c['address']}")
    if out["result"] == "resolved":
        t = out["target"]
        print(f"=> resolved: {t['name']}@{t['alias']} [{t['ref']}] -> {t['address']}")
    else:
        print(f"=> {out['result']}")


if __name__ == "__main__":
    main()
