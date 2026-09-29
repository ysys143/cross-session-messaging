"""Sessions on other machines, over two-way SSH (ADR-0007).

A pairing joins a project here with a project on another machine. Each side
holds a dedicated key (`~/.xsm/remote/id_ed25519`) that the other side's
`authorized_keys` accepts for one thing only: running `xsm-remote.py <peer>`.
The peer label is written into that line at pairing time, so the receiver
knows who is calling from the key itself, never from anything the message
says. Trust is per host and key, not per session (the SSH key owner already
has a shell over there).

Sending runs the forced command over SSH with a JSON request on stdin; the
receiving xsm checks the pairing and the target's project, delivers through
its own local path, waits briefly for the receipt and answers. Replies go the
other way with the other machine's key: both directions must reach
(user decision, 2026-09-22). Nothing here listens; SSH does.

The envelope a remote message carries has no `uds:` reply address (S5: a
remote socket path is misdelivered on the receiving machine) and an `origin`
the receiver fills in. The gate accepts it only if this machine's own
receiver recorded that message id for that peer.
"""
from __future__ import annotations

import json
import math
import os
import shlex
import socket
import subprocess
import sys
import time
from contextlib import nullcontext

from . import config, envelope, identity, install, ledger, paths, registry

REMOTE = "remote"
MARK = "xsm-remote"            # comment tag on authorized_keys lines xsm wrote


class RemoteError(Exception):
    """A call that did not come back with an answer. Whether the request
    reached the peer is unknown: it may have run there and lost its reply."""


class RemoteUnreached(RemoteError):
    """A call that certainly never reached the peer's xsm: no pairing, no ssh
    to run, or ssh gave up before logging in. Nothing ran over there."""


# ssh's own messages for failures before the forced command could start. ssh
# exits 255 for these, but also for a connection that drops mid-session, so
# the exit code alone cannot say the request never ran (issue #4, 2026-09-29).
_BEFORE_LOGIN = ("Could not resolve hostname", "connect to host", "Connection refused",
                 "No route to host", "Network is unreachable", "Permission denied (",
                 "Host key verification failed", "kex_exchange_identification",
                 "during banner exchange")


# --- keys and authorized_keys ----------------------------------------------------------

def key_path() -> str:
    return paths.path(REMOTE, "id_ed25519")


def ensure_key() -> str:
    """This machine's xsm key; returns the public key line."""
    os.makedirs(paths.path(REMOTE), mode=0o700, exist_ok=True)
    if not os.path.exists(key_path()):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                        "%s@%s" % (MARK, this_host()), "-f", key_path()], check=True)
    with open(key_path() + ".pub", encoding="utf-8") as fh:
        return fh.read().strip()


def this_host() -> str:
    return os.environ.get("XSM_HOSTNAME") or socket.gethostname().split(".")[0]


def authorized_keys_path() -> str:
    return os.environ.get("XSM_AUTHORIZED_KEYS") or os.path.expanduser("~/.ssh/authorized_keys")


