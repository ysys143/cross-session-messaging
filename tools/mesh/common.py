"""Shared pieces of the S8 trust-handshake prototype (experiment code, not a product).

State lives under $MESH_HOME (default /tmp/xsm-spike/mesh):
  policy.toml        user-written scopes (never edited by agents)
  trust/<pair>.json  pair records (0600)
  sessions/*.json    supplementary session records written by the receive hook
  held/*.json        messages the receive hook blocked
  decisions.jsonl    every receive-hook decision
  seen/<pair>        message ids already accepted (replay guard)
"""
import fnmatch
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
import tomllib

HOME = os.path.expanduser("~")
MESH_HOME = os.environ.get("MESH_HOME", "/tmp/xsm-spike/mesh")
HEADER_RE = re.compile(
    r'^\[agent-mesh v1 pair=(?P<pair>[0-9a-f]+) from="(?P<frm>[^"]+)" kind=(?P<kind>claude|codex|remote) '
    r'mode=(?P<mode>bypass|prompting) id=(?P<id>[0-9a-f-]+) mac=(?P<mac>[0-9a-f]+)\] ?(?P<body>.*)$',
    re.S)
ENVELOPE_RE = re.compile(r'^<cross-session-message[^>]*>\n(?P<body>.*)\n</cross-session-message>$', re.S)


def path(*parts):
    return os.path.join(MESH_HOME, *parts)


def write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def read_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def log_decision(entry):
    os.makedirs(MESH_HOME, exist_ok=True)
    with open(path("decisions.jsonl"), "a") as f:
        f.write(json.dumps({"t": time.time(), **entry}) + "\n")


# ---------- policy ----------

def load_policy():
    p = path("policy.toml")
    raw = open(p, "rb").read()
    return tomllib.loads(raw.decode()), hashlib.sha256(raw).hexdigest()[:16]


def _norm_pattern(pattern):
    pattern = os.path.expanduser(pattern)
    head, sep, tail = pattern.partition("*")
    fixed = head.rstrip("/")
    return os.path.realpath(fixed) + ("/" if head.endswith("/") else "") + sep + tail


def member_matches(member, ident):
    if member.get("runtime") and member["runtime"] != ident["runtime"]:
        return False
    if member.get("home") and member["home"] != ident["alias"]:
        return False
    if member.get("host") and member["host"] != ident.get("host", "local"):
        return False
    if member.get("cwd"):
        cwd = os.path.realpath(ident["cwd"]) if ident.get("host", "local") == "local" else ident["cwd"]
        pat = _norm_pattern(member["cwd"]) if ident.get("host", "local") == "local" else member["cwd"]
        if not (fnmatch.fnmatch(cwd, pat) or cwd == pat.rstrip("/*")):
            return False
    return True


def scope_for(policy, a, b):
    """Return the first scope both identities belong to, or None."""
    for scope in policy.get("scope", []):
        members = scope.get("members", [])
        if any(member_matches(m, a) for m in members) and any(member_matches(m, b) for m in members):
            return scope
    return None


# ---------- session identity ----------

def lstart(pid):
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    return out or None


def alive(ident):
    if ident.get("host", "local") != "local":
        return True  # remote liveness is the transport's job (ssh)
    now = lstart(ident["pid"])
    return now is not None and now == ident.get("lstart")


def claude_identity(session_id=None, cwd=None):
    for home in sorted(glob.glob(os.path.join(HOME, ".claude*"))):
        for p in glob.glob(os.path.join(home, "sessions", "*.json")):
            rec = read_json(p)
            if not rec:
                continue
            if (session_id and rec.get("sessionId") == session_id) or \
               (cwd and os.path.realpath(rec.get("cwd", "")) == os.path.realpath(cwd)):
                return {"runtime": "claude", "home": home, "alias": os.path.basename(home).lstrip("."),
                        "session_id": rec["sessionId"], "pid": rec["pid"], "lstart": lstart(rec["pid"]),
                        "cwd": rec.get("cwd"), "name": rec.get("name"),
                        "address": rec.get("messagingSocketPath"), "host": "local"}
    return None


def codex_identity(thread_id=None, cwd=None):
    for p in glob.glob(path("sessions", "codex-*.json")):
        rec = read_json(p)
        if rec and ((thread_id and rec["session_id"] == thread_id) or
                    (cwd and os.path.realpath(rec["cwd"]) == os.path.realpath(cwd))):
            return rec
    return None


def identity(selector):
    """selector: claude:<session_id|cwd> | codex:<thread_id|cwd> | remote:<host>:<name>"""
    kind, _, rest = selector.partition(":")
    if kind == "claude":
        return claude_identity(session_id=rest) or claude_identity(cwd=rest)
    if kind == "codex":
        return codex_identity(thread_id=rest) or codex_identity(cwd=rest)
    if kind == "remote":
        host, _, name = rest.partition(":")
        return {"runtime": "remote", "host": host, "alias": host, "session_id": f"{host}:{name}",
                "name": name, "cwd": "", "pid": None, "lstart": None}
    raise SystemExit(f"bad selector {selector}")


def session_mode(ident):
    rec = read_json(path("sessions", f"{ident['runtime']}-{ident['session_id']}.json")) or {}
    return "bypass" if rec.get("permission_mode") == "bypassPermissions" else \
        ("prompting" if rec.get("permission_mode") else None)


# ---------- pairs ----------

def pair_id(a, b):
    keys = sorted(f"{x['runtime']}:{x['alias']}:{x['session_id']}" for x in (a, b))
    return hashlib.sha256("|".join(keys).encode()).hexdigest()[:12]


def pair_path(pid_):
    return path("trust", f"{pid_}.json")


def mac(token, pid_, msg_id, body):
    return hmac.new(bytes.fromhex(token), f"{pid_}|{msg_id}|{body}".encode(), "sha256").hexdigest()[:32]


def new_token():
    return secrets.token_hex(16)


def side_matches(side, ident):
    return (side["runtime"] == ident["runtime"] and side["session_id"] == ident["session_id"]
            and side.get("pid") == ident.get("pid") and side.get("lstart") == ident.get("lstart"))


def ssh_fingerprint(host):
    """Current ed25519 host key fingerprint as seen by ssh-keyscan (hostname resolved via ssh -G)."""
    cfg = subprocess.run(["ssh", "-G", host], capture_output=True, text=True).stdout
    hostname = next((l.split()[1] for l in cfg.splitlines() if l.startswith("hostname ")), host)
    scan = subprocess.run(["ssh-keyscan", "-t", "ed25519", hostname],
                          capture_output=True, text=True, timeout=10).stdout
    if not scan.strip():
        return None
    fp = subprocess.run(["ssh-keygen", "-lf", "-"], input=scan, capture_output=True, text=True).stdout
    return fp.split()[1] if fp else None
