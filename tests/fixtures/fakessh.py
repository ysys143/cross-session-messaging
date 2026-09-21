#!/usr/bin/env python3
"""A stand-in for ssh that makes one box look like several machines.

Each host is a directory with its own XSM_HOME and authorized_keys. With
`-i <key>` it behaves like sshd with a forced command: it finds the
authorized_keys line for that key on the target host and runs that line's
command, as sshd would. Without a key it runs the given command on the host,
the way the user's own SSH access would."""
import json, os, shlex, subprocess, sys

args = sys.argv[1:]
key, host, rest = None, None, []
i = 0
while i < len(args):
    a = args[i]
    if a == "-i":
        key = args[i + 1]; i += 2; continue
    if a == "-o":
        i += 2; continue
    host = a
    rest = args[i + 1:]
    break
machines = json.loads(os.environ["FAKE_MACHINES"])
m = machines[host]
env = dict(os.environ, XSM_HOME=m["home"], XSM_AUTHORIZED_KEYS=m["ak"], XSM_HOSTNAME=m["name"],
           SSH_CONNECTION="10.0.0.1 1 10.0.0.2 22")
if key:
    pub = open(key + ".pub").read().split()[1]
    line = next((l for l in open(m["ak"]).read().splitlines() if pub in l), None)
    if not line:
        sys.stderr.write("Permission denied (publickey).\n"); sys.exit(255)
    command = line.split('command="', 1)[1].split('"', 1)[0]
    sys.exit(subprocess.run(command, shell=True, env=env).returncode)
sys.exit(subprocess.run(" ".join(rest), shell=True, env=env).returncode)
