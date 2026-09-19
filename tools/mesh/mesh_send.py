#!/usr/bin/env python3
"""S8 prototype: send a message only over an established trust pair.

  mesh_send.py --as SEL_A --to SEL_B --text TEXT [--mode bypass|prompting]
               [--msg-id ID] [--forge-mac] [--pull-from HOST:PATH]

--pull-from is for a remote sender: the text is read over SSH from HOST, after
checking that HOST's current host key matches the fingerprint pinned in the
pair. Delivery then happens locally (ADR-0007 option D, pull variant).
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(__file__))
import common as c  # noqa: E402
import mesh_trust  # noqa: E402


def result(**kw):
    print(json.dumps(kw))
    return 0 if kw.get("result") == "sent" else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="sender", required=True)
    ap.add_argument("--to", dest="target", required=True)
    ap.add_argument("--text", default="")
    ap.add_argument("--mode", choices=["bypass", "prompting"])
    ap.add_argument("--msg-id")
    ap.add_argument("--forge-mac", action="store_true")
    ap.add_argument("--pull-from")
    args = ap.parse_args()

    a, b = c.identity(args.sender), c.identity(args.target)
    if not a or not b:
        return result(result="refused", reason="unknown session")
    pid_ = c.pair_id(a, b)
    rec = c.read_json(c.pair_path(pid_))
    if not rec or rec["status"] == "revoked" or rec["expires"] < time.time():
        rc = mesh_trust.establish(args.sender, args.target)
        rec = c.read_json(c.pair_path(pid_))
        if rc != 0 or not rec:
            return result(result="refused", reason="handshake refused")
    if rec["status"] != "active":
        return result(result="not-sent", reason=f"pair {pid_} is {rec['status']} (needs human approval)")

    text = args.text
    if args.pull_from:
        host, _, remote_path = args.pull_from.partition(":")
        current = c.ssh_fingerprint(host)
        if current != rec["host_pins"].get(host):
            return result(result="refused", reason="peer_changed",
                          pinned=rec["host_pins"].get(host), current=current)
        text = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"cat {remote_path}"],
                              capture_output=True, text=True, timeout=20).stdout.strip()

    msg_id = args.msg_id or str(uuid.uuid4())
    mode = args.mode or c.session_mode(a) or "prompting"
    mac = "0" * 32 if args.forge_mac else c.mac(rec["token"], pid_, msg_id, text)
    kind = a["runtime"]
    name = a.get("name") or a["session_id"][:8]
    if a.get("host", "local") != "local":
        name = f"{name}@{a['host']}"
    header = f'[agent-mesh v1 pair={pid_} from="{name}" kind={kind} mode={mode} id={msg_id} mac={mac}]'
    payload = f"{header} {text}"

    if b["runtime"] == "claude":
        # Native envelope: honest from-mode, no from address (replies go through mesh).
        content = (f'<cross-session-message from-name="{name}" from-mode="{mode}">\n'
                   f"{payload}\n</cross-session-message>")
        frame = {"type": "user", "message": {"role": "user", "content": content},
                 "priority": "next", "msg_id": str(uuid.uuid4())}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(b["address"])
            s.sendall((json.dumps(frame) + "\n").encode())
    elif b["runtime"] == "codex":
        env = dict(os.environ, CODEX_HOME=b["codex_home"])
        out = subprocess.run(["/opt/homebrew/bin/codex", "queue", "--thread", b["session_id"],
                              "--message", payload], env=env, capture_output=True, text=True)
        if out.returncode != 0:
            return result(result="error", stderr=out.stderr.strip()[-300:])
    else:
        return result(result="refused", reason="unsupported target runtime")
    return result(result="sent", pair=pid_, msg_id=msg_id, mode=mode, to=b["runtime"])


if __name__ == "__main__":
    sys.exit(main())
