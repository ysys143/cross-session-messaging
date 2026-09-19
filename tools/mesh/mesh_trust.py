#!/usr/bin/env python3
"""S8 prototype: establish, approve, revoke and list trust pairs.

  mesh_trust.py establish SEL_A SEL_B   scope check, then create (auto) or pend (approve)
  mesh_trust.py approve PAIR            human approval through a native macOS dialog
  mesh_trust.py revoke PAIR
  mesh_trust.py list

Selectors: claude:<session_id|cwd>, codex:<thread_id|cwd>, remote:<ssh-host>:<name>
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import common as c  # noqa: E402


def parse_ttl(text):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(text[:-1]) * units[text[-1]]


def side(ident):
    keep = ("runtime", "alias", "session_id", "pid", "lstart", "name", "host", "cwd")
    return {k: ident.get(k) for k in keep}


def establish(sel_a, sel_b):
    policy, phash = c.load_policy()
    a, b = c.identity(sel_a), c.identity(sel_b)
    if not a or not b:
        print(json.dumps({"result": "refused", "reason": "unknown session", "a": bool(a), "b": bool(b)}))
        return 2
    dead = [x["name"] for x in (a, b) if not c.alive(x)]
    if dead:
        print(json.dumps({"result": "refused", "reason": "session not alive", "sessions": dead}))
        return 2
    scope = c.scope_for(policy, a, b)
    if scope is None:
        print(json.dumps({"result": "refused", "reason": "not in a common scope"}))
        return 2
    remote = [x for x in (a, b) if x.get("host", "local") != "local"]
    mode = scope.get("trust", "approve")
    pins = {}
    if remote:
        if scope.get("remote", "deny") == "deny":
            print(json.dumps({"result": "refused", "reason": "remote peers denied by scope"}))
            return 2
        mode = "approve"  # remote pairs always need a human
        for r in remote:
            fp = c.ssh_fingerprint(r["host"])
            if not fp:
                print(json.dumps({"result": "refused", "reason": f"no host key for {r['host']}"}))
                return 2
            pins[r["host"]] = fp
    pid_ = c.pair_id(a, b)
    existing = c.read_json(c.pair_path(pid_))
    if existing and existing["status"] == "revoked":
        # Revocation is sticky: auto mode must not silently undo it (S8-h finding).
        # Only `approve` (a human, through the dialog) can re-activate the pair.
        print(json.dumps({"result": "refused", "reason": "pair was revoked; needs human approval",
                          "pair": pid_}))
        return 2
    if existing and existing["status"] == "active" and existing["expires"] > time.time() \
            and all(any(c.side_matches(s, x) for s in (existing["a"], existing["b"])) for x in (a, b)):
        print(json.dumps({"result": "exists", "pair": pid_}))
        return 0
    rec = {"pair": pid_, "scope": scope["id"], "status": "active" if mode == "auto" else "pending",
           "mode": mode, "a": side(a), "b": side(b), "token": c.new_token(),
           "created": time.time(), "expires": time.time() + parse_ttl(scope.get("ttl", "8h")),
           "policy_hash": phash, "host_pins": pins, "approved_by": None}
    c.write_json(c.pair_path(pid_), rec)
    print(json.dumps({"result": rec["status"], "pair": pid_, "scope": scope["id"], "host_pins": pins}))
    return 0


def approve(pid_):
    rec = c.read_json(c.pair_path(pid_))
    if not rec:
        print("no such pair")
        return 2
    msg = (f"agent-mesh: allow these two sessions to message each other?\\n\\n"
           f"{rec['a']['runtime']} {rec['a']['alias']} {rec['a']['name']}\\n"
           f"{rec['b']['runtime']} {rec['b']['alias']} {rec['b']['name']}\\n\\n"
           f"scope: {rec['scope']}  pair: {pid_}")
    script = (f'display dialog "{msg}" with title "agent-mesh trust" '
              f'buttons {{"Deny", "Approve"}} default button "Deny" giving up after 120')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    approved = "button returned:Approve" in out.stdout
    rec["status"] = "active" if approved else rec["status"]
    rec["approved_by"] = "macos-dialog" if approved else None
    rec["approval_result"] = out.stdout.strip() or out.stderr.strip()
    c.write_json(c.pair_path(pid_), rec)
    print(json.dumps({"pair": pid_, "approved": approved, "dialog": rec["approval_result"]}))
    return 0 if approved else 1


def revoke(pid_):
    rec = c.read_json(c.pair_path(pid_))
    if not rec:
        print("no such pair")
        return 2
    rec["status"] = "revoked"
    rec["revoked_at"] = time.time()
    c.write_json(c.pair_path(pid_), rec)
    print(json.dumps({"pair": pid_, "status": "revoked"}))
    return 0


def list_pairs():
    import glob
    for p in sorted(glob.glob(c.path("trust", "*.json"))):
        r = c.read_json(p)
        if not r:
            continue
        left = int(r["expires"] - time.time())
        print(f"{r['pair']} {r['status']:<8} scope={r['scope']} mode={r['mode']} ttl_left={left}s "
              f"{r['a']['runtime']}:{r['a']['name']} <-> {r['b']['runtime']}:{r['b']['name']} pins={r['host_pins']}")
    return 0


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    return {"establish": lambda: establish(*args), "approve": lambda: approve(*args),
            "revoke": lambda: revoke(*args), "list": list_pairs}[cmd]()


if __name__ == "__main__":
    sys.exit(main())
