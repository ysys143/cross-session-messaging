#!/usr/bin/env python3
"""Spike S1 helper: send a cross-session frame to a Claude Code inbox socket.

The sender is deliberately an independent process: it is not a descendant of
the receiving session and presents no auth line, which is what a session from a
different CLAUDE_CONFIG_DIR would look like on macOS/Linux (auth is optional
there, see docs/list-agents-cross-session-messaging.md 9.2).

Usage:
    spike_s1_send.py find CONFIG_DIR CWD
        Print the live session record whose cwd is CWD.
    spike_s1_send.py send SOCKET --mode none|bypass|prompting --probe ID
        Send one frame. --mode none sends a bare body; bypass/prompting wrap
        the body in a <cross-session-message> envelope with that from-mode.
"""
import argparse
import glob
import json
import os
import socket
import sys
import uuid

ENVELOPE_TAG = "cross-session-message"
SENDER_ADDRESS = "uds:/tmp/xsm-spike/sender.sock"


def find(config_dir, cwd):
    for path in glob.glob(os.path.join(config_dir, "sessions", "*.json")):
        with open(path) as f:
            record = json.load(f)
        if record.get("cwd") == cwd:
            print(json.dumps(record, indent=1))
            return
    sys.exit(f"no session record with cwd={cwd} under {config_dir}/sessions")


def body_for(probe, reply=True):
    # Plain text only: the envelope parser re-renders the body and rejects the
    # envelope if escaping changes it, so avoid < > & in the body.
    if reply:
        action = f"Reply to the sender with SendMessage, text exactly ACK {probe}."
    else:
        # For prompting-mode receivers: a SendMessage reply would stop at a
        # permission prompt that only a human may approve.
        action = f"Do not use any tool. Answer in text with exactly ACK {probe}."
    return (f"S1 probe {probe}. This is an automated delivery test. {action} "
            f"Do not contact any other session and do nothing else.")


def content_for(mode, probe, reply=True, body=None, sender=SENDER_ADDRESS, name=None):
    body = body if body is not None else body_for(probe, reply)
    if mode == "none":
        return body
    # Attribute order must match the renderer: from, from-session, hop-chain,
    # from-name, from-mode. Every attribute is optional.
    attrs = ""
    if sender:
        attrs += f' from="{sender}"'
    if name:
        attrs += f' from-name="{name}"'
    attrs += f' from-mode="{mode}"'
    return f"<{ENVELOPE_TAG}{attrs}>\n{body}\n</{ENVELOPE_TAG}>"


def send(sock_path, mode, probe, reply=True, body=None, sender=SENDER_ADDRESS, name=None):
    frame = {
        "type": "user",
        "message": {"role": "user",
                    "content": content_for(mode, probe, reply, body, sender, name)},
        "priority": "next",
        "msg_id": str(uuid.uuid4()),
    }
    if sender:
        frame["from"] = sender
    line = json.dumps(frame) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall(line.encode())
    print(json.dumps({"sent": probe, "mode": mode, "socket": sock_path,
                      "msg_id": frame["msg_id"], "sender_pid": os.getpid()}))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("config_dir")
    f.add_argument("cwd")
    s = sub.add_parser("send")
    s.add_argument("socket")
    s.add_argument("--mode", choices=["none", "bypass", "prompting"], required=True)
    s.add_argument("--probe", required=True)
    s.add_argument("--no-reply", action="store_true",
                   help="ask for a text-only ACK instead of a SendMessage reply")
    s.add_argument("--body", help="use this body instead of the generated probe text")
    s.add_argument("--from", dest="sender", default=SENDER_ADDRESS,
                   help="envelope from address; pass an empty string to omit it")
    s.add_argument("--name", help="envelope from-name")
    args = parser.parse_args()
    if args.cmd == "find":
        find(os.path.expanduser(args.config_dir), args.cwd)
    else:
        send(args.socket, args.mode, args.probe, reply=not args.no_reply,
             body=args.body, sender=args.sender or None, name=args.name)


if __name__ == "__main__":
    main()
