"""Runs tests/vectors.json against this implementation.

The vectors are the contract: a port in another language passes them the same
way, by feeding each case through its own parser, scope check, resolver, gate
and forecast. Keep behaviour changes and vector changes in the same commit —
if only the code moves, this file fails, which is the point.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
VECTORS = json.load(open(os.path.join(REPO, "tests", "vectors.json")))


class VectorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsm-vec-")
        os.environ["XSM_HOME"] = self.tmp
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import paths
        paths.HOME = self.tmp
        paths.ensure_home()
        for name in ("ws", "far", "other"):
            os.makedirs(os.path.join(self.tmp, name), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -----------------------------------------------------------------

    def fill(self, value):
        return value.replace("{tmp}", self.tmp) if isinstance(value, str) else value

    def write_config(self, config):
        from xsm import config as cfg, paths
        if config is None:
            return
        filled = json.loads(json.dumps(config).replace("{tmp}", self.tmp))
        paths.write_json(paths.path(cfg.CONFIG), filled)

    def register(self, spec):
        """Create a session the way a hook would, plus the runtime's own record."""
        from xsm import paths, registry
        home = os.path.join(self.tmp, "homes", spec["alias"])
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        cwd = self.fill(spec.get("cwd") or self.tmp)
        rec = registry.upsert("claude", home, spec["session_id"], os.getpid(), cwd,
                              permission_mode=spec.get("permission_mode"), name=spec["name"])
        paths.write_json(os.path.join(home, "sessions", "%d.json" % os.getpid()),
                         {"name": spec["name"], "sessionId": spec["session_id"],
                          "messagingSocketPath": spec.get("socket", "")})
        return rec

    def records_by_session(self, sessions):
        return {spec["session_id"]: self.register(spec) for spec in sessions}


class ParseVectors(VectorCase):
    pass


class BuildVectors(VectorCase):
    pass


class ScopeVectors(VectorCase):
    pass


class ResolveVectors(VectorCase):
    pass


class GateVectors(VectorCase):
    pass


class ForecastVectors(VectorCase):
    pass


def _parse_case(case):
    def run(self):
        from xsm import envelope
        parsed = envelope.parse(case["prompt"])
        expect = case["expect"]
        self.assertEqual(parsed.peer, expect["peer"], case["id"])
        self.assertEqual(parsed.body, expect["body"], case["id"])
        for key, value in expect.get("header", {}).items():
            self.assertEqual(parsed.header.get(key), value, "%s: header %s" % (case["id"], key))
        if expect.get("header") == {}:
            self.assertEqual(parsed.header, {}, case["id"])
        for key, value in expect.get("attrs", {}).items():
            self.assertEqual(parsed.attrs.get(key), value, "%s: attr %s" % (case["id"], key))
    return run


def _build_case(case):
    def run(self):
        from xsm import envelope
        args = case["args"]
        wire = envelope.build(args["body"], msg_id=args["msg_id"], sender=case["sender"],
                              scope=args["scope"], kind=args.get("kind", "note"),
                              reply_to=args.get("reply_to"))
        for fragment in case.get("expect_contains", []):
            self.assertIn(fragment, wire, case["id"])
        for fragment in case.get("expect_absent", []):
            self.assertNotIn(fragment, wire, case["id"])
        self.assertTrue(envelope.parse(wire).peer, "%s: must parse back as a peer message" % case["id"])
    return run


def _scope_case(case):
    def run(self):
        from xsm import config
        self.write_config(case.get("config"))
        a = {k: self.fill(v) for k, v in case["a"].items()}
        b = {k: self.fill(v) for k, v in case["b"].items()}
        scope, reason = config.scope_for(a, b)
        want = case["expect"]["scope_kind"]
        if want is None:
            self.assertIsNone(scope, "%s: %s" % (case["id"], reason))
        else:
            self.assertIsNotNone(scope, case["id"])
            self.assertTrue(scope == want or scope.startswith(want + ":"),
                            "%s: got %r, wanted %r" % (case["id"], scope, want))
    return run