def install_receiver() -> str:
    """Copy the receiver out of the repository, under ~/.xsm, and return its
    entry. On macOS a process started by sshd may not read ~/Documents and the
    like (privacy protection, measured: "Operation not permitted"), and a
    repository usually lives there."""
    import shutil
    dest = paths.path(REMOTE, "pkg")
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(os.path.join(install.REPO, "xsm"), os.path.join(tmp, "xsm"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(tmp, "hooks"))
    shutil.copy2(os.path.join(install.REPO, "hooks", "xsm-remote.py"),
                 os.path.join(tmp, "hooks", "xsm-remote.py"))
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return os.path.join(dest, "hooks", "xsm-remote.py")


def forced_line(peer: str, pubkey: str) -> str:
    entry = os.path.join(paths.path(REMOTE, "pkg"), "hooks", "xsm-remote.py")
    if not os.path.exists(entry):
        entry = install_receiver()
    command = "%s %s %s" % (install.pinned_python(), entry, peer)
    if os.environ.get("XSM_HOME") and not install._is_default_home(os.environ["XSM_HOME"]):
        command = "XSM_HOME=%s %s" % (os.environ["XSM_HOME"], command)
    opts = 'command="%s",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding' % command
    keytype, keydata = pubkey.split()[:2]
    return "%s %s %s %s:%s" % (opts, keytype, keydata, MARK, peer)


def install_peer_key(peer: str, pubkey: str) -> None:
    install_receiver()                    # the forced command runs this copy
    path = authorized_keys_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    lines = [l for l in lines if not l.endswith("%s:%s" % (MARK, peer))]
    lines.append(forced_line(peer, pubkey))
    with open(path + ".xsm-tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path + ".xsm-tmp", 0o600)
    os.replace(path + ".xsm-tmp", path)


def remove_peer_key(peer: str) -> bool:
    path = authorized_keys_path()
    if not os.path.exists(path):
        return False
    lines = open(path, encoding="utf-8").read().splitlines()
    kept = [l for l in lines if not l.endswith("%s:%s" % (MARK, peer))]
    if len(kept) == len(lines):
        return False
    with open(path + ".xsm-tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + ("\n" if kept else ""))
    os.chmod(path + ".xsm-tmp", 0o600)
    os.replace(path + ".xsm-tmp", path)
    return True


# --- pairings ------------------------------------------------------------------------

def pairings() -> list:
    return list(config.load().get("remotes") or [])


def pairing_for(peer: str) -> dict | None:
    return next((p for p in pairings() if p.get("peer") == peer), None)


def _save_pairing(entry: dict) -> None:
    raw = config._raw()
    rows = [p for p in raw.get("remotes") or [] if p.get("peer") != entry["peer"]]
    rows.append(entry)
    raw["remotes"] = rows
    config._save(raw)


def _drop_pairing(peer: str) -> bool:
    raw = config._raw()
    rows = raw.get("remotes") or []
    kept = [p for p in rows if p.get("peer") != peer]
    raw["remotes"] = kept
    config._save(raw)
    return len(kept) != len(rows)


def _resolved(host: str) -> list:
    """The host's real address from the user's ssh config, as options that
    survive `-F /dev/null`."""
    ssh = os.environ.get("XSM_SSH") or "ssh"
    try:
        out = subprocess.run([ssh, "-G", host], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    opts = []
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in ("hostname", "user", "port", "proxyjump", "proxycommand") and value \
                and value != "none":
            opts += ["-o", "%s=%s" % (key, value)]
    return opts


def ssh_argv(host: str, use_xsm_key: bool = True) -> list:
    argv = [os.environ.get("XSM_SSH") or "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if use_xsm_key:
        # Offer the xsm key and nothing else. A host entry's IdentityFile or a
        # key agent would otherwise log in with the user's own key, and the
        # far side would open a shell instead of the xsm forced command
        # (measured against a real ~/.ssh/config).
        argv = argv[:1] + ["-F", "/dev/null"] + _resolved(host) + argv[1:] + [
            "-T", "-i", key_path(), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=accept-new"]
    return argv + [host]


def call(peer: str, request: dict, timeout: float = 60) -> dict:
    """Run the peer's forced command with one JSON request; returns its reply."""
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    span_cm = telemetry.span("xsm.remote.ssh", {"xsm.remote.peer": peer,
                                                "xsm.remote.op": request.get("op")},
                             kind="CLIENT") if telemetry else nullcontext()
    start = time.time()
    try:
        with span_cm as span:
            if span is not None:
                # Stamped here rather than by the caller: the far side should
                # hang off the hop that actually reached it, not off whatever
                # assembled the request earlier.
                request = dict(request, traceparent=span.traceparent())
            return _call(peer, request, timeout)
    finally:
        if telemetry:
            # Every hop, not just the ones that answered: a round trip that
            # timed out is the one worth seeing on a latency chart.
            telemetry.histogram("xsm.remote.ssh.duration", time.time() - start,
                                {"xsm.remote.peer": peer})


def _call(peer: str, request: dict, timeout: float) -> dict:
    pairing = pairing_for(peer)
    if not pairing:
        raise RemoteUnreached("%s is not a paired remote; pair it first: xsm remote add %s"
                              % (peer, peer))
    try:
        out = subprocess.run(ssh_argv(pairing["host"]), input=json.dumps(request),
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("%s did not answer within %ds" % (peer, timeout))
    except OSError as exc:
        raise RemoteUnreached("cannot run ssh: %s" % exc)
    if out.returncode != 0:
        why = "ssh to %s failed: %s" % (peer, (out.stderr or out.stdout).strip()[:300])
        if out.returncode == 255 and not out.stdout.strip() \
                and any(p in (out.stderr or "") for p in _BEFORE_LOGIN):
            raise RemoteUnreached(why)
        raise RemoteError(why)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RemoteError("%s answered something xsm cannot read: %s" % (peer, out.stdout[:200]))


def add(host: str, local_project: str, remote_project: str | None = None,
        reach_me_as: str | None = None, remote_xsm: str | None = None,
        here: str | None = None) -> dict:
    """Pair this machine with `host` for one project on each side, both ways.
    Uses the user's own SSH access to `host` once, to exchange xsm keys."""
    remote_project = remote_project or local_project
    _require_named(local_project, os.path.realpath(here or os.getcwd()))
    me_as = reach_me_as or this_host()
    pub = ensure_key()
    accept = ("%s remote accept --peer %s --reach-as %s --project %s --remote-project %s --key %s" % (
        remote_xsm or os.path.join(install.REPO, "bin", "xsm"), shlex.quote(this_host()),
        shlex.quote(me_as),
        shlex.quote(remote_project), shlex.quote(local_project), shlex.quote(pub)))
    out = subprocess.run(ssh_argv(host, use_xsm_key=False) + [accept], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise RemoteError("could not pair with %s: %s" % (host, (out.stderr or out.stdout).strip()[:300]))
    try:
        answer = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RemoteError("%s answered something xsm cannot read: %s" % (host, out.stdout[:200]))
    peer = answer["peer"]
    install_peer_key(peer, answer["key"])
    entry = {"peer": peer, "host": host, "local_project": local_project,
             "remote_project": remote_project, "added": time.time()}
    _save_pairing(entry)
    # Both directions must reach (ADR-0007): ask the other side to call back.
    try:
        back = call(peer, {"op": "ping-back"})
    except RemoteError as exc:
        back = {"ok": False, "error": str(exc)}
    if not back.get("ok"):
        _drop_pairing(peer)
        remove_peer_key(peer)
        raise RemoteError("%s cannot reach this machine as %r over SSH (%s); two-way SSH is "
                          "required" % (peer, me_as, back.get("error")))
    return entry


def accept(peer: str, host: str, project: str, remote_project: str, key: str) -> dict:
    """The far side of `add`, run over the user's SSH: trust the caller's key for
    xsm only, record the pairing, and hand back this machine's key."""
    pub = ensure_key()
    install_peer_key(peer, key)
    _save_pairing({"peer": peer, "host": host, "local_project": project,
                   "remote_project": remote_project, "added": time.time()})
    return {"ok": True, "peer": this_host(), "key": pub}


def remove(peer: str) -> dict:
    pairing = pairing_for(peer)
    told = False
    if pairing:
        try:
            told = bool(call(peer, {"op": "unpair"}).get("ok"))
        except RemoteError:
            pass
    return {"pairing": _drop_pairing(peer), "key": remove_peer_key(peer), "told_peer": told}


# --- the receiving side (forced command) ---------------------------------------------

def _project_members(project: str, here: str) -> bool:
    """Whether a folder is in a named project, by the recorded paths alone.
    The receiver may run where it cannot touch the folder itself (see
    install_receiver), so this reads no file system."""
    for scope in config.projects():
        if scope.get("id") != project:
            continue
        for m in scope.get("members", []):
            root = (m.get("root") or "").rstrip("/")
            if root and (here == root or here.startswith(root + "/")):
                return True
    return False


def _require_named(project: str, here: str) -> None:
    if not _project_members(project, here):
        raise RemoteError("%s is not a named project this folder joined; pair a project joined "
                          "with `xsm join` on both machines" % project)


def serve(peer: str, request: dict) -> dict:
    """What `xsm-remote.py <peer>` does for one request. `peer` comes from the
    authorized_keys line, so it is who holds the key."""
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    # A forced command is a fresh process every time, so the request's own
    # traceparent is the only thing tying this side to the caller's trace.
    span_cm = telemetry.span("xsm.remote.serve", {"xsm.remote.peer": peer,
                                                  "xsm.remote.op": request.get("op")},
                             kind="SERVER", traceparent=request.get("traceparent")) \
        if telemetry else nullcontext()
    with span_cm as span:
        reply = _serve(peer, request, span)
        if span is not None:
            span.set_attribute("xsm.result.ok", bool(reply.get("ok")))
            if reply.get("status"):
                span.set_attribute("xsm.result.status", reply["status"])
            if not reply.get("ok"):
                span.set_status("ERROR", str(reply.get("error") or ""))
        return reply


def _serve(peer: str, request: dict, span=None) -> dict:
    pairing = pairing_for(peer)
    op = request.get("op")
    if op == "unpair":
        _drop_pairing(peer)
        remove_peer_key(peer)
        return {"ok": True}
    if not pairing:
        return {"ok": False, "error": "no pairing with %s here" % peer}
    if op == "ping":
        return {"ok": True, "peer": this_host()}
    if op == "ping-back":
        try:
            return {"ok": bool(call(peer, {"op": "ping"}).get("ok"))}
        except RemoteError as exc:
            return {"ok": False, "error": str(exc)}
    if op == "status":
        return _inbound_status(peer, str(request.get("id") or ""), request.get("wait"))
    project = pairing["local_project"]
    if op == "sessions":
        rows = [r for r in registry.records() if r.get("state") == "live"
                and _project_members(project, r.get("cwd") or "/")]
        return {"ok": True, "sessions": [{"name": r.get("name"), "alias": r.get("alias"),
                                          "ref": r.get("ref"), "runtime": r.get("runtime")}
                                         for r in rows]}
    if op != "send":
        return {"ok": False, "error": "unknown op %r" % op}
    if request.get("project") != pairing["remote_project"]:
        return {"ok": False, "status": "refused",
                "error": "this pairing is for project %s, not %s"
                         % (pairing["remote_project"], request.get("project"))}
    from . import resolve
    found = resolve.resolve(request.get("target") or "")
    if not found.ok:
        return {"ok": False, "status": "refused", "error": found.reason}
    target = found.record
    from .send import not_running
    stopped = not_running(target)
    if stopped:
        # Refused here as a local send would be, or the message waits in a
        # queue nobody reads and the peer is told it was queued (2026-09-28).
        return {"ok": False, "status": "refused", "error": stopped}
    if not _project_members(project, target.get("cwd") or "/"):
        return {"ok": False, "status": "refused",
                "error": "%s is not in project %s here" % (target.get("name"), project)}
    if target.get("ref") in config.blocked():
        return {"ok": False, "status": "refused", "error": "the target is blocked here"}
    msg_id = request["id"]
    if ledger.status(msg_id) and not recorded_inbound(msg_id, peer):
        # An id this machine already holds for something else: taking it would
        # overwrite that entry and let `status` answer for it.
        return {"ok": False, "status": "refused", "error": "message id %s is already in use here"
                                                           % msg_id}
    if ledger.received(msg_id):
        # The same message again, after the sender lost our answer. It was
        # handed over once; say how, and do not hand it over twice.
        state = ledger.status(msg_id)
        return {"ok": True, "status": state.get("status"), "duplicate": True,
                "target": {k: (state.get("to") or {}).get(k) for k in ("name", "alias", "ref")}}
    sender = dict(request.get("sender") or {})
    sender.update({"socket": None, "alias": "%s@%s" % (sender.get("alias"), peer)})
    content = envelope.build(request.get("body") or "", msg_id=msg_id, sender=sender,
                             scope="remote:%s" % peer, kind=request.get("kind") or "note",
                             reply_to=request.get("reply_to"), origin=peer,
                             # Strict when sending, lenient when receiving: a peer on a
                             # newer version may name an outcome this one has never heard
                             # of, and that is not a reason to drop its message.
                             outcome=request.get("outcome")
                             if request.get("outcome") in envelope.OUTCOMES else None,
                             traceparent=span.traceparent() if span is not None
                             else request.get("traceparent"))
    # The gate trusts a remote message only if this receiver recorded it.
    paths.write_json(paths.path(REMOTE, "inbound-%s.json" % msg_id),
                     {"id": msg_id, "peer": peer, "t": time.time()})
    ledger.queued(msg_id, sender, target, "remote:%s" % peer, request.get("kind") or "note",
                  request.get("body") or "")
    from . import adapters
    try:
        if target.get("runtime") == "claude":
            adapters.to_claude(target["socket"], content, msg_id, priority="next",
                               reply_address=None)
        else:
            adapters.to_codex(target.get("home"), str(target.get("session_id")), content)
    except adapters.DeliveryError as err:
        # Recorded, so a later `status` from the sender reads error, not queued.
        ledger.failed(msg_id, "%s: %s" % (err.reason, err.detail))
        return {"ok": False, "status": "error", "error": "%s: %s" % (err.reason, err.detail)}
    wait = clamp_wait(request.get("wait"), MAX_SEND_WAIT)
    state = ledger.wait_for(msg_id, wait) if wait else {}
    return {"ok": True, "status": state.get("status") or "queued",
            "target": {"name": target.get("name"), "alias": target.get("alias"),
                       "ref": target.get("ref")}}


MAX_STATUS_WAIT = 30.0
MAX_SEND_WAIT = 300.0


def clamp_wait(value, cap: float) -> float:
    """A wait taken from a peer's JSON (or a --wait flag) into [0, cap]. NaN
    and inf pass float() and `min(nan, 30)` is nan, which never reaches the
    deadline in ledger.wait_for: one such request would hold the forced
    command open forever (review, 2026-09-29)."""
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(seconds) or seconds < 0:
        return 0.0
    return min(seconds, cap)


def recorded_inbound(msg_id: str, peer: str) -> bool:
    rec = paths.read_json(paths.path(REMOTE, "inbound-%s.json" % msg_id)) or {}
    return rec.get("peer") == peer


def _inbound_status(peer: str, msg_id: str, wait=None) -> dict:
    """What became of a message `peer` sent here, for a sender that lost the
    answer to its send. Only for ids this receiver recorded as coming from that
    peer: any other id reads as unknown, so a peer learns nothing about
    messages that are not its own. `status: null` means no record here."""
    if not msg_id or os.path.basename(msg_id) != msg_id or msg_id.startswith(".") \
            or not recorded_inbound(msg_id, peer):
        return {"ok": True, "status": None}
    state = ledger.status(msg_id)
    if state.get("scope") != "remote:%s" % peer:
        return {"ok": True, "status": None}
    wait = clamp_wait(wait, MAX_STATUS_WAIT)
    if wait and state.get("status") == "queued":
        state = ledger.wait_for(msg_id, wait)
    return {"ok": True, "status": state.get("status"),
            "reason": (state.get("receipt") or {}).get("reason") or state.get("error") or "",
            "target": {k: (state.get("to") or {}).get(k) for k in ("name", "alias", "ref")}}


def main(argv=None) -> int:
    """Entry of the forced command: `xsm-remote.py <peer>` with a JSON request on stdin."""
    argv = sys.argv[1:] if argv is None else argv
    peer = argv[0] if argv else ""
    try:
        request = json.loads(sys.stdin.read() or "{}")
        reply = serve(peer, request)
    except Exception as exc:                       # the caller must always get an answer
        reply = {"ok": False, "status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
    print(json.dumps(reply, ensure_ascii=False))
    return 0


# --- the sending side -------------------------------------------------------------------

def split_target(spec: str) -> tuple:
    """`name@alias@peer` or `ref:x@peer` -> (local spec, peer) when peer is paired."""
    if "@" not in (spec or ""):
        return spec, None
    head, _, tail = spec.rpartition("@")
    if pairing_for(tail):
        return head, tail
    return spec, None


def send(sender: dict, spec: str, peer: str, body: str, kind: str, reply_to: str | None,
         wait: float, msg_id: str | None = None, traceparent: str | None = None,
         outcome: str | None = None) -> dict:
    pairing = pairing_for(peer)
    from . import channel
    if not _project_members(pairing["local_project"], sender.get("cwd") or "/"):
        return {"ok": False, "status": "refused",
                "error": "this session is not in project %s, which is what %s is paired with"
                         % (pairing["local_project"], peer)}
    msg_id = msg_id or envelope.new_id()
    wait = clamp_wait(wait, MAX_SEND_WAIT)
    request = {"op": "send", "id": msg_id, "target": spec, "body": body, "kind": kind,
               "reply_to": reply_to, "wait": wait, "project": pairing["local_project"],
               "traceparent": traceparent, "outcome": outcome,
               "sender": {k: sender.get(k) for k in ("name", "alias", "ref", "session_id",
                                                     "permission_mode")}}
    ledger.queued(msg_id, sender, {"name": spec, "alias": peer, "ref": None, "runtime": "remote"},
                  "remote:%s" % peer, kind, body)
    timeout = max(30, wait + 20)
    try:
        reply = call(peer, request, timeout=timeout)
    except RemoteUnreached as exc:
        ledger.failed(msg_id, str(exc))
        return {"ok": False, "status": "error", "error": str(exc), "id": msg_id}
    except RemoteError as exc:
        # The request may have run over there and only the answer was lost;
        # `queued` would claim it is on its way, `error` that it never left.
        ledger.unknown(msg_id, str(exc), settle_after=time.time() + timeout + UNKNOWN_GRACE)
        return {"ok": False, "status": "unknown", "id": msg_id,
                "error": "%s; it may or may not have arrived. Check with `xsm status %s` before "
                         "sending it again: a new send is a new message and may run twice"
                         % (exc, msg_id)}
    _settle(msg_id, peer, reply)
    reply["id"] = msg_id
    return reply


def _settle(msg_id: str, peer: str, reply: dict) -> None:
    """Write what the peer answered into this side's ledger."""
    status = reply.get("status")
    if status in ("delivered", "held", "blocked"):
        ledger.receipt(msg_id, status, dict(reply.get("target") or {}, alias="%s@%s" % (
            (reply.get("target") or {}).get("alias"), peer)), reply.get("error") or "")
    elif not reply.get("ok"):
        # The peer answered and did not take it: as final as a local refusal.
        ledger.failed(msg_id, "%s: %s" % (peer, reply.get("error") or "refused"),
                      status="refused" if status == "refused" else "error")


# Added to the lost call's own timeout: until then "no record over there" may
# still mean "not yet" — the call whose answer was lost can still be running
# on the far side.
UNKNOWN_GRACE = 60


def reconcile(msg_id: str, wait: float = 0.0) -> dict:
    """Ask the peer what became of a message sent to it that this side holds
    as `queued` or `unknown`, and record the answer. A remote message gets no
    local receipt, so without asking it stays unconfirmed here even after the
    far side recorded it delivered (issue #4)."""
    entry = ledger.status(msg_id)
    scope = entry.get("scope") or ""
    if not scope.startswith("remote:") or entry.get("status") not in ("queued", "unknown"):
        return entry
    peer = scope[len("remote:"):]
    try:
        wait = clamp_wait(wait, MAX_STATUS_WAIT)
        reply = call(peer, {"op": "status", "id": msg_id, "wait": wait},
                     timeout=max(30, wait + 20))
    except RemoteError as exc:
        entry["note"] = "could not ask %s: %s" % (peer, exc)
        return entry
    if not reply.get("ok"):
        if str(reply.get("error") or "").startswith("unknown op"):
            # A peer running an xsm from before `status` answers every op it
            # does not know this way. Not an answer about the message: it stays
            # as it was, and the person is told what would help.
            entry["note"] = ("%s runs an xsm too old to answer `status` for this message; "
                             "update xsm there" % peer)
        else:
            entry["note"] = "%s did not answer for it: %s" % (peer, reply.get("error"))
        return entry
    if reply.get("status") is None:
        settle_after = entry.get("settle_after") or (entry.get("unknown_t") or 0) + 300
        if entry.get("status") == "unknown" and time.time() >= settle_after:
            # Nothing was recorded over there after the lost call's time was
            # up, so it most likely never got as far as queueing it. Not
            # certain: a request delayed by a long network partition could
            # still arrive. Resending under the same id is always safe (the
            # peer refuses a second run of an id it knows); a new id risks a
            # duplicate (review, 2026-09-29).
            ledger.failed(msg_id, "%s had no record of it after the call's time was up, so it "
                                  "most likely did not arrive; resending under the same id is "
                                  "always safe, a new id risks a duplicate" % peer)
        elif entry.get("status") == "unknown":
            # Too soon to say: the far side may still be working on the call
            # whose answer was lost. Calling it failed now is what makes a
            # person resend under a new id and run the task twice (review of
            # issue #4, 2026-09-29).
            entry["note"] = ("%s has no record of it yet; ask again after %s before sending it again"
                             % (peer, time.strftime("%H:%M:%S", time.localtime(settle_after))))
            return entry
        else:
            entry["note"] = "%s has no record of it (pruned there, or re-paired)" % peer
            return entry
    elif reply["status"] == "queued":
        ledger.reached(msg_id)
    else:
        _settle(msg_id, peer, dict(reply, ok=reply["status"] in ("delivered", "held", "blocked"),
                                   error=reply.get("reason")))
    return ledger.status(msg_id)