def _resolve_case(case):
    def run(self):
        from xsm import resolve
        records = self.records_by_session(case["sessions"])
        target = case["target"]
        for session_id, rec in records.items():
            target = target.replace("{ref:%s}" % session_id, rec["ref"])
        found = resolve.resolve(target, include_offline=True)
        expect = case["expect"]
        self.assertEqual(found.status, expect["status"], "%s: %s" % (case["id"], found.reason))
        if "alias" in expect:
            self.assertEqual(found.record["alias"], expect["alias"], case["id"])
        if "candidates" in expect:
            self.assertEqual(len(found.candidates), expect["candidates"], case["id"])
    return run


def _gate_case(case):
    def run(self):
        from xsm import envelope, paths
        self.write_config(case.get("config"))
        records = self.records_by_session(case["sessions"])
        receiver = records[case["receiver"]]
        message = case["message"]

        if message["kind"] == "raw":
            prompt = message.get("prompt", "")
        elif message["kind"] == "envelope-only":
            prompt = "<%s from-mode=\"prompting\">\n%s\n</%s>" % (
                envelope.TAG, message["body"], envelope.TAG)
        else:
            sender = records.get(message["from"]) or {
                "name": "ghost", "alias": "claude-9", "ref": "ffffff",
                "session_id": "ghost", "permission_mode": "auto"}
            from xsm import config as cfg
            scope = message.get("scope_override") or cfg.scope_for(sender, receiver)[0] or "none"
            prompt = envelope.build(message["body"], msg_id="vec-%s" % case["id"],
                                    sender=sender, scope=scope, kind=message.get("kind_field", "note"))

        payload = {"hook_event_name": message.get("event", "UserPromptSubmit"),
                   "session_id": receiver["session_id"], "cwd": receiver["cwd"],
                   "prompt": prompt, "prompt_id": "p", "session_title": receiver["name"],
                   "permission_mode": receiver.get("permission_mode") or "auto"}
        env = dict(os.environ, XSM_HOME=self.tmp,
                   CLAUDE_CONFIG_DIR=receiver["home"], CLAUDE_CODE_SESSION_ID=receiver["session_id"])
        if case.get("force_error"):
            env["XSM_FORCE_ERROR"] = "1"
        else:
            env.pop("XSM_FORCE_ERROR", None)
        out = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "xsm-hook.py")],
                             input=json.dumps(payload), capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, "%s: hook must always exit 0" % case["id"])
        expect = case["expect"]
        stdout = out.stdout.strip()

        if expect["output"] == "none":
            self.assertEqual(stdout, "", "%s: expected silence, got %s" % (case["id"], stdout))
            return
        emitted = json.loads(stdout)
        if expect["output"] == "allow":
            context = emitted["hookSpecificOutput"]["additionalContext"]
            for fragment in expect.get("context_contains", []):
                self.assertIn(fragment, context, case["id"])
        else:
            self.assertEqual(emitted["decision"], "block", case["id"])
            self.assertIn(expect["reason_contains"], emitted["reason"], case["id"])
            if expect.get("held"):
                held = os.listdir(paths.path(paths.HELD))
                self.assertTrue(held, "%s: the body must be kept before refusing" % case["id"])
    return run


def _forecast_case(case):
    def run(self):
        from xsm import paths, send
        home = os.path.join(self.tmp, "recv-home-%s" % case["id"])
        os.makedirs(home, exist_ok=True)
        if case["receiver_inbound"]:
            paths.write_json(os.path.join(home, "settings.json"),
                             {"crossSessionInbound": case["receiver_inbound"]})
        sender = {"runtime": "claude", "permission_mode": case["sender_mode"]}
        receiver = {"runtime": "claude", "permission_mode": case["receiver_mode"], "home": home}
        self.assertEqual(send.native_forecast(sender, receiver)[0], case["expect"], case["id"])
    return run


def _attach(cls, section, factory):
    for case in VECTORS[section]:
        setattr(cls, "test_%s" % case["id"].replace("-", "_"), factory(case))


_attach(ParseVectors, "parse", _parse_case)
_attach(BuildVectors, "build", _build_case)
_attach(ScopeVectors, "scope", _scope_case)
_attach(ResolveVectors, "resolve", _resolve_case)
_attach(GateVectors, "gate", _gate_case)
_attach(ForecastVectors, "native_forecast", _forecast_case)


if __name__ == "__main__":
    unittest.main()
