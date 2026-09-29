"""Unit tests for the parts that decide something: wire format, addressing,
scope, the hook's fallback, and the settings merge.

Run: python3 -m unittest discover -s tests -v
Everything here works on a temporary XSM_HOME; no session is contacted.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _run_cli(argv: list) -> str:
    from xsm import cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(argv)
    return out.getvalue()


RUNTIME_IDENTITY = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID",
                    "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "XSM_SANDBOXED")


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsm-test-")
        os.environ["XSM_HOME"] = self.tmp
        # The suite runs inside a Claude or Codex shell, whose ids and sandbox
        # markers decide who `me` is: a test setting a Claude id kept the real
        # CODEX_THREAD_ID beside it (review, 2026-09-28). Tests set their own.
        for key in RUNTIME_IDENTITY:
            self.addCleanup(lambda k=key, v=os.environ.get(key):
                            os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))
            os.environ.pop(key, None)
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import paths, registry
        paths.HOME = self.tmp
        paths.ensure_home()
        # Never scan this machine's real processes from a test: it made runs
        # depend on whatever Codex happened to be open (and lsof was slow).
        registry._running_codex = lambda: []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class EnvelopeTest(TempState):
    def test_round_trip_keeps_header_and_body(self):
        from xsm import envelope
        sender = {"name": "a", "alias": "claude-4", "ref": "aaaaaa", "socket": "/tmp/s.sock",
                  "session_id": "s1", "permission_mode": "bypassPermissions"}
        wire = envelope.build("hello", msg_id="m1", sender=sender, scope="repo:x", kind="task")
        parsed = envelope.parse(wire)
        self.assertTrue(parsed.peer)
        self.assertEqual(parsed.body, "hello")
        self.assertEqual(parsed.header["id"], "m1")
        self.assertEqual(parsed.header["kind"], "task")
        self.assertEqual(parsed.attrs["from-mode"], "bypass")

    def test_from_mode_follows_the_registry_not_the_caller(self):
        from xsm import envelope
        prompting = {"name": "a", "alias": "h", "ref": "r", "permission_mode": "auto"}
        unknown = {"name": "a", "alias": "h", "ref": "r"}
        self.assertIn('from-mode="prompting"',
                      envelope.build("x", msg_id="i", sender=prompting, scope="s"))
        self.assertNotIn("from-mode", envelope.build("x", msg_id="i", sender=unknown, scope="s"))

    def test_plain_text_is_not_a_peer_message(self):
        from xsm import envelope
        parsed = envelope.parse("just typing")
        self.assertFalse(parsed.peer)
        self.assertEqual(parsed.header, {})

    def test_envelope_without_header_is_still_peer(self):
        """The case S8-g2 measured: an envelope we did not write."""
        from xsm import envelope
        parsed = envelope.parse('<cross-session-message from-mode="bypass">\nhi\n'
                                '</cross-session-message>')
        self.assertTrue(parsed.peer)
        self.assertEqual(parsed.header, {})

    def test_traceparent_rides_along_when_there_is_one(self):
        from xsm import envelope
        sender = {"name": "a", "alias": "claude-4", "ref": "aaaaaa"}
        tp = "00-%s-%s-01" % ("a" * 32, "b" * 16)
        wire = envelope.build("hi", msg_id="m1", sender=sender, scope="s", traceparent=tp)
        self.assertEqual(envelope.parse(wire).header["traceparent"], tp)
        self.assertEqual(envelope.parse(wire).body, "hi")
        plain = envelope.build("hi", msg_id="m1", sender=sender, scope="s")
        self.assertNotIn("traceparent", plain)
        self.assertNotIn("traceparent", envelope.parse(plain).header)


class ScopeTest(TempState):
    def test_same_directory_is_in_scope_and_different_ones_are_not(self):
        from xsm import config
        here = {"cwd": self.tmp, "runtime": "claude", "alias": "claude-3"}
        there = {"cwd": os.path.dirname(self.tmp), "runtime": "claude", "alias": "claude-4"}
        self.assertIsNotNone(config.scope_for(here, dict(here))[0])
        self.assertIsNone(config.scope_for(here, there)[0])

    def test_explicit_scope_crosses_directories(self):
        from xsm import config, paths
        a = {"cwd": os.path.join(self.tmp, "a"), "runtime": "claude", "alias": "claude-3"}
        b = {"cwd": os.path.join(self.tmp, "b"), "runtime": "codex", "alias": "codex"}
        paths.write_json(paths.path(config.CONFIG), {"scopes": [
            {"id": "pair", "members": [{"runtime": "claude", "cwd": os.path.join(self.tmp, "a")},
                                       {"runtime": "codex", "cwd": os.path.join(self.tmp, "b")}]}]})
        self.assertEqual(config.scope_for(a, b)[0], "pair")

    def test_a_worker_outside_the_parents_scope_talks_to_its_parent_only(self):
        from xsm import config, workers
        parent = {"cwd": os.path.join(self.tmp, "a"), "runtime": "claude", "ref": "p1"}
        worker = {"cwd": os.path.join(self.tmp, "b"), "runtime": "codex", "ref": "w1"}
        neighbour = {"cwd": os.path.join(self.tmp, "b"), "runtime": "claude", "ref": "n1"}
        self.assertIsNone(config.scope_for(parent, worker)[0])
        workers.save({"name": "helper", "ref": "w1", "parent_ref": "p1"})
        self.assertEqual(config.scope_for(parent, worker)[0], "worker:helper")
        self.assertEqual(config.scope_for(worker, parent)[0], "worker:helper")
        self.assertIsNone(config.scope_for(parent, neighbour)[0])
        self.assertIsNotNone(config.scope_for(worker, neighbour)[0])   # same folder, as before

    def _codex(self, sid, where, pid=None):
        """A registered Codex session in `where` (relative to tmp): Codex needs
        no socket, so a live pid is enough to read live."""
        from xsm import registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        cwd = os.path.join(self.tmp, where)
        os.makedirs(cwd, exist_ok=True)
        registry.upsert("codex", home, sid, pid or os.getpid(), cwd, name=sid)
        return registry.by_session("codex", sid)

    def _dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def _share_ref(self, sid, ref):
        """Force a ref collision: 24-bit refs collided within ~11k tries."""
        from xsm import paths, registry
        p = paths.path(paths.SESSIONS, "codex-%s.json" % sid)
        rec = paths.read_json(p)
        rec["ref"] = ref
        paths.write_json(p, rec)
        return registry.by_session("codex", sid)

    def test_a_reach_opens_one_session_to_one_folder_both_ways(self):
        from xsm import config
        me = self._codex("s-me", "a")
        mate = self._codex("s-mate", "a")
        there = self._codex("s-there", "b/sub")
        elsewhere = self._codex("s-else", "c")
        self.assertIsNone(config.scope_for(me, there)[0])
        self.assertIn("xsm_reach", config.scope_for(me, there)[1])
        entry, added = config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        self.assertTrue(added)
        self.assertEqual((entry["session_id"], entry["pid"]), ("s-me", os.getpid()))
        link = "reach:%s" % me["ref"]
        self.assertEqual(config.scope_for(me, there)[0], link)
        self.assertEqual(config.scope_for(there, me)[0], link)          # the reply
        self.assertIsNone(config.scope_for(mate, there)[0])              # only that session
        self.assertIsNone(config.scope_for(me, elsewhere)[0])            # only that folder
        self.assertEqual(config.drop_reach(me["ref"]), 1)
        self.assertIsNone(config.scope_for(me, there)[0])

    def test_a_reach_goes_when_its_session_does(self):
        from xsm import config, housekeeping
        me = self._codex("s-gone", "a")
        os.makedirs(os.path.join(self.tmp, "b"), exist_ok=True)
        config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        self._codex("s-gone", "a", pid=self._dead_pid())               # it crashed
        removed = housekeeping.prune()
        self.assertEqual([r["ref"] for r in removed["reaches"]], [me["ref"]])
        self.assertEqual(config.reaches(), [])

    def test_a_reach_does_not_come_back_with_a_resume(self):
        """`claude --resume` reuses the session id under a new pid; before the
        hourly prune ran, the reach used to apply again (2026-09-28)."""
        from xsm import config, housekeeping
        me = self._codex("s-res", "a")
        there = self._codex("s-far", "b")
        config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        self.assertIsNotNone(config.scope_for(me, there)[0])
        resumed = self._codex("s-res", "a", pid=os.getppid())          # same id, new run
        self.assertEqual(resumed["state"], "live")
        self.assertEqual(resumed["ref"], me["ref"])
        self.assertIsNone(config.scope_for(resumed, there)[0])
        self.assertIsNone(config.scope_for(there, resumed)[0])
        # a probe without pid/lstart is checked against the pointer on disk
        bare = {k: resumed[k] for k in ("runtime", "home", "session_id", "ref", "cwd")}
        self.assertIsNone(config.scope_for(bare, there)[0])
        self.assertEqual(len(housekeeping.prune()["reaches"]), 1)
        self.assertEqual(config.reaches(), [])

    def test_session_end_drops_the_reach(self):
        from xsm import config, receive
        me = self._codex("s-bye", "a")
        other = self._codex("s-stay", "c")
        os.makedirs(os.path.join(self.tmp, "b"), exist_ok=True)
        config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        config.add_reach(other["ref"], os.path.join(self.tmp, "b"), "tester")
        receive.handle({"hook_event_name": "SessionEnd", "session_id": "s-bye",
                        "reason": "prompt_input_exit", "turn_id": "x"})
        self.assertEqual([r.get("session_id") for r in config.reaches()], ["s-stay"])

    def test_a_goodbye_from_another_home_leaves_the_pointer_and_its_reach(self):
        """Pointers are keyed on runtime and session id only; a copied
        CODEX_HOME holding the same thread id ended the other home's session
        and dropped its reach while it ran on (2026-09-28)."""
        from xsm import config, receive, registry
        me = self._codex("s-twin", "a")
        os.makedirs(os.path.join(self.tmp, "b"), exist_ok=True)
        config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        copy = os.path.join(self.tmp, "homes", "codex-copy")
        os.makedirs(copy, exist_ok=True)
        with mock.patch.dict(os.environ, {"CODEX_HOME": copy}):
            receive.handle({"hook_event_name": "SessionEnd", "session_id": "s-twin",
                            "reason": "prompt_input_exit", "turn_id": "x"})
        rec = registry.by_session("codex", "s-twin")
        self.assertNotIn("ended_at", rec)
        self.assertEqual(rec["state"], "live")
        self.assertEqual([r.get("session_id") for r in config.reaches()], ["s-twin"])
        # the session's own goodbye, from its own home, still ends it
        with mock.patch.dict(os.environ, {"CODEX_HOME": me["home"]}):
            receive.handle({"hook_event_name": "SessionEnd", "session_id": "s-twin",
                            "reason": "prompt_input_exit", "turn_id": "x"})
        self.assertIn("ended_at", registry.by_session("codex", "s-twin"))
        self.assertEqual(config.reaches(), [])

    def test_a_reach_holds_for_the_granted_session_only_when_refs_collide(self):
        from xsm import config
        me = self._codex("s-one", "a")
        twin = self._share_ref(self._codex("s-two", "d")["session_id"], me["ref"])
        there = self._codex("s-far2", "b")
        with self.assertRaises(ValueError) as cm:            # which of the two? refuse
            config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        self.assertIn("share ref", str(cm.exception))
        config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester", session=me)
        self.assertIsNotNone(config.scope_for(me, there)[0])
        self.assertIsNone(config.scope_for(twin, there)[0])
        self.assertIsNone(config.scope_for(there, twin)[0])

    def test_a_reach_stored_without_its_session_holds_for_nobody(self):
        """Rows written before the binding carry only a ref; they fail closed
        and granting again replaces them."""
        from xsm import config, paths
        me = self._codex("s-old", "a")
        there = self._codex("s-far3", "b")
        root = config.project_root(os.path.join(self.tmp, "b"))
        paths.write_json(paths.path(config.CONFIG), {"reaches": [
            {"ref": me["ref"], "root": root, "t": 1, "by": "tester"}]})
        self.assertIsNone(config.scope_for(me, there)[0])
        entry, added = config.add_reach(me["ref"], os.path.join(self.tmp, "b"), "tester")
        self.assertTrue(added)
        self.assertEqual(len(config.reaches()), 1)
        self.assertIsNotNone(config.scope_for(me, there)[0])

    def test_no_worker_link_without_refs(self):
        from xsm import config, workers
        workers.save({"name": "early", "ref": None, "parent_ref": None})
        a = {"cwd": os.path.join(self.tmp, "a"), "runtime": "claude"}
        b = {"cwd": os.path.join(self.tmp, "b"), "runtime": "claude"}
        self.assertIsNone(config.scope_for(a, b)[0])


class RecordedSocketTest(TempState):
    """The inbox socket comes from CLAUDE_CODE_MESSAGING_SOCKET, which Claude Code
    documents and hands to hooks; its own sessions/<pid>.json is only a fallback."""

    def _home(self, native):
        from xsm import paths
        home = os.path.join(self.tmp, "homes", "claude-3")
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        paths.write_json(os.path.join(home, "sessions", "%d.json" % os.getpid()), native)
        return home

    def test_recorded_socket_is_used_without_the_native_field(self):
        from xsm import registry
        home = self._home({"name": "solo", "sessionId": "s1"})
        sock = "/tmp/cc-socks/%d.sock" % os.getpid()
        registry.upsert("claude", home, "s1", os.getpid(), self.tmp, socket=sock)
        self.assertEqual(registry.by_session("claude", "s1")["socket"], sock)

    def test_native_field_still_covers_records_without_one(self):
        from xsm import registry
        home = self._home({"name": "solo", "sessionId": "s1",
                           "messagingSocketPath": "/tmp/native.sock"})
        registry.upsert("claude", home, "s1", os.getpid(), self.tmp)
        self.assertEqual(registry.by_session("claude", "s1")["socket"], "/tmp/native.sock")

    def test_a_socket_named_for_another_pid_is_not_kept(self):
        from xsm import registry
        home = self._home({"name": "solo", "sessionId": "s1"})
        rec = registry.upsert("claude", home, "s1", os.getpid(), self.tmp,
                              socket="/tmp/cc-socks/%d.sock" % (os.getpid() + 1))
        self.assertNotIn("socket", rec)

    def test_socket_survives_a_register_without_one_but_not_a_new_pid(self):
        from xsm import registry
        home = self._home({"name": "solo", "sessionId": "s1"})
        sock = "/tmp/cc-socks/%d.sock" % os.getpid()
        registry.upsert("claude", home, "s1", os.getpid(), self.tmp, socket=sock)
        self.assertEqual(registry.upsert("claude", home, "s1", os.getpid(), self.tmp)["socket"], sock)
        # claude --resume brings the same session id back under another pid
        self.assertNotIn("socket", registry.upsert("claude", home, "s1", os.getpid() + 1, self.tmp))


class ResolveTest(TempState):
    def _register(self, name, alias, session_id, state="live"):
        from xsm import paths, registry
        home = os.path.join(self.tmp, "homes", alias)
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        rec = registry.upsert("claude", home, session_id, os.getpid(), self.tmp, name=name)
        paths.write_json(os.path.join(home, "sessions", "%d.json" % os.getpid()),
                         {"name": name, "messagingSocketPath": "", "sessionId": session_id})
        return rec

    def test_ambiguous_names_refuse_with_candidates(self):
        from xsm import resolve
        self._register("twin", "claude-3", "s1")
        self._register("twin", "claude-4", "s2")
        found = resolve.resolve("twin", include_offline=True)
        self.assertEqual(found.status, "ambiguous")
        self.assertEqual(len(found.candidates), 2)

    def test_home_qualifier_picks_one(self):
        from xsm import resolve
        self._register("twin", "claude-3", "s1")
        self._register("twin", "claude-4", "s2")
        found = resolve.resolve("twin@claude-4", include_offline=True)
        self.assertEqual(found.status, "resolved")
        self.assertEqual(found.record["alias"], "claude-4")

    def test_at_sign_inside_a_name_is_not_a_qualifier(self):
        """Codex allows @ in thread names (S7), so only a known home qualifies."""
        from xsm import resolve
        self._register("team@standup", "claude-3", "s3")
        found = resolve.resolve("team@standup", include_offline=True)
        self.assertEqual(found.status, "resolved")


class RefCollisionTest(TempState):
    """Refs are 24 bits (sha256[:6]); 2026-09-28 found collisions within ~11k
    synthetic tries. An address or a header ref that names two sessions must
    not quietly pick one."""

    def _codex(self, sid, pid=None):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, sid, pid or os.getpid(), self.tmp, name=sid)
        return registry.by_session("codex", sid)

    def _collide(self, sid, ref):
        from xsm import paths, registry
        p = paths.path(paths.SESSIONS, "codex-%s.json" % sid)
        rec = paths.read_json(p)
        rec["ref"] = ref
        paths.write_json(p, rec)
        return registry.by_session("codex", sid)

    def test_a_shared_ref_resolves_ambiguous_with_session_addresses(self):
        from xsm import resolve
        one = self._codex("c-one")
        self._codex("c-two")
        self._collide("c-two", one["ref"])
        found = resolve.resolve("ref:%s" % one["ref"])
        self.assertEqual(found.status, "ambiguous")
        self.assertEqual(len(found.candidates), 2)
        self.assertIn("codex:c-two", found.reason)
        self.assertEqual(resolve.resolve("codex:c-two").status, "resolved")

    def test_receive_refuses_a_sender_whose_ref_names_two_sessions(self):
        from xsm import config, envelope, receive
        one = self._codex("c-a")
        two = self._collide(self._codex("c-b")["session_id"], one["ref"])
        me = self._codex("c-me")
        wire = envelope.build("hi", msg_id="m1", sender=one, scope=config.scope_for(one, me)[0])
        bare = re.sub(r' from-session="[^"]*"', "", wire)
        blind = bare.replace('from="c-a@codex"', 'from="someone@codex"')
        # with neither the session nor the name telling them apart, no sender
        decision, reason = receive.check(envelope.parse(blind), me)
        self.assertEqual(decision, "block")
        self.assertIn("share ref %s" % two["ref"], reason)
        rec, why = receive._sender_record(envelope.parse(blind))
        self.assertIsNone(rec)
        self.assertIn("ambiguous", why)
        # the envelope names its session: that picks one of the two
        self.assertEqual(receive.check(envelope.parse(wire), me), ("pass", ""))
        self.assertEqual(receive._sender_record(envelope.parse(wire))[0]["session_id"], "c-a")
        # without it, the from name still does
        self.assertEqual(receive._sender_record(envelope.parse(bare))[0]["session_id"], "c-a")


class StoppedTargetSendTest(TempState):
    """Names resolve to live sessions only; ref:, claude: and codex: used to
    reach a stopped one too (2026-09-28)."""

    def _session(self, runtime, sid, pid):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", runtime)
        os.makedirs(home, exist_ok=True)
        registry.upsert(runtime, home, sid, pid, self.tmp, name=sid)
        return registry.by_session(runtime, sid)

    def _dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def _no_delivery(self):
        from xsm import adapters

        def called(*args, **kw):
            raise AssertionError("an adapter was called for a stopped target")
        return mock.patch.multiple(adapters, to_claude=called, to_codex=called)

    def test_an_ended_or_stale_target_is_refused_by_any_address(self):
        from xsm import ledger, registry, send
        me = self._session("codex", "me", os.getpid())
        gone = self._session("claude", "gone", self._dead_pid())
        registry.mark_ended("claude", "gone", "prompt_input_exit")
        crashed = self._session("codex", "crashed", self._dead_pid())
        self.assertEqual(registry.by_session("claude", "gone")["state"], "ended")
        with self._no_delivery():
            for spec in ("ref:%s" % gone["ref"], "claude:gone"):
                r = send.send(spec, "hi", sender=me)
                self.assertEqual(r.status, "refused", spec)
                self.assertIn("is not running (ended)", r.reason)
                self.assertIn("claude --resume gone", r.reason)
            for spec in ("ref:%s" % crashed["ref"], "codex:crashed"):
                r = send.send(spec, "hi", sender=me)
                self.assertEqual(r.status, "refused", spec)
                self.assertIn("is not running (stale)", r.reason)
        self.assertEqual(ledger.recent(), [], "nothing was recorded as queued")

    def test_live_and_unknown_targets_still_send(self):
        from xsm import adapters, send
        me = self._session("codex", "me2", os.getpid())
        live = self._session("codex", "alive", os.getpid())
        unknown = self._session("codex", "unsure", 0)          # no pid: liveness unknown
        self.assertEqual((live["state"], unknown["state"]), ("live", "unknown"))
        sent = []
        with mock.patch.object(adapters, "to_codex", lambda *a: sent.append(a[1])), \
                mock.patch.dict(os.environ, {"CODEX_SANDBOX": "", "XSM_SANDBOXED": ""}):
            for spec in ("ref:%s" % live["ref"], "codex:unsure"):
                self.assertEqual(send.send(spec, "hi", sender=me).status, "sent-unconfirmed", spec)
        self.assertEqual(sent, ["alive", "unsure"])


class HookFallbackTest(TempState):
    """A broken hook must refuse peer messages and never block the user."""

    def _run(self, prompt):
        env = dict(os.environ, XSM_FORCE_ERROR="1", XSM_HOME=self.tmp)
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "x",
                              "cwd": self.tmp, "prompt": prompt})
        return subprocess.run([sys.executable, os.path.join(REPO, "hooks", "xsm-hook.py")],
                              input=payload, capture_output=True, text=True, env=env)

    def test_peer_message_is_blocked(self):
        out = self._run('<cross-session-message from-mode="bypass">\n[xsm v1 id=1]\nhi\n'
                        '</cross-session-message>')
        self.assertIn('"decision": "block"', out.stdout)
        self.assertEqual(out.returncode, 0)

    def test_human_prompt_passes(self):
        out = self._run("please refactor this file")
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.returncode, 0)


class InstallTest(TempState):
    def test_merge_keeps_foreign_hooks_and_uninstall_restores(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-9")
        os.makedirs(home)
        original = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "sh ~/other-hook.sh"}]}]},
            "crossSessionInbound": "accept"}
        target = os.path.join(home, "settings.json")
        paths.write_json(target, original, mode=0o644)

        install.apply(home, "claude")
        after = paths.read_json(target)
        commands = [h["command"] for g in after["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertIn("sh ~/other-hook.sh", commands)
        self.assertTrue(any(install.MARKER in c for c in commands))
        self.assertEqual(after["crossSessionInbound"], "accept")

        install.remove(home, "claude")
        self.assertEqual(paths.read_json(target), original)



class ForecastTest(TempState):
    """The native gate Claude applies before our hook (S1): an explicit
    setting wins, then permission-mode parity."""

    def _home_with(self, value):
        from xsm import paths
        home = os.path.join(self.tmp, "home-%s" % (value or "none"))
        os.makedirs(home, exist_ok=True)
        if value:
            paths.write_json(os.path.join(home, "settings.json"), {"crossSessionInbound": value})
        return home

    def test_accept_setting_beats_mode_mismatch(self):
        from xsm import send
        a = {"runtime": "claude", "permission_mode": "bypassPermissions"}
        b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with("accept")}
        self.assertEqual(send.native_forecast(a, b)[0], "accept")

    def test_mode_mismatch_without_a_setting_is_held(self):
        from xsm import send
        a = {"runtime": "claude", "permission_mode": "bypassPermissions"}
        b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with(None)}
        verdict, why = send.native_forecast(a, b)
        self.assertEqual(verdict, "hold")
        self.assertIn("crossSessionInbound", why)

    def test_matching_modes_pass(self):
        from xsm import send
        a = {"runtime": "claude", "permission_mode": "auto"}
        b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with(None)}
        self.assertEqual(send.native_forecast(a, b)[0], "accept")

    def test_codex_target_has_no_such_gate(self):
        from xsm import send
        self.assertEqual(send.native_forecast({"runtime": "claude"}, {"runtime": "codex"})[0], "n/a")

    def test_a_project_setting_only_tightens_the_users(self):
        """Claude 2.1.284 held a message from a repository set to "hold" while
        the user's settings said "accept" (measured 2026-09-29)."""
        from xsm import paths, send
        a = {"runtime": "claude", "permission_mode": "auto"}
        for user, project, name, want in (("accept", "hold", "settings.local.json", "hold"),
                                          ("accept", "refuse", "settings.json", "refuse"),
                                          ("hold", "accept", "settings.json", "hold"),
                                          (None, None, None, "accept")):
            cwd = os.path.join(self.tmp, "repo-%s-%s" % (user, project))
            if project:
                os.makedirs(os.path.join(cwd, ".claude"), exist_ok=True)
                paths.write_json(os.path.join(cwd, ".claude", name), {"crossSessionInbound": project})
            b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with(user),
                 "cwd": cwd}
            self.assertEqual(send.native_forecast(a, b)[0], want, (user, project))

    def test_a_message_forecast_as_held_reads_as_awaiting_approval(self):
        from xsm import cli, ledger
        sender = {"name": "a", "ref": "aaaaaa", "runtime": "codex"}
        target = {"name": "b", "ref": "bbbbbb", "runtime": "claude"}
        ledger.queued("m-held", sender, target, "project:x", "note", "hi", forecast="hold")
        ledger.queued("m-plain", sender, target, "project:x", "note", "hi", forecast="accept")
        from unittest import mock
        with mock.patch.object(cli.registry, "records",
                               return_value=[{"ref": "bbbbbb", "state": "live"}]):
            rows = {r["id"]: r for r in cli._mark_undelivered(ledger.recent())}
        self.assertEqual(rows["m-held"]["status"], "awaiting-approval")
        self.assertEqual(rows["m-plain"]["status"], "queued")
        ledger.receipt("m-held", "delivered", target)        # the person pressed Deliver
        with mock.patch.object(cli.registry, "records",
                               return_value=[{"ref": "bbbbbb", "state": "live"}]):
            rows = {r["id"]: r for r in cli._mark_undelivered(ledger.recent())}
        self.assertEqual(rows["m-held"]["status"], "delivered")



class UnknownSelfTest(TempState):
    """A hook that cannot tell which session it guards must refuse peer
    messages: scope is unchecked, so passing would make that session an open
    door. Driven through handle() directly because whether the process tree
    happens to contain a real `claude` ancestor is environment-dependent.
    """

    def _message(self):
        from xsm import envelope
        sender = {"name": "send", "alias": "claude-3", "ref": "aaaaaa",
                  "session_id": "s1", "permission_mode": "auto"}
        return envelope.build("hi", msg_id="m1", sender=sender, scope="dir:x")

    def _handle(self, prompt):
        from xsm import receive
        receive.register = lambda data, runtime: None        # identity unknown
        return receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "r1",
                               "cwd": self.tmp, "prompt": prompt, "session_title": "recv"})

    def test_peer_message_is_refused(self):
        out = self._handle(self._message())
        self.assertEqual(out["decision"], "block")
        self.assertIn("cannot identify this session", out["reason"])
        self.assertTrue(os.listdir(os.path.join(self.tmp, "held")), "body must be kept")

    def test_human_prompt_is_untouched(self):
        self.assertIsNone(self._handle("my own prompt"))


class InterpreterPinTest(TempState):
    """Hooks run under a pinned absolute interpreter, never a launcher."""

    def test_absolute_path_is_taken_as_is(self):
        from xsm import install
        self.assertEqual(install.resolve_python(sys.executable), os.path.realpath(sys.executable))

    def test_unknown_spec_is_refused(self):
        from xsm import install
        with self.assertRaises(ValueError):
            install.resolve_python("definitely-not-a-python-9.9")

    def test_pin_is_recorded_and_used_in_the_hook_command(self):
        from xsm import install
        install.pin_python(sys.executable)
        self.assertEqual(install.pinned_python(), sys.executable)
        self.assertIn(sys.executable, install.hook_command("claude", "SessionStart"))
        self.assertTrue(install.hook_command("claude", "SessionStart").endswith(install.MARKER))

class IdempotenceTest(TempState):
    """Installing twice changes nothing and leaves no second backup, a dry run
    writes nothing at all, and an interpreter pin survives a later install that
    does not mention one."""

    def _home(self):
        from xsm import paths
        home = os.path.join(self.tmp, "claude-idem")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "settings.json"), {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "sh ~/other.sh"}]}]}}, mode=0o644)
        return home

    def _backups(self, home):
        return [f for f in os.listdir(home) if ".xsm-backup-" in f]

    def test_second_install_is_a_no_op(self):
        from xsm import install, paths
        home = self._home()
        install.apply(home, "claude")
        first = paths.read_json(os.path.join(home, "settings.json"))
        second = install.apply(home, "claude")
        self.assertTrue(second.get("unchanged"))
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json")), first)
        self.assertEqual(len(self._backups(home)), 1)

    def test_a_duplicated_or_stale_marked_group_collapses_to_one(self):
        from xsm import install, paths
        home = self._home()
        install.apply(home, "claude")
        target = os.path.join(home, "settings.json")
        data = paths.read_json(target)
        data["hooks"]["SessionStart"].append(
            {"hooks": [{"type": "command", "command": "/old/python /old/xsm-hook.py %s" % install.MARKER}]})
        paths.write_json(target, data, mode=0o644)
        install.apply(home, "claude")
        groups = paths.read_json(target)["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["hooks"][0]["command"], install.hook_command("claude", "SessionStart"))

    def test_round_trip_leaves_the_file_byte_identical(self):
        """A settings file people edit by hand must not be reformatted."""
        from xsm import install
        home = os.path.join(self.tmp, "claude-fmt")
        os.makedirs(home, exist_ok=True)
        target = os.path.join(home, "settings.json")
        original = ('{\n  "crossSessionInbound": "accept",\n  "hooks": {\n    "UserPromptSubmit": [\n'
                    '      {\n        "hooks": [\n          {\n            "type": "command",\n'
                    '            "command": "sh ~/other.sh"\n          }\n        ]\n      }\n'
                    '    ]\n  }\n}\n')
        open(target, "w").write(original)
        install.apply(home, "claude")
        install.remove(home, "claude")
        self.assertEqual(open(target).read(), original)

    def test_pin_survives_an_install_without_python(self):
        from xsm import install
        install.pin_python("/usr/bin/python3")
        self.assertEqual(install.resolve_python(None), "/usr/bin/python3")


class LauncherTest(TempState):
    """bin/xsm is meant to be symlinked onto PATH, so it must follow the link
    chain to find the package — resolving $0's directory alone once made
    `~/.local/bin/xsm` look for the code in ~/.local."""

    def _run(self, path, *args):
        env = dict(os.environ, XSM_HOME=self.tmp)
        return subprocess.run([path, *args], capture_output=True, text=True, env=env, cwd="/")

    def test_direct_call_works(self):
        out = self._run(os.path.join(REPO, "bin", "xsm"), "list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_symlinked_call_works(self):
        link = os.path.join(self.tmp, "xsm")
        os.symlink(os.path.join(REPO, "bin", "xsm"), link)
        out = self._run(link, "list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_symlink_to_a_symlink_works(self):
        first = os.path.join(self.tmp, "xsm-one")
        second = os.path.join(self.tmp, "xsm-two")
        os.symlink(os.path.join(REPO, "bin", "xsm"), first)
        os.symlink(first, second)
        out = self._run(second, "list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_copy_away_from_the_package_says_so(self):
        lonely = os.path.join(self.tmp, "lonely", "bin")
        os.makedirs(lonely)
        copied = os.path.join(lonely, "xsm")
        shutil.copy2(os.path.join(REPO, "bin", "xsm"), copied)
        out = self._run(copied, "list")
        self.assertEqual(out.returncode, 4)
        self.assertIn("no package at", out.stderr)


class ListFromAPlainTerminalTest(TempState):
    """`xsm list` run outside any session has no "us" to compare against, so it
    must not label every row out-of-scope."""

    def test_no_scope_verdict_without_a_session(self):
        from xsm import cli, paths, registry
        home = os.path.join(self.tmp, "homes", "claude-4")
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        registry.upsert("claude", home, "s1", os.getpid(), self.tmp, name="solo")
        paths.write_json(os.path.join(home, "sessions", "%d.json" % os.getpid()),
                         {"name": "solo", "sessionId": "s1", "messagingSocketPath": ""})
        for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET"):
            os.environ.pop(var, None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--all"])
        printed = out.getvalue()
        self.assertIn("not a registered session", printed)
        self.assertNotIn("out-of-scope", printed)


class NativeClaudeMessageTest(TempState):
    """Claude's own SendMessage carries from="uds:<socket>" and no xsm header
    (ADR-0013). check() names the sender by that socket and applies the scope
    rule an xsm message meets."""

    def _parsed(self, where):
        from xsm import envelope
        return envelope.parse('<cross-session-message from="%s" from-name="x" from-mode="bypass">'
                              "\nhello\n</cross-session-message>" % where)

    def _check(self, where, me, records):
        from unittest import mock
        from xsm import receive
        with mock.patch.object(receive.registry, "records", return_value=records), \
                mock.patch.object(receive.config, "scope_for", return_value=("project:ws", "")):
            return receive.check(self._parsed(where), me, {"strict_peers": False})

    def test_only_a_claude_receiver_takes_one(self):
        sender = {"runtime": "claude", "socket": "/tmp/cc-socks/1.sock", "state": "live",
                  "ref": "aaaaaa", "name": "a", "alias": "claude"}
        claude = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        codex = dict(claude, runtime="codex")
        self.assertEqual(self._check("uds:/tmp/cc-socks/1.sock", claude, [sender])[0], "pass")
        self.assertEqual(self._check("uds:/tmp/cc-socks/1.sock", codex, [sender])[0], "block")

    def test_a_socket_two_sessions_claim_names_nobody(self):
        sender = {"runtime": "claude", "socket": "/tmp/cc-socks/1.sock", "state": "live",
                  "ref": "aaaaaa"}
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        decision, reason = self._check("uds:/tmp/cc-socks/1.sock", me,
                                       [sender, dict(sender, ref="cccccc")])
        self.assertEqual(decision, "block")
        self.assertIn("claimed by 2 sessions", reason)

    def test_a_session_cleared_on_the_same_socket_is_not_a_second_owner(self):
        sender = {"runtime": "claude", "socket": "/tmp/cc-socks/1.sock", "state": "live",
                  "ref": "aaaaaa"}
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        before_clear = dict(sender, ref="cccccc", state="ended")
        self.assertEqual(self._check("uds:/tmp/cc-socks/1.sock", me,
                                     [before_clear, sender])[0], "pass")

    def test_a_repeated_from_cannot_replace_the_first(self):
        from xsm import envelope
        parsed = envelope.parse('<cross-session-message from="uds:/tmp/cc-socks/1.sock" '
                                'from-name="a" from="uds:/tmp/cc-socks/2.sock" x">'
                                "\nhello\n</cross-session-message>")
        self.assertEqual(parsed.attrs["from"], "uds:/tmp/cc-socks/1.sock")

    def test_a_sender_without_a_local_socket_is_held(self):
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        decision, reason = self._check("bridge:remote-control", me, [])
        self.assertEqual(decision, "block")
        self.assertIn("not a session xsm knows", reason)

    def test_a_socket_whose_only_owner_ended_is_held_as_not_running(self):
        ended = {"runtime": "claude", "socket": "/tmp/cc-socks/1.sock", "state": "ended",
                 "ref": "aaaaaa"}
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        decision, reason = self._check("uds:/tmp/cc-socks/1.sock", me, [ended])
        self.assertEqual(decision, "block")
        self.assertIn("not running", reason)

    def test_a_nested_envelope_is_judged_by_the_outer_sender(self):
        from unittest import mock
        from xsm import envelope, receive
        inner = envelope.build("hi", msg_id="m1", sender={"name": "x", "alias": "claude",
                                                          "ref": "cccccc"}, scope="project:ws")
        parsed = envelope.parse('<cross-session-message from="uds:/tmp/cc-socks/9.sock" '
                                'from-name="z" from-mode="bypass">\n%s\n</cross-session-message>'
                                % inner)
        self.assertEqual(parsed.attrs["from"], "uds:/tmp/cc-socks/9.sock")
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        with mock.patch.object(receive.registry, "records", return_value=[]):
            self.assertEqual(receive.check(parsed, me, {"strict_peers": False})[0], "block")

    def test_a_header_over_a_claude_socket_cannot_claim_a_codex_sender(self):
        from unittest import mock
        from xsm import envelope, receive
        codex = {"runtime": "codex", "ref": "cccccc", "name": "c", "alias": "codex",
                 "state": "live"}
        body = envelope.build("hi", msg_id="m2", sender=codex, scope="project:ws")
        parsed = envelope.parse(body.replace("<cross-session-message",
                                             '<cross-session-message from="uds:/tmp/cc-socks/9.sock"', 1))
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        with mock.patch.object(receive.registry, "records", return_value=[codex]), \
                mock.patch.object(receive.config, "scope_for", return_value=("project:ws", "")):
            decision, reason = receive.check(parsed, me, {"strict_peers": False})
        self.assertEqual(decision, "block")
        self.assertIn("not a Claude session", reason)

    def test_a_stopped_sender_is_held(self):
        sender = {"runtime": "claude", "socket": "/tmp/cc-socks/1.sock", "state": "stale",
                  "ref": "aaaaaa"}
        me = {"runtime": "claude", "ref": "bbbbbb", "cwd": "/ws"}
        self.assertEqual(self._check("uds:/tmp/cc-socks/1.sock", me, [sender])[0], "block")


class PluginPackagingTest(TempState):
    """The repository is also a Claude Code plugin (user decision, 2026-09-23:
    support both the plugin and `xsm install`). These pin what the manifests
    must say, because a plugin that is wrong is only found by installing it."""

    def _json(self, *parts):
        with open(os.path.join(REPO, *parts)) as fh:
            return json.load(fh)

    def test_the_manifests_point_at_files_that_exist(self):
        plugin = self._json(".claude-plugin", "plugin.json")
        self.assertEqual(plugin["name"], "xsm")
        self.assertNotIn("commands", plugin, "the commands are arguments of the one skill now")
        for field in ("skills", "hooks"):
            target = os.path.join(REPO, plugin[field][2:] if plugin[field].startswith("./")
                                  else plugin[field])
            self.assertTrue(os.path.exists(target), "%s -> %s" % (field, target))
        self.assertNotIn("mcpServers", plugin,
                         "Claude reads a plugin's MCP servers from .mcp.json at the plugin root; "
                         "a manifest field was ignored (measured 2026-09-23)")
        market = self._json(".claude-plugin", "marketplace.json")
        self.assertEqual([p["name"] for p in market["plugins"]], ["xsm"])
        self.assertEqual(market["plugins"][0]["version"], plugin["version"],
                         "the marketplace entry and the plugin must not disagree")

    def test_the_hooks_manifest_covers_every_event_the_gate_needs(self):
        hooks = self._json("hooks", "hooks.json")["hooks"]
        self.assertEqual(sorted(hooks),
                         ["PermissionRequest", "SessionEnd", "SessionStart", "UserPromptExpansion",
                          "UserPromptSubmit"])
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}", hook["command"], event)
                    launcher = os.path.join(REPO, "hooks", "xsm-hook")
                    self.assertTrue(os.access(launcher, os.X_OK), "the launcher must be executable")

    def test_the_mcp_server_has_no_env_that_could_point_the_store_at_nowhere(self):
        # Measured 2026-09-23: Claude counts a plugin's MCP servers only from
        # .mcp.json at the plugin root. An inline mcpServers object in
        # plugin.json and a path to another file both came out as zero.
        server = self._json(".mcp.json")["mcpServers"]["xsm"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", server["command"])
        self.assertNotIn("env", server, "an empty XSM_HOME would mean the working directory")
        self.assertTrue(os.access(os.path.join(REPO, "hooks", "xsm-mcp"), os.X_OK))

    def test_the_plugin_ships_the_skill_with_its_guide(self):
        """Claude copies a plugin as it is; references/ has to be in the tree."""
        for rel in ("SKILL.md", os.path.join("references", "guide.md")):
            self.assertTrue(os.path.isfile(os.path.join(REPO, "skills", "xsm", rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(REPO, ".claude-plugin", "commands")))


class BrokenCodexInstallTest(TempState):
    """An npm @openai/codex without its platform binary sat first on PATH and
    could not run at all (2026-09-23). Every delivery to a Codex session goes
    through `codex queue`, so xsm has to reach for one that works."""

    def _fake(self, name, script):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + script)
        os.chmod(path, 0o755)
        return path

    def test_a_broken_codex_is_skipped_for_the_next_one(self):
        from unittest import mock
        from xsm import adapters
        broken = self._fake("broken-codex", "echo 'Error: Missing optional dependency "
                                            "@openai/codex-darwin-arm64' >&2\nexit 1\n")
        good = self._fake("good-codex", 'echo "queued $*"\nexit 0\n')
        with mock.patch.object(adapters, "codex_bins", lambda: [broken, good]):
            self.assertIn("queued", adapters.to_codex(self.tmp, "t1", "hello"))

    def test_a_real_failure_is_not_retried_on_another_install(self):
        from unittest import mock
        from xsm import adapters
        refuses = self._fake("refusing-codex", "echo 'Error: no rollout found for thread' >&2\n"
                                               "exit 1\n")
        other = self._fake("other-codex", "echo should-not-run\nexit 0\n")
        calls = []
        with mock.patch.object(adapters, "codex_bins", lambda: [refuses, other]), \
                mock.patch.object(adapters, "queue_direct",
                                  lambda home, thread, text: calls.append(thread) or "direct"):
            self.assertEqual(adapters.to_codex(self.tmp, "t1", "hello"), "direct")
        self.assertEqual(calls, ["t1"], "the second install is not a second chance")

    def test_the_candidates_are_ordered_and_deduplicated(self):
        from unittest import mock
        from xsm import adapters
        mine = self._fake("my-codex", "exit 0\n")
        with mock.patch.dict(os.environ, {"XSM_CODEX": mine}), \
                mock.patch.object(adapters.shutil, "which", lambda name: mine):
            self.assertEqual(adapters.codex_bins()[0], mine)
            self.assertEqual(adapters.codex_bins().count(mine), 1)

    def test_doctor_names_an_install_that_cannot_run(self):
        from unittest import mock
        from xsm import install
        broken = self._fake("broken-codex", "echo 'Error: Missing optional dependency' >&2\n"
                                            "exit 1\n")
        good = self._fake("good-codex", "echo codex-cli 9.9.9\n")
        with mock.patch.object(install, "adapters", create=True):
            pass
        from xsm import adapters
        with mock.patch.object(adapters, "codex_bins", lambda: [broken, good]):
            rows = install.codex_versions()
        self.assertTrue(rows[0][1].startswith("does not run"), rows[0])
        self.assertEqual(rows[1][1], "codex-cli 9.9.9")

    def test_a_worker_is_started_with_a_codex_that_runs(self):
        from unittest import mock
        from xsm import adapters, workers
        broken = self._fake("broken-codex", "exit 1\n")
        good = self._fake("good-codex", "echo codex-cli 9.9.9\n")
        with mock.patch.object(adapters, "codex_bins", lambda: [broken, good]):
            self.assertEqual(workers._working_codex(), good)


class RetiredCommandTest(TempState):
    """The per-command files became arguments of one skill: `/xsm list` in
    Claude Code, `$xsm list` in Codex (2026-09-27). What earlier versions wrote
    into a home has to go, or a session keeps offering commands nobody
    maintains — in Codex, eight skills beside `xsm`."""

    def _marked(self, path, text):
        from xsm import install
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text + install.FILE_MARKER + "\n")

    def test_install_clears_what_earlier_versions_wrote_and_nothing_else(self):
        from xsm import cli, install
        home = os.path.join(self.tmp, "claude-home")
        old = os.path.join(home, "commands", "xsm-list.md")
        self._marked(old, "---\ndescription: old\n---\nbody\n")
        mine = os.path.join(home, "commands", "xsm-mine.md")
        with open(mine, "w") as fh:
            fh.write("---\ndescription: someone else's\n---\nkeep me\n")
        self.assertEqual(install.retired_commands(home), [old])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["install", "--claude-home", home, "--no-mcp", "--python", sys.executable])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(mine), "only files carrying our marker are ours")
        self.assertTrue(os.path.islink(os.path.join(home, "skills", "xsm")))

    def test_a_codex_command_skill_goes_and_the_main_skill_stays(self):
        from xsm import install
        home = os.path.join(self.tmp, "codex-home")
        stale = os.path.join(home, "skills", "xsm-list")
        self._marked(os.path.join(stale, "SKILL.md"), "---\nname: xsm-list\n---\nold\n")
        theirs = os.path.join(home, "skills", "xsm-theirs")
        os.makedirs(theirs)
        with open(os.path.join(theirs, "SKILL.md"), "w") as fh:
            fh.write("---\nname: xsm-theirs\n---\nnot ours\n")
        install.install_skill(home)
        self.assertEqual(install.remove_retired(home), [stale])
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(theirs))
        self.assertTrue(os.path.islink(os.path.join(home, "skills", "xsm")))

    def test_a_link_is_never_removed_whatever_it_points_at(self):
        """A link is someone's own arrangement; the old cleanup unlinked one on
        the Claude side and reported a failed rmtree as removed on the Codex side."""
        from xsm import install
        home = os.path.join(self.tmp, "linked-home")
        real = os.path.join(self.tmp, "elsewhere")
        self._marked(os.path.join(real, "xsm-list.md"), "body\n")
        self._marked(os.path.join(real, "xsm-who", "SKILL.md"), "body\n")
        os.makedirs(os.path.join(home, "commands"))
        os.makedirs(os.path.join(home, "skills"))
        os.symlink(os.path.join(real, "xsm-list.md"), os.path.join(home, "commands", "xsm-list.md"))
        os.symlink(os.path.join(real, "xsm-who"), os.path.join(home, "skills", "xsm-who"))
        self.assertEqual(install.retired_commands(home), [])
        self.assertEqual(install.remove_retired(home), [])
        self.assertTrue(os.path.islink(os.path.join(home, "commands", "xsm-list.md")))
        self.assertTrue(os.path.islink(os.path.join(home, "skills", "xsm-who")))

    def test_refresh_clears_them_on_a_plugin_home_too(self):
        """A plugin home is skipped by --refresh, but a copy an earlier direct
        install left there is still ours to clear."""
        from xsm import cli, config, install, paths
        home = os.path.join(self.tmp, "plugin-home")
        old = os.path.join(home, "commands", "xsm-who.md")
        self._marked(old, "body\n")
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [{"version": "0.4.0"}]}})
        config.add_home(home, "claude")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["install", "--refresh"])
        self.assertFalse(os.path.exists(old))
        self.assertIn("xsm-who.md", out.getvalue())

    def test_refresh_does_not_bring_back_a_home_its_user_deleted(self):
        """Two profiles moved to the trash came back two minutes later as empty
        homes with hooks in them (2026-09-27)."""
        from xsm import cli, config
        home = os.path.join(self.tmp, "deleted-home")
        config.add_home(home, "claude")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["install", "--refresh"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(home))
        self.assertIn("gone; skipped", out.getvalue())
        from xsm import install
        report = install.doctor()
        self.assertEqual(report["gone"], [os.path.realpath(home)])
        self.assertEqual(report["installs"], [])


class WorkerPolicyTest(TempState):
    """The rule was widened three times in one day, each time after a worker
    waited on a question nobody was there to answer. One declaration now."""

    def test_both_runtimes_are_configured_from_the_one_declaration(self):
        import json as _json
        from xsm import workers
        os.makedirs(os.path.join(self.tmp, "workers", "w"), exist_ok=True)
        settings = _json.load(open(workers._claude_worker_settings(
            {"name": "w", "mode": "background", "cwd": self.tmp, "runtime": "claude",
             "approval_timeout": 5})))
        allowed = settings["permissions"]["allow"]
        for tool in workers.CLAUDE_WORKER_TOOLS:
            self.assertIn(tool, allowed)
        for tool in workers.CLAUDE_WORKER_MCP:
            self.assertIn("mcp__xsm__%s" % tool, allowed)
        self.assertEqual(len(allowed), len(workers.CLAUDE_WORKER_TOOLS) +
                         len(workers.CLAUDE_WORKER_MCP), "nothing allowed off the declaration")
        self.assertEqual(sorted(workers.WORKER_POLICY),
                         sorted(["shell", "read", "write", "reach", "ask"]))

    def test_the_policy_can_be_read_from_the_command_line(self):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["workers", "--policy"])
        text = out.getvalue()
        self.assertIn("without asking", text)
        self.assertIn("xsm_inbox", text)
        self.assertIn("workspace-write", text)


class ShippedFilesTest(TempState):
    def test_the_release_archive_leaves_development_out(self):
        """What ships is the plugin; tests, tools and spike notes are how the
        project is developed (2026-09-23)."""
        rules = open(os.path.join(REPO, ".gitattributes")).read()
        for path in ("tests/", "tools/", "docs/spikes/", "docs/reviews/"):
            self.assertRegex(rules, r"%s\s+export-ignore" % re.escape(path))
        for path in ("xsm", "hooks", "bin", "skills", ".claude-plugin"):
            self.assertNotIn("\n%s/ " % path, rules, "%s has to ship" % path)


class InstallRefreshTest(TempState):
    """Two profiles ran a skill eight versions behind for a day because the
    copies xsm wrote were never compared or refreshed (2026-09-23)."""

    def _home(self):
        home = os.path.join(self.tmp, "claude-home")
        os.makedirs(os.path.join(home, "commands"), exist_ok=True)
        return home

    def test_a_copy_that_fell_behind_is_reported_and_refreshed(self):
        from xsm import install
        home = self._home()
        skills = os.path.join(home, "skills", "xsm")
        os.makedirs(skills)
        with open(os.path.join(skills, "SKILL.md"), "w") as fh:
            fh.write("---\nname: xsm\n---\nan old copy\n")
        self.assertEqual(install.stale_copies(home), [skills])
        self.assertEqual(install.install_skill(home, refresh=True)[0], "copy-current")
        self.assertEqual(install.stale_copies(home), [])

    def test_a_home_with_the_plugin_is_seen(self):
        from xsm import install, paths
        home = self._home()
        self.assertIsNone(install.plugin_installed(home))
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [{"scope": "user",
                                                                 "version": "0.2.0"}]}})
        self.assertEqual(install.plugin_installed(home), "0.2.0")

    def test_installing_over_a_plugin_is_refused_so_hooks_do_not_run_twice(self):
        from xsm import cli, install, paths
        home = self._home()
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [{"version": "0.2.0"}]}})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["install", "--claude-home", home])
        self.assertNotEqual(code, 0)
        self.assertIn("run every hook twice", err.getvalue())
        self.assertIsNone(install._read_text(os.path.join(home, "settings.json")))


class LeftoverTest(TempState):
    """A home that moves from `xsm install` to the plugin keeps the files the
    install wrote, and a stale copy of them reads as ours (2026-09-23)."""

    def test_a_home_on_the_plugin_reports_leftovers_instead_of_staleness(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-home")
        os.makedirs(home)
        install.install_skill(home)
        self.assertEqual(install.leftovers(home), [], "not on the plugin yet")
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [{"version": "0.3.1"}]}})
        self.assertEqual(install.leftovers(home), [os.path.join(home, "skills", "xsm")],
                         "a personal skills/xsm takes the bare /xsm from the plugin")
        self.assertEqual(install.stale_copies(home), [], "the plugin keeps itself current")
        self.assertTrue(install.remove_skill(home))
        self.assertEqual(install.leftovers(home), [])

    def test_a_copied_skill_is_removed_on_uninstall_too(self):
        from xsm import install
        home = os.path.join(self.tmp, "claude-copy")
        skills = os.path.join(home, "skills", "xsm")
        os.makedirs(skills)
        with open(os.path.join(skills, "SKILL.md"), "w") as fh:
            fh.write("---\nname: xsm\n---\nour copy\n")
        self.assertTrue(install.remove_skill(home))
        self.assertFalse(os.path.exists(skills))

    def test_someone_elses_skill_of_the_same_name_survives(self):
        from xsm import install
        home = os.path.join(self.tmp, "claude-foreign")
        skills = os.path.join(home, "skills", "xsm")
        os.makedirs(skills)
        with open(os.path.join(skills, "SKILL.md"), "w") as fh:
            fh.write("---\nname: xsm-theirs\n---\nnot ours\n")
        self.assertFalse(install.remove_skill(home))
        self.assertTrue(os.path.exists(skills))


class StuckReportTest(TempState):
    """`xsm doctor` said nothing while a worker waited ten minutes on a
    permission and two messages sat queued to a closed Codex thread."""

    def test_doctor_names_what_is_waiting_on_someone(self):
        from xsm import install, ledger, paths, registry
        paths.write_json(paths.path("approvals", "a1.json"),
                         {"id": "a1", "worker": "w1", "tool": "Read", "status": "pending",
                          "t": time.time() - 300})
        a = {"name": "a", "alias": "h", "ref": "r1", "runtime": "claude"}
        b = {"name": "b", "alias": "h", "ref": "r2", "runtime": "codex"}
        ledger.queued("m1", a, b, "dir:x", "note", "hello")
        entry = paths.read_json(paths.path(paths.LEDGER, "m1.json")) or {}
        entry["t"] = time.time() - 600
        paths.write_json(paths.path(paths.LEDGER, "m1.json"), entry)
        ledger.queued("m2", a, b, "dir:x", "note", "boom")
        ledger.failed("m2", "sandbox-blocked: Operation not permitted")
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        rec = registry.upsert("codex", home, "t-gone", os.getpid(), self.tmp, name="left")
        rec["end_reason"] = "thread_replaced"
        paths.write_json(registry._record_path("codex", "t-gone"), rec)

        stuck = install.stuck()
        self.assertEqual(stuck["approvals"][0]["worker"], "w1")
        self.assertGreaterEqual(stuck["approvals"][0]["waiting_s"], 300)
        self.assertEqual([r["id"] for r in stuck["undelivered"]], ["m1"])
        self.assertEqual(stuck["send_failures"], {"sandbox-blocked": 1})
        self.assertEqual([t["name"] for t in stuck["threads_replaced"]], ["left"])
        lines = "\n".join(cli_stuck_lines(stuck))
        self.assertIn("has waited", lines)
        self.assertIn("still queued", lines)
        self.assertIn("sandbox-blocked", lines)


def cli_stuck_lines(stuck):
    from xsm import cli
    return cli._stuck_lines(stuck)


class SuggestionTest(TempState):
    """A name that matches nothing comes back with what does exist, so a caller
    working from a stale list does not have to fetch one."""

    def test_unknown_name_lists_registered_sessions(self):
        from xsm import paths, registry, resolve
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        found = resolve.resolve("nobody-here")
        self.assertEqual(found.status, "not-found")
        self.assertEqual([c["name"] for c in found.candidates], ["worker"])
        self.assertIn("worker@codex", resolve.describe(found.candidates))

    def test_unknown_ref_also_suggests(self):
        from xsm import registry, resolve
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        self.assertEqual(resolve.resolve("ref:zzzzzz").candidates[0]["name"], "worker")


class HeldRecordTest(TempState):
    """A refused message must say where it came from, even when it carried no
    xsm header — an injection's only trace is the envelope's reply address."""

    def test_injection_records_its_reply_address(self):
        from xsm import envelope, paths, receive
        receive.register = lambda data, runtime: None
        prompt = ('<%s from="uds:/tmp/cc-socks/999.sock" from-mode="prompting">\nraw\n</%s>'
                  % (envelope.TAG, envelope.TAG))
        receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "r1",
                        "cwd": self.tmp, "prompt": prompt, "session_title": "recv"})
        held = os.listdir(paths.path(paths.HELD))
        self.assertEqual(len(held), 1)
        record = paths.read_json(paths.path(paths.HELD, held[0]))
        self.assertEqual(record["from"], "uds:/tmp/cc-socks/999.sock")
        self.assertEqual(record["body"], "raw")


class CodexReceiverNameTest(TempState):
    """A Codex thread's name lives in its state DB, not in the hook input, so
    the hook must read it back before recording who received a message.
    Observed: decisions for a named Codex receiver said `receiver: null`."""

    def test_held_record_names_the_codex_receiver(self):
        import sqlite3
        from xsm import envelope, paths, receive, registry
        home = os.path.join(self.tmp, "codex-named")
        os.makedirs(home)
        con = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        con.execute("create table threads (id text, name text)")
        con.execute("insert into threads values ('t1', 'reviewer')")
        con.commit()
        con.close()
        receive.register = lambda data, runtime: registry.upsert(
            "codex", home, "t1", os.getpid(), self.tmp)
        prompt = "<%s>\nraw\n</%s>" % (envelope.TAG, envelope.TAG)
        receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "t1",
                        "cwd": self.tmp, "prompt": prompt,
                        "transcript_path": home + "/sessions/2026/t1.jsonl"})
        held = os.listdir(paths.path(paths.HELD))
        record = paths.read_json(paths.path(paths.HELD, held[0]))
        self.assertEqual(record["receiver"], "reviewer")


class SessionFolderTest(TempState):
    def test_a_claude_session_stays_in_the_folder_it_started_in(self):
        """A `cd` in its Bash tool changed the hook's cwd, and `xsm who` put
        the session in that subfolder (2026-09-23)."""
        from unittest import mock
        from xsm import receive
        data = {"cwd": os.path.join(self.tmp, "sub")}
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.tmp}):
            self.assertEqual(receive.session_folder("claude", data), self.tmp)
            self.assertEqual(receive.session_folder("codex", data), data["cwd"],
                             "Codex's cwd already is its start folder")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            self.assertEqual(receive.session_folder("claude", data), data["cwd"])


class CodexThreadLivenessTest(TempState):
    """A Codex TUI keeps its process while it changes thread (/new, resume);
    the old thread shuts down and reads its queue no more. 2026-09-23: two
    messages sat queued to codex-orch's closed thread, reported live, for an
    hour. The xsm MCP server is started per thread and stopped with it, so
    its pid is the thread's liveness."""

    def _beacon(self, pid, ppid, started=None):
        from xsm import paths
        paths.write_json(paths.path(paths.MCP, "%d.json" % pid),
                         {"pid": pid, "ppid": ppid, "started": started or time.time(),
                          "cwd": self.tmp})

    def test_a_thread_whose_mcp_server_is_gone_is_ended_not_live(self):
        from xsm import identity, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        with mock.patch.object(identity, "lstart", lambda pid: None):
            registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="orch",
                            mcp_pid=os.getpid())
            rec = registry.by_session("codex", "t1") or {}
            self.assertEqual(rec["state"], "live")
            registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="orch",
                            mcp_pid=999999)
            rec = registry.by_session("codex", "t1") or {}
        self.assertEqual(rec["state"], "ended")
        self.assertEqual(rec["end_reason"], "thread_replaced")
        self.assertIn("opened another thread", resolve_hint(rec))

    def test_a_resume_after_options_is_still_a_resume(self):
        """Issue #5: `codex --no-alt-screen resume <id>` read as a fresh TUI,
        which was then handed the newest thread in its folder."""
        from xsm import registry
        cases = {("codex", "resume", "T1"): "T1",
                 ("codex", "--no-alt-screen", "resume", "T1"): "T1",
                 ("codex", "-c", "k=v", "resume", "T1", "--yolo"): "T1",
                 ("codex", "resume"): "?",
                 ("codex", "resume", "--last"): "?",
                 ("codex",): None,
                 ("codex", "--no-alt-screen"): None}
        for argv, want in cases.items():
            self.assertEqual(registry._resumed_thread(list(argv)), want, argv)

    def test_the_app_server_daemon_is_told_apart_from_a_tui(self):
        from xsm import identity
        daemon = "/h/.codex/packages/app-server-daemon/releases/0.158.0/bin/codex app-server " \
                 "--listen unix:// --managed-daemon\n"
        for args, want in ((daemon, True), ("/usr/bin/codex --no-alt-screen resume T1\n", False),
                           ("/usr/bin/codex app-server daemon pid-update-loop\n", False),
                           # A per-app stdio server (desktop app, IDE) is not the
                           # daemon whose loaded list we ask (review, 2026-09-29).
                           ("/Applications/Codex.app/bin/codex app-server --listen stdio://\n", False),
                           ("/usr/bin/codex app-server\n", False),
                           ("python3 app-server\n", False), ("", False)):
            with mock.patch.object(identity.subprocess, "run",
                                   lambda *a, **k: mock.Mock(stdout=args)):
                self.assertEqual(identity.is_app_server(4242), want, args)

    def test_a_thread_registered_under_the_daemon_is_marked_hosted(self):
        from xsm import identity, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        with mock.patch.object(identity, "is_app_server", lambda pid: True):
            rec = registry.upsert("codex", home, "t1", os.getpid(), self.tmp)
        self.assertTrue(rec.get("app_server"))
        with mock.patch.object(identity, "is_app_server", lambda pid: False):
            rec = registry.upsert("codex", home, "t1", os.getpid(), self.tmp)
        self.assertIs(rec.get("app_server"), False,
                      "a TUI pid says so outright, so only legacy records need the probe")

    def test_the_hook_records_the_newest_mcp_server_under_its_codex(self):
        from xsm import receive, registry
        self._beacon(4001, os.getpid(), started=100)
        self._beacon(4002, os.getpid(), started=200)          # the thread open now
        self._beacon(4003, 1, started=300)                    # someone else's TUI
        with mock.patch.object(registry.identity, "pid_alive", lambda pid: True), \
                mock.patch.object(receive, "pid_of", lambda runtime: os.getpid()), \
                mock.patch.object(receive, "home_of", lambda r, d: self.tmp):
            rec = receive.register({"session_id": "t9", "cwd": self.tmp}, "codex") or {}
        self.assertEqual(rec["mcp_pid"], 4002)

    def test_a_fresh_thread_shows_in_the_list_as_not_yet_addressable(self):
        """Codex writes nothing about a thread before its first prompt; its
        MCP servers are the only sign. The list says so instead of nothing."""
        from xsm import cli, registry
        self._beacon(os.getpid(), os.getpid())        # both alive: this process
        with mock.patch.object(registry.identity, "comm", lambda pid: "codex"):
            rows = registry.fresh_codex_threads()
            self.assertEqual(len(rows), 1)
            self.assertIn("nobody has typed in", rows[0]["why"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["list", "--compact", "--dir", self.tmp])
        self.assertIn("codex-%d@codex [-]" % os.getpid(), out.getvalue())
        self.assertIn("nobody has typed in", out.getvalue())

    def test_a_dead_beacon_is_dropped(self):
        from xsm import paths, registry
        self._beacon(999999, os.getpid())
        self.assertEqual(registry.mcp_beacons(), [])
        self.assertFalse(os.path.exists(paths.path(paths.MCP, "999999.json")))

    def test_the_mcp_server_writes_its_beacon_while_it_serves(self):
        from xsm import mcp, paths
        with mock.patch.object(mcp.Server, "serve", lambda self: 0), \
                mock.patch.object(mcp.identity, "lstart", lambda pid: None):
            real_write = mcp.write_beacon

            def write_and_check():
                real_write()
                self.assertTrue(os.path.exists(paths.path(paths.MCP, "%d.json" % os.getpid())))
            with mock.patch.object(mcp, "write_beacon", write_and_check), \
                    mock.patch.dict(os.environ, {"XSM_SANDBOXED": "1"}):
                self.assertEqual(mcp.main(), 0)
                self.assertNotIn("XSM_SANDBOXED", os.environ, "the MCP server is outside the sandbox")
        self.assertFalse(os.path.exists(paths.path(paths.MCP, "%d.json" % os.getpid())),
                         "removed when the server stops")


class UnpromptedCodexThreadTest(TempState):
    """A Codex TUI writes no thread row, rollout or hook call before the first
    prompt, and `codex queue` refuses such a thread ("no rollout found"). Its
    log database names the thread within seconds, and a queue row written
    directly is taken by the TUI like any other (measured 2026-09-23)."""

    def _codex_home(self):
        import sqlite3
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home, exist_ok=True)
        con = sqlite3.connect(os.path.join(home, "queue_1.sqlite"))
        con.execute("create table queued_items (id text primary key not null, thread_id text "
                    "not null, payload_json text not null, queue_order integer not null, "
                    "created_at_ms integer not null, updated_at_ms integer not null)")
        con.commit()
        con.close()
        con = sqlite3.connect(os.path.join(home, "logs_2.sqlite"))
        con.execute("create table logs (id integer primary key, ts integer, thread_id text, "
                    "process_uuid text)")
        con.executemany("insert into logs (ts, thread_id, process_uuid) values (?, ?, ?)", [
            (1000, "", "pid:77:a"),
            (1003, "01a0ca1b-aaaa-7000-8000-000000000001", "pid:77:a"),    # opened with the TUI
            (1500, "01a0ca1b-bbbb-7000-8000-000000000002", "pid:77:a"),    # a side thread later
            (1004, "01a0ca1b-cccc-7000-8000-000000000003", "pid:78:b"),    # another TUI
        ])
        con.commit()
        con.close()
        return home

    def test_the_thread_a_process_opened_is_read_from_codex_logs(self):
        from xsm import adapters
        home = self._codex_home()
        self.assertEqual(adapters.thread_of_process(home, 77, 1001),
                         "01a0ca1b-aaaa-7000-8000-000000000001")
        self.assertEqual(adapters.thread_of_process(home, 77, 1498),
                         "01a0ca1b-bbbb-7000-8000-000000000002", "after /new: the newer one")
        self.assertIsNone(adapters.thread_of_process(home, 79, 1001))

    def test_a_refused_codex_queue_falls_back_to_the_row_it_would_write(self):
        import sqlite3
        from xsm import adapters
        home = self._codex_home()
        failed = mock.Mock(returncode=1, stdout="", stderr=(
            "Error: failed to queue session message: thread/queue/add failed: failed to read "
            "thread: invalid thread-store request: no rollout found for thread id T1"))
        with mock.patch.object(adapters.subprocess, "run", lambda *a, **k: failed), \
                mock.patch.object(adapters, "codex_bin", lambda: "/bin/codex"):
            adapters.to_codex(home, "T1", "hello")
            adapters.to_codex(home, "T1", "again")
        con = sqlite3.connect(os.path.join(home, "queue_1.sqlite"))
        rows = con.execute("select thread_id, payload_json, queue_order from queued_items "
                           "order by queue_order").fetchall()
        con.close()
        self.assertEqual([(r[0], r[2]) for r in rows], [("T1", 1), ("T1", 2)])
        payload = json.loads(rows[0][1])["UserInput"]
        self.assertEqual(payload["content"], [{"type": "text", "text": "hello",
                                               "text_elements": []}])
        self.assertTrue(payload["client_id"])

    def test_a_changed_codex_queue_table_is_refused_not_guessed_at(self):
        import sqlite3
        from xsm import adapters
        home = os.path.join(self.tmp, "codex-new")
        os.makedirs(home)
        con = sqlite3.connect(os.path.join(home, "queue_2.sqlite"))
        con.execute("create table queued_items (id text, thread_id text, body text)")
        con.commit()
        con.close()
        with self.assertRaises(adapters.DeliveryError) as cm:
            adapters.queue_direct(home, "T1", "hello")
        self.assertEqual(cm.exception.reason, "codex-internal-changed")

    def test_other_codex_queue_failures_are_not_rerouted(self):
        from xsm import adapters
        home = self._codex_home()
        failed = mock.Mock(returncode=1, stdout="", stderr="Error: readonly database")
        with mock.patch.object(adapters.subprocess, "run", lambda *a, **k: failed), \
                mock.patch.object(adapters, "codex_bin", lambda: "/bin/codex"), \
                mock.patch.object(adapters, "queue_direct") as direct:
            with self.assertRaises(adapters.DeliveryError):
                adapters.to_codex(home, "T1", "hello")
        direct.assert_not_called()

    def test_threads_opened_minutes_apart_get_different_default_names(self):
        from xsm import registry
        a = registry.default_codex_name("01a0ca1b-e3f1-7a22-9b01-4c5d6e7f8a90")
        b = registry.default_codex_name("01a0ca1b-f402-7c11-8d23-1a2b3c4d5e6f")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "codex-7f8a90")


def resolve_hint(record):
    from xsm import resolve
    return resolve.resume_hint(record)


class SandboxedSendTest(TempState):
    def test_a_sandboxed_shell_is_told_to_use_mcp_before_trying_codex_queue(self):
        """Every S10 worker lost one attempt to `codex queue` failing inside
        its sandbox before falling back to the MCP tool."""
        from xsm import adapters, ledger, registry, send
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        a = registry.upsert("codex", home, "t-a", os.getpid(), self.tmp, name="a")
        registry.upsert("codex", home, "t-b", os.getpid(), self.tmp, name="b")
        tried = []
        adapters.to_codex = lambda *args: tried.append(args)
        with mock.patch.dict(os.environ, {"CODEX_SANDBOX": "seatbelt"}):
            r = send.send("b", "hi", sender=a)
        self.assertEqual(r.status, "refused")
        self.assertIn("xsm_send MCP tool", r.reason)
        self.assertEqual(tried, [], "codex queue was not even tried")
        self.assertEqual(ledger.recent(), [], "nothing was recorded as queued")
        with mock.patch.dict(os.environ, {"XSM_SANDBOXED": "1"}):
            self.assertEqual(send.send("b", "hi", sender=a).status, "refused")
        self.assertEqual(send.send("b", "hi", sender=a).status, "sent-unconfirmed")


class CodexInboxTest(TempState):
    """A Codex session takes its queue only between turns. In S10 collab run 4
    the Codex worker never ended one (it polled with sleep) and read none of
    the six messages sent to it. `xsm inbox` hands them over mid-turn."""

    def setUp(self):
        super().setUp()
        from xsm import adapters, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        self.a = registry.upsert("codex", home, "t-a", os.getpid(), self.tmp, name="a")
        self.b = registry.upsert("codex", home, "t-b", os.getpid(), self.tmp, name="b")
        self.queued = []
        adapters.to_codex = lambda home, thread, content: self.queued.append(content) or "ok"

    def _send(self, text="hello"):
        from xsm import send
        return send.send("b", text, sender=self.a)

    def test_a_message_waits_and_is_read_once(self):
        from xsm import inbox, ledger, receive
        r = self._send("phase 2 started")
        self.assertEqual(r.status, "sent-unconfirmed")
        self.assertEqual(inbox.count("t-b"), 1)
        texts = receive.take_inbox(self.b)
        self.assertEqual(len(texts), 1)
        self.assertIn("phase 2 started", texts[0])
        self.assertIn("another agent session", texts[0], "the same framing the hook adds")
        self.assertEqual(ledger.status(r.msg_id or "")["status"], "delivered")
        self.assertEqual(receive.take_inbox(self.b), [], "taken once")

    def test_the_queue_copy_arriving_later_is_dropped_not_held(self):
        from xsm import paths, receive
        self._send()
        receive.take_inbox(self.b)
        receive.register = lambda data, runtime: self.b
        out = receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "t-b",
                              "turn_id": "x", "cwd": self.tmp, "prompt": self.queued[0]})
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["decision"], "block", "refusing is what drops it from Codex")
        self.assertIn("already received", out["reason"])
        self.assertEqual(os.listdir(paths.path(paths.HELD)), [], "the session has it; not held")

    def test_a_message_the_hook_delivered_is_gone_from_the_inbox(self):
        from xsm import inbox, receive
        self._send()
        receive.register = lambda data, runtime: self.b
        out = receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "t-b",
                              "turn_id": "x", "cwd": self.tmp, "prompt": self.queued[0]})
        self.assertNotEqual((out or {}).get("decision"), "block")
        self.assertEqual(inbox.count("t-b"), 0)
        self.assertEqual(receive.take_inbox(self.b), [])

    def test_the_inbox_runs_the_same_checks_as_the_hook(self):
        from xsm import config, ledger, receive
        r = self._send()
        config.blocked = lambda: {self.a["ref"]}
        texts = receive.take_inbox(self.b)
        self.assertEqual(len(texts), 1)
        self.assertIn("refused", texts[0])
        self.assertNotIn("hello", texts[0])
        self.assertEqual(ledger.status(r.msg_id or "")["status"], "held")

    def test_a_failed_send_leaves_no_copy(self):
        from xsm import adapters, inbox

        def refuse(home, thread, content):
            raise adapters.DeliveryError("sandbox-blocked", "Operation not permitted")
        adapters.to_codex = refuse
        self.assertEqual(self._send().status, "error")
        self.assertEqual(inbox.count("t-b"), 0)

    def test_waiting_returns_the_moment_a_message_lands(self):
        """The alternative is what a worker actually did: sleep in a loop,
        which never ends the turn, which is why the queue never arrived."""
        import threading
        from xsm import inbox
        threading.Timer(0.3, self._send, ["late one"]).start()
        started = time.time()
        self.assertEqual(inbox.wait_for("t-b", 10, interval=0.05), 1)
        self.assertLess(time.time() - started, 5, "it does not sit out the deadline")

    def test_waiting_takes_nothing_so_the_message_is_still_there(self):
        from xsm import inbox, receive
        self._send("keep me")
        self.assertEqual(inbox.wait_for("t-b", 5, interval=0.05), 1)
        self.assertEqual(len(receive.take_inbox(self.b)), 1, "the wait must not claim it")

    def test_an_expiry_is_not_a_failure_and_says_what_to_do(self):
        from xsm import cli, inbox, registry
        out, err = io.StringIO(), io.StringIO()
        said = io.StringIO()
        self.assertEqual(inbox.wait_for("t-b", 0.2, interval=0.05, out=said), 0)
        self.assertIn("do not sleep-poll", said.getvalue())
        with mock.patch.object(registry, "me", lambda: self.b), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["inbox", "--wait", "0.2"]), 0, "nothing arrived is not an error")
        self.assertIn("(no messages waiting)", out.getvalue())
        self.assertNotIn("waiting for messages", out.getvalue(), "stdout stays the message alone")

    def test_a_long_wait_is_clamped_to_the_cap(self):
        from xsm import cli, inbox, registry
        seen = []
        err = io.StringIO()
        with mock.patch.object(registry, "me", lambda: self.b), \
                mock.patch.object(inbox, "wait_for",
                                  lambda sid, secs, **kw: seen.append(secs) or 0), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            cli.main(["inbox", "--wait", "99999"])
        self.assertIn("clamped", err.getvalue(), "an unbounded wait would be a daemon")
        with mock.patch.object(inbox, "MAX_WAIT", 0.2):
            started = time.time()
            self.assertEqual(inbox.wait_for("nobody", 99999, interval=0.05), 0)
        self.assertLess(time.time() - started, 5, "the cap is what stops it being a daemon")

    def test_the_keepalive_goes_to_stderr(self):
        from xsm import inbox
        said = io.StringIO()
        inbox.wait_for("t-b", 0.3, interval=0.05, keepalive=0.01, out=said)
        self.assertIn("waiting for messages", said.getvalue(),
                      "a silent process is one an agent kills")

    def test_every_xsm_command_tells_a_codex_session_what_is_waiting(self):
        from xsm import cli
        self._send()
        self.addCleanup(lambda v=os.environ.get("CODEX_THREAD_ID"):
                        os.environ.__setitem__("CODEX_THREAD_ID", v) if v is not None
                        else os.environ.pop("CODEX_THREAD_ID", None))
        os.environ["CODEX_THREAD_ID"] = "t-b"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["ledger", "--compact"])
        self.assertIn("1 message from other sessions waiting", err.getvalue())
        self.assertIn("xsm inbox", err.getvalue())


class ProjectJoinTest(TempState):
    """Sessions in different projects talk once both projects have joined the
    same xsm project by name. One side joining is not enough: that would let a
    project pull another in without its consent."""

    def _dirs(self):
        a, b = os.path.join(self.tmp, "proj-a"), os.path.join(self.tmp, "proj-b")
        os.makedirs(os.path.join(a, "sub"))
        os.makedirs(b)
        return a, b

    def test_both_sides_must_join(self):
        from xsm import config
        a, b = self._dirs()
        sa, sb = {"cwd": os.path.join(a, "sub")}, {"cwd": b}
        self.assertIsNone(config.scope_for(sa, sb)[0])
        config.join("demo", a)
        scope, reason = config.scope_for(sa, sb)
        self.assertIsNone(scope, "one side alone must not open it")
        self.assertIn("%s has not joined project demo" % os.path.realpath(b), reason)
        config.join("demo", b)
        self.assertEqual(config.scope_for(sa, sb)[0], "demo")

    def test_named_project_is_joined_in_addition_to_the_default(self):
        from xsm import config
        import subprocess
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "sub"))
        subprocess.run(["git", "init", "-q", repo], check=True)
        _, other = self._dirs()
        config.join("demo", repo)
        config.join("demo", other)
        inside = config.scope_for({"cwd": repo}, {"cwd": os.path.join(repo, "sub")})
        self.assertEqual(inside[0], "repo:repo", "joining must not change the default scope")
        self.assertEqual(config.scope_for({"cwd": repo}, {"cwd": other})[0], "demo")
        self.assertEqual(config.default_project(other), ("dir:proj-b", os.path.realpath(other)))

    def test_join_is_idempotent_and_leave_closes_it(self):
        from xsm import config
        a, b = self._dirs()
        self.assertTrue(config.join("demo", a)[1])
        self.assertFalse(config.join("demo", a)[1])
        config.join("demo", b)
        self.assertTrue(config.leave("demo", b))
        self.assertIsNone(config.scope_for({"cwd": a}, {"cwd": b})[0])
        self.assertTrue(config.leave("demo", a))
        self.assertEqual(config.projects(), [])

    def test_root_does_not_match_a_sibling_prefix(self):
        from xsm import config
        a, _ = self._dirs()
        self.assertFalse(config.member_matches({"root": a}, {"cwd": a + "-other"}))

    def test_bad_name_and_hand_written_scope_are_refused(self):
        from xsm import config, paths
        a, _ = self._dirs()
        with self.assertRaises(ValueError):
            config.join("../x", a)
        paths.write_json(paths.path(config.CONFIG), {"scopes": [
            {"id": "manual", "members": [{"cwd": "/somewhere/*"}]}]}, mode=0o644)
        with self.assertRaises(ValueError):
            config.join("manual", a)


class DefaultHomePrefixTest(TempState):
    """Whether the installing shell exports XSM_HOME=~/.xsm must not change the
    hook command, and an older install carrying that prefix is kept as is."""

    def test_default_home_is_not_written_and_old_prefix_is_kept(self):
        from unittest import mock
        from xsm import install
        with mock.patch.dict(os.environ, {"XSM_HOME": os.path.expanduser("~/.xsm")}):
            plain = install.hook_command("claude", "SessionStart")
        self.assertFalse(plain.startswith("XSM_HOME="))
        old = "XSM_HOME=%s %s" % (os.path.expanduser("~/.xsm"), plain)
        self.assertTrue(install._same_command(old, plain))
        self.assertFalse(install._same_command("XSM_HOME=/elsewhere " + plain, plain))


class CommandInstallTest(TempState):
    """The skill goes in with the hooks and comes out again without touching
    anything we did not write."""

    def _home(self):
        home = os.path.join(self.tmp, "claude-cmd")
        os.makedirs(home, exist_ok=True)
        return home

    def test_the_skill_is_linked_and_unlinked(self):
        from xsm import install
        home = self._home()
        self.assertEqual(install.install_skill(home)[0], "linked")
        link = os.path.join(home, "skills", "xsm")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(install.install_skill(home)[0], "linked")      # idempotent
        self.assertTrue(install.remove_skill(home))
        self.assertFalse(os.path.exists(link))

    def test_a_foreign_skill_directory_is_left_alone(self):
        from xsm import install
        home = self._home()
        existing = os.path.join(home, "skills", "xsm")
        os.makedirs(existing)
        self.assertEqual(install.install_skill(home)[0], "foreign")
        self.assertFalse(install.remove_skill(home))
        self.assertTrue(os.path.isdir(existing))

    def test_a_copy_is_reported_as_current_or_stale(self):
        """Following an older instruction left people with a copied SKILL.md;
        install has to say when that copy has fallen behind. The copy is the
        whole directory now: a SKILL.md without references/guide.md points the
        model at a file that is not there."""
        from xsm import install
        home = self._home()
        skill_dir = os.path.join(home, "skills", "xsm")
        shutil.copytree(os.path.join(install.REPO, "skills", "xsm"), skill_dir)
        self.assertEqual(install.skill_state(home)[0], "copy-current")
        guide = os.path.join(skill_dir, "references", "guide.md")
        open(guide, "a").write("\nstale line\n")
        state, detail = install.skill_state(home)
        self.assertEqual(state, "copy-stale")
        self.assertEqual(detail, skill_dir)

    def test_refresh_brings_a_skill_md_only_copy_up_to_date(self):
        """~/.claude-4 and ~/.codex-3 held SKILL.md alone; refresh has to add
        references/ rather than call the copy current."""
        from xsm import install
        home = self._home()
        skill_dir = os.path.join(home, "skills", "xsm")
        os.makedirs(skill_dir)
        shutil.copy2(os.path.join(install.REPO, "skills", "xsm", "SKILL.md"),
                     os.path.join(skill_dir, "SKILL.md"))
        self.assertEqual(install.skill_state(home)[0], "copy-stale")
        self.assertEqual(install.install_skill(home, refresh=True)[0], "copy-current")
        self.assertTrue(os.path.isfile(os.path.join(skill_dir, "references", "guide.md")))
        self.assertEqual(install.skill_state(home)[0], "copy-current")
        self.assertTrue(install.remove_skill(home))
        self.assertFalse(os.path.exists(skill_dir))

    def test_a_link_nested_inside_the_directory_is_reported(self):
        """`ln -sfn repo/skills/xsm <home>/skills/xsm` puts the link *inside* an
        existing directory; the old instructions did exactly that."""
        from xsm import install
        home = self._home()
        skill_dir = os.path.join(home, "skills", "xsm")
        os.makedirs(skill_dir)
        os.symlink(os.path.join(install.REPO, "skills", "xsm"), os.path.join(skill_dir, "xsm"))
        state, detail = install.skill_state(home)
        self.assertEqual(state, "nested-link")
        self.assertTrue(detail.endswith("xsm/xsm"))


class StatuslineTest(TempState):
    """The statusLine path calls no model, so it must be cheap, read the session
    from stdin, and never replace a statusLine the user already has."""

    def _home(self, settings=None):
        from xsm import paths
        home = os.path.join(self.tmp, "claude-sl")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "settings.json"), settings or {}, mode=0o644)
        return home

    def test_output_names_this_session_from_stdin(self):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        env = dict(os.environ, XSM_HOME=self.tmp)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        out = subprocess.run([os.path.join(REPO, "bin", "xsm"), "statusline"],
                             input='{"session_id": "t1"}', capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("as worker", out.stdout)
        self.assertIn("0 peers", out.stdout)

    def test_install_sets_it_and_uninstall_removes_it(self):
        from xsm import install, paths
        home = self._home({"model": "opus"})
        self.assertEqual(install.install_statusline(home), "installed")
        self.assertEqual(install.install_statusline(home), "already")
        self.assertIn("statusline", paths.read_json(os.path.join(home, "settings.json"))["statusLine"]["command"])
        self.assertTrue(install.remove_statusline(home))
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json")), {"model": "opus"})

    def test_an_existing_statusline_is_kept_and_xsm_adds_one_line_under_it(self):
        """A dashboard, Orca's line, anything: its output goes out unchanged and
        xsm adds exactly one line; uninstall gives the original back."""
        from xsm import install, paths
        script = os.path.join(self.tmp, "mine.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nread x\necho \"DASH line1\"\necho \"DASH line2 $x\"\n")
        os.chmod(script, 0o755)
        mine = {"type": "command", "command": script, "refreshInterval": 7, "padding": 1}
        home = self._home({"statusLine": mine})
        self.assertEqual(install.install_statusline(home), "composed")
        now = paths.read_json(os.path.join(home, "settings.json"))["statusLine"]
        self.assertIn("--base", now["command"])
        self.assertEqual((now["refreshInterval"], now["padding"]), (7, 1), "its settings carry over")
        self.assertEqual(install.install_statusline(home), "already")
        env = dict(os.environ, XSM_HOME=self.tmp)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        out = subprocess.run(["/bin/sh", "-c", now["command"].split(" #")[0]],
                             input='{"session_id": "t1"}', capture_output=True, text=True, env=env)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[:2], ["DASH line1", 'DASH line2 {"session_id": "t1"}'],
                         "the user's statusline gets the same input and prints unchanged")
        self.assertEqual(len(lines), 3, "xsm adds one line, no more")
        self.assertTrue(lines[2].startswith("xsm "))
        self.assertTrue(install.remove_statusline(home))
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json"))["statusLine"], mine)

    def test_a_broken_base_statusline_does_not_take_xsms_line_with_it(self):
        from xsm import install, paths
        home = self._home({"statusLine": {"type": "command", "command": "exit 3"}})
        install.install_statusline(home)
        now = paths.read_json(os.path.join(home, "settings.json"))["statusLine"]
        env = dict(os.environ, XSM_HOME=self.tmp)
        out = subprocess.run(["/bin/sh", "-c", now["command"].split(" #")[0]], input="{}",
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith("xsm "))


class CompactOutputTest(TempState):
    """Slash commands have the model copy the CLI output back verbatim, so the
    output is the cost: no alignment padding, home shortened, unregistered and
    stopped sessions left out."""

    def test_list_compact_is_short_and_unpadded(self):
        from xsm import cli, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--compact", "--dir", self.tmp])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].endswith("(here)"), lines)
        self.assertTrue(lines[1].startswith(" worker@codex ["), lines)
        self.assertNotIn(self.tmp, lines[1], "the folder is said once, above its sessions")
        self.assertFalse(any("  " in line for line in lines))

    def test_list_table_is_markdown_the_folder_said_once(self):
        """For the TUI to draw: both Claude Code and Codex render tables."""
        from xsm import cli, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="one|two")
        registry.upsert("codex", home, "t2", os.getpid(), self.tmp, name="three")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--table", "--dir", self.tmp])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "| folder | session | runtime | ref | note |")
        self.assertEqual(lines[1], "|---|---|---|---|---|")
        self.assertEqual([l.split("|")[1].strip() for l in lines[2:]], ["here", ""])
        self.assertIn("one\\|two", out.getvalue(), "a pipe in a name does not split a cell")

    def test_every_display_command_asks_for_a_table_its_output_provides(self):
        """/xsm list, who, log, projects and doctor all pass Markdown tables
        through for the TUI to draw."""
        from unittest import mock
        from xsm import cli, ledger, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        me = registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="me")
        ledger.queued("m1", me, me, "dir:x", "note", "a | b")
        skill = open(os.path.join(REPO, "skills", "xsm", "SKILL.md")).read()
        commands = ["%s --table" % c for c in ("list", "who", "projects", "doctor")] + \
            ["ledger --table --mine --last 5", "held list --table"]
        for command in commands:
            with self.subTest(command=command):
                if command.startswith(("ledger", "held")):
                    self.assertIn("xsm " + command, skill)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        mock.patch.object(registry, "me", lambda: me):
                    cli.main(command.split())
                text = out.getvalue()
                self.assertTrue(text.startswith("| ") or text.startswith("no ")
                                or text.startswith("nothing"), (command, text[:80]))
                self.assertNotIn("```", text)
        self.assertIn("a \\| b", _run_cli(["ledger", "--table"]), "a pipe stays inside its cell")

    def test_tables_stay_narrow_enough_to_stay_tables(self):
        """Past the pane's width Codex draws each row as a stacked card: ten
        ledger rows came out as fifty lines (2026-09-23)."""
        from unittest import mock
        from xsm import install, ledger, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        me = registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="me")
        far = {"name": "x" * 40, "alias": "codex", "ref": "zzzzzz", "runtime": "codex"}
        ledger.queued("m1", me, far, "dir:x", "note", "y" * 200)
        ledger.queued("m2", far, far, "dir:x", "note", "not mine")
        with mock.patch.object(registry, "me", lambda: me):
            text = _run_cli(["ledger", "--table", "--mine"])
        rows = text.splitlines()[2:]
        self.assertEqual(len(rows), 1, "only this session's messages")
        self.assertIn(" | you → xxxxxxxx", rows[0])
        self.assertLess(max(len(line) for line in text.splitlines()), 90)

    def test_list_compact_groups_by_folder_this_one_first(self):
        """A path on every line wrapped each entry in a narrow Codex pane."""
        from xsm import cli, registry
        home = os.path.join(self.tmp, "homes", "codex")
        sub_dir, other = os.path.join(self.tmp, "a", "b"), os.path.join(self.tmp, "..", "zz-other")
        for d in (home, sub_dir):
            os.makedirs(d, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), sub_dir, name="deep")
        registry.upsert("codex", home, "t2", os.getpid(), self.tmp, name="one")
        registry.upsert("codex", home, "t3", os.getpid(), self.tmp, name="two")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--compact", "--dir", self.tmp, "-a"])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].endswith("(here)"), lines)
        self.assertEqual(sorted(l.split("@")[0].strip() for l in lines[1:3]), ["one", "two"])
        self.assertEqual(lines[3], "./a/b")
        self.assertTrue(lines[4].startswith(" deep@codex"), lines)


class ListScopeTest(TempState):
    """`xsm list` shows the sessions this folder can talk to; -a shows all."""

    def _list(self, *argv):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--compact"] + list(argv))
        return out.getvalue()

    def test_default_is_this_project_and_a_shows_everything(self):
        from xsm import config, registry
        here, there = os.path.join(self.tmp, "here"), os.path.join(self.tmp, "there")
        os.makedirs(here)
        os.makedirs(there)
        home = os.path.join(self.tmp, "codex")
        os.makedirs(home)
        registry.upsert("codex", home, "t1", os.getpid(), here, name="near")
        registry.upsert("codex", home, "t2", os.getpid(), there, name="far")
        out = self._list("--dir", here)
        self.assertIn("near@", out)
        self.assertNotIn("far@", out)
        self.assertIn("far@", self._list("--dir", here, "-a"))
        config.join("demo", here)
        config.join("demo", there)
        self.assertIn("far@", self._list("--dir", here), "a joined project counts as this one")


class SupersededSessionTest(TempState):
    def test_an_id_the_process_no_longer_runs_is_ended(self):
        from xsm import registry
        registry.identity.socket_live = lambda path: True
        registry._claude_native = lambda home, pid: {"name": "b", "socket": "/s",
                                                    "name_source": "user",
                                                    "native_session_id": "current"}
        registry.upsert("claude", self.tmp, "current", os.getpid(), self.tmp)
        registry.upsert("claude", self.tmp, "old", os.getpid(), self.tmp)
        states = {r["session_id"]: r["state"] for r in registry.records()}
        self.assertEqual(states, {"current": "live", "old": "ended"})

    def _ledger_line(self):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["ledger", "--compact"])
        return out.getvalue().strip()

    def test_ledger_compact_has_one_line_per_message(self):
        from xsm import ledger, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        b = registry.upsert("codex", home, "t-b", os.getpid(), self.tmp, name="b")   # live target
        a = {"name": "a", "alias": "h", "ref": "r1", "runtime": "claude"}
        ledger.queued("m1", a, b, "dir:x", "note", "hello\nworld")
        self.assertEqual(self._ledger_line(), "queued a->b m1: hello world")

    def test_queued_to_a_stopped_target_reads_undelivered(self):
        """It will never be recorded as delivered; do not leave it looking pending."""
        from xsm import ledger
        a = {"name": "a", "alias": "h", "ref": "r1", "runtime": "claude"}
        gone = {"name": "b", "alias": "h", "ref": "r2", "runtime": "claude"}
        ledger.queued("m1", a, gone, "dir:x", "note", "hello")
        self.assertTrue(self._ledger_line().startswith("undelivered a->b m1"))

    def test_display_commands_ask_for_a_verbatim_copy(self):
        """The prompt must not contain conditions for the model to weigh."""
        skill = open(os.path.join(REPO, "skills", "xsm", "SKILL.md")).read()
        display = skill.split("**Display commands**", 1)[1].split("**`send", 1)[0]
        self.assertIn("copied exactly", display)
        self.assertNotIn("unless", display)


class SelfIdentityTest(TempState):
    """A session asking who it is, before its own hook has ever run.

    Measured 2026-09-22: xsm was installed into a home whose session was
    already open, and /xsm-who — whose shell runs before that prompt's hook —
    said "not registered". The next prompt registered it; the command was just
    too early."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "claude-home")
        os.makedirs(os.path.join(self.home, "sessions"))
        for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MESSAGING_SOCKET"):
            self.addCleanup(lambda k=key, v=os.environ.get(key):
                            os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))
        os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
        os.environ["CLAUDE_CONFIG_DIR"] = self.home
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-mine"
        paths_json = os.path.join(self.home, "sessions", "%d.json" % os.getpid())
        from xsm import paths
        paths.write_json(paths_json, {"pid": os.getpid(), "sessionId": "s-mine", "cwd": self.tmp,
                                      "name": "mine", "nameSource": "auto"})

    def _who(self):
        from xsm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["who"])
        return code, out.getvalue(), err.getvalue()

    def test_installed_but_not_yet_hooked_is_adopted(self):
        from xsm import install, registry
        install.apply(self.home, "claude")
        me = registry.me()
        self.assertIsNotNone(me, "xsm is in this session's own settings: that is the consent")
        self.assertEqual(me["session_id"], "s-mine")
        self.assertTrue(me.get("adopted"))
        code, out, _ = self._who()
        self.assertEqual(code, 0)
        self.assertIn("mine@", out)
        # The real hook, at the next prompt, takes over the record.
        registry.upsert("claude", self.home, "s-mine", os.getpid(), self.tmp)
        self.assertNotIn("adopted", registry.me())

    def test_a_home_without_xsm_is_not_adopted_and_says_how_to_fix_it(self):
        from xsm import registry
        self.assertIsNone(registry.me())
        code, _, err = self._who()
        self.assertEqual(code, 2)
        self.assertIn("xsm install --claude-home", err)

    def test_hooks_removed_from_a_declared_home_is_not_consent(self):
        from xsm import install, registry
        install.apply(self.home, "claude")
        install.remove(self.home, "claude")
        self.assertIsNone(registry.me())
        reason = registry.self_consent(registry.claude_home_here())
        assert reason is not None, "hooks were removed, so there must be a reason"
        self.assertIn("missing", reason)

    def test_a_sandboxed_codex_session_finds_itself_by_thread_id(self):
        """The Codex sandbox refuses `ps`, so the process walk cannot identify a
        session there; two sharing a folder were each refused (S10 smoke run)."""
        from xsm import identity, registry
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.addCleanup(lambda v=os.environ.get("CODEX_THREAD_ID"):
                        os.environ.__setitem__("CODEX_THREAD_ID", v) if v is not None
                        else os.environ.pop("CODEX_THREAD_ID", None))
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home)
        registry.upsert("codex", home, "t-one", os.getpid(), self.tmp, name="one")
        registry.upsert("codex", home, "t-two", os.getpid(), self.tmp, name="two")
        identity.ancestor_pid = lambda names, max_hops=10: None      # what the sandbox does
        os.environ["CODEX_THREAD_ID"] = "t-two"
        self.assertEqual(registry.me()["session_id"], "t-two")
        os.environ["CODEX_THREAD_ID"] = "t-one"
        self.assertEqual(registry.me()["session_id"], "t-one")

    def test_a_codex_carrying_a_claude_sessions_id_still_finds_itself(self):
        """A Codex started from a shell a Claude session made (a tmux server
        first opened there) carries that Claude's session id too, and took the
        Claude session for itself (2026-09-27)."""
        from xsm import identity, registry
        for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            self.addCleanup(lambda k=var, v=os.environ.get(var):
                            os.environ.__setitem__(k, v) if v is not None
                            else os.environ.pop(k, None))
        chome = os.path.join(self.tmp, "homes", "claude")
        xhome = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(chome)
        os.makedirs(xhome)
        identity.pid_alive = lambda pid: True
        identity.lstart = lambda pid: {1111: "Mon Sep 28 09:00:00 2026",
                                       2222: "Mon Sep 28 10:00:00 2026"}.get(pid)
        registry.upsert("claude", chome, "s-claude", 1111, self.tmp, name="builder")
        registry.upsert("codex", xhome, "t-codex", 2222, self.tmp, name="reviewer")
        identity.lstart = lambda pid: None                           # `ps` refused
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-claude"
        os.environ["CODEX_THREAD_ID"] = "t-codex"
        walk = identity.ancestor_pid
        self.addCleanup(setattr, identity, "ancestor_pid", walk)

        identity.ancestor_pid = lambda names, max_hops=10: None      # the Codex sandbox
        # Codex started after that Claude, so it is the one inside.
        self.assertEqual(registry.me()["session_id"], "t-codex")
        identity.ancestor_pid = lambda names, max_hops=10: 2222      # nearest agent is Codex
        self.assertEqual(registry.me()["session_id"], "t-codex")
        identity.ancestor_pid = lambda names, max_hops=10: 1111      # a Claude run under that Codex
        self.assertEqual(registry.me()["session_id"], "s-claude")
        # A hook names its session outright; that is not an inherited guess.
        self.assertEqual(registry._me("s-claude", None)[0]["session_id"], "s-claude")

    def test_a_sandbox_that_forbids_signals_and_ps_still_sees_live_peers(self):
        """From inside the Codex sandbox kill(pid, 0) answers EPERM and `ps`
        cannot run. Both were read as "dead", so `xsm list` there showed no one
        (S10 pilot, seen in xsm's own spans)."""
        from unittest import mock
        from xsm import identity
        rec = {"runtime": "codex", "pid": os.getpid(), "lstart": "Tue Sep 22 11:06:20 2026"}
        with mock.patch("os.kill", side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch.object(identity, "lstart", lambda pid: None):
            self.assertEqual(identity.state_of(rec), "live")
        with mock.patch("os.kill", side_effect=ProcessLookupError(3, "No such process")):
            self.assertEqual(identity.state_of(rec), "stale", "a pid that is gone is still gone")
        with mock.patch.object(identity, "lstart", lambda pid: "Mon Sep 21 09:00:00 2026"):
            self.assertEqual(identity.state_of(rec), "stale", "a measured, different start is reuse")

    def test_a_sandbox_that_forbids_the_inbox_socket_still_sees_live_claude_peers(self):
        """The Codex sandbox refuses connect() on a Claude inbox socket with
        EPERM; that read as a dead socket, and a Codex worker listed both its
        Claude peers as `stale` for a whole run (S10 collab run 4)."""
        import socket
        from unittest import mock
        from xsm import identity
        rec = {"runtime": "claude", "pid": os.getpid(), "socket": "/tmp/cc-socks/1.sock"}
        with mock.patch.object(socket.socket, "connect",
                               side_effect=PermissionError(1, "Operation not permitted")):
            self.assertEqual(identity.state_of(rec), "live")
        with mock.patch.object(socket.socket, "connect",
                               side_effect=ConnectionRefusedError(61, "Connection refused")):
            self.assertEqual(identity.state_of(rec), "stale", "a socket nobody listens on is gone")

    def test_an_unregistered_session_never_borrows_a_neighbours_identity(self):
        """Before: a session that knew its own id but had no record fell through
        to the cwd guess and printed whichever session shared its folder."""
        from xsm import registry
        other = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(other)
        registry.upsert("codex", other, "t-neighbour", os.getpid(), self.tmp, name="neighbour")
        self.assertIsNone(registry.me(), "not someone else's record")


class LedgerFailureTest(TempState):
    def test_a_send_the_sandbox_blocked_is_not_left_as_queued(self):
        """Five such sends sat as `queued` in the S10 collab pilot; the failure
        was only in the decision log."""
        from xsm import ledger
        ledger.queued("m1", {"name": "a"}, {"name": "b"}, "repo:x", "note", "hi")
        ledger.failed("m1", "sandbox-blocked: cannot open the inbox socket")
        entry = ledger.status("m1")
        self.assertEqual(entry["status"], "error")
        self.assertIn("sandbox-blocked", entry["error"])


class LifecycleTest(TempState):
    """What happens to a session after it stops (ADR-0001 addendum)."""

    def _register(self, name="s", runtime="codex", pid=None):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", runtime)
        os.makedirs(home, exist_ok=True)
        return registry.upsert(runtime, home, "sid-" + name, pid or os.getpid(), self.tmp, name=name)

    def _dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_clean_exit_reads_ended_and_a_crash_reads_stale(self):
        from xsm import identity, registry
        clean = self._register("clean", pid=self._dead_pid())
        registry.mark_ended("codex", clean["session_id"], "prompt_input_exit")
        crashed = self._register("crashed", pid=self._dead_pid())
        self.assertEqual(identity.state_of(registry.by_session("codex", clean["session_id"])), "ended")
        self.assertEqual(identity.state_of(registry.by_session("codex", crashed["session_id"])), "stale")

    def test_resuming_clears_the_goodbye(self):
        """claude --resume comes back with the same session id (measured)."""
        from xsm import identity, registry
        rec = self._register("resumable", pid=self._dead_pid())
        registry.mark_ended("codex", rec["session_id"], "prompt_input_exit")
        again = self._register("resumable")                     # same id, live pid
        self.assertNotIn("ended_at", again)
        self.assertEqual(identity.state_of(again), "live")
        self.assertEqual(again["ref"], rec["ref"])

    def test_sessionend_hook_marks_the_pointer(self):
        from xsm import receive, registry
        rec = self._register("leaving")
        receive.handle({"hook_event_name": "SessionEnd", "session_id": rec["session_id"],
                        "reason": "prompt_input_exit", "turn_id": "x"})
        self.assertEqual(registry.by_session("codex", rec["session_id"])["end_reason"],
                         "prompt_input_exit")

    def test_prune_keeps_recent_stopped_pointers_and_drops_old_ones(self):
        from xsm import housekeeping, paths, registry
        live = self._register("live")
        recent = self._register("recent", pid=self._dead_pid())
        old = self._register("old", pid=self._dead_pid())
        p = os.path.join(self.tmp, "sessions", "codex-%s.json" % old["session_id"])
        rec = paths.read_json(p)
        rec["updated"] = time.time() - 30 * 86400
        paths.write_json(p, rec)
        removed = housekeeping.prune()
        self.assertEqual(removed["sessions"], ["old"])
        names = {r["name"] for r in registry.records()}
        self.assertEqual(names, {"live", "recent"})

    def test_prune_ages_out_ledger_and_held(self):
        from xsm import housekeeping, paths
        now = time.time()
        paths.write_json(paths.path(paths.LEDGER, "fresh.json"), {"t": now})
        paths.write_json(paths.path(paths.LEDGER, "ancient.json"), {"t": now - 90 * 86400})
        paths.write_json(paths.path(paths.HELD, "ancient.json"), {"t": now - 90 * 86400})
        removed = housekeeping.prune()
        self.assertEqual(removed["ledger"], ["ancient.json"])
        self.assertEqual(removed["held"], ["ancient.json"])
        self.assertTrue(os.path.exists(paths.path(paths.LEDGER, "fresh.json")))

    def test_opportunistic_prune_runs_at_most_hourly(self):
        from xsm import housekeeping
        self.assertIsNotNone(housekeeping.maybe_prune())
        self.assertIsNone(housekeeping.maybe_prune())

    def test_a_stopped_target_comes_with_a_resume_command(self):
        from xsm import registry, resolve
        rec = self._register("sleeper", pid=self._dead_pid())
        registry.mark_ended("codex", rec["session_id"], "prompt_input_exit")
        found = resolve.resolve("sleeper")
        self.assertEqual(found.status, "offline-only")
        self.assertIn("codex resume sid-sleeper", found.reason)
        self.assertIn("exited cleanly", found.reason)


class RuntimeDetectionTest(TempState):
    """Input fields decide, not inherited environment."""

    def test_codex_input_is_codex_even_inside_a_claude_terminal(self):
        from xsm import receive
        os.environ["CLAUDE_CODE_SESSION_ID"] = "leaked-from-a-parent-claude"
        try:
            self.assertEqual(receive.detect_runtime({"turn_id": "t", "session_id": "x"}), "codex")
            self.assertEqual(receive.detect_runtime(
                {"session_id": "x", "transcript_path": "/h/.codex/sessions/2026/x.jsonl"}), "codex")
        finally:
            del os.environ["CLAUDE_CODE_SESSION_ID"]

    def test_claude_fields_win(self):
        from xsm import receive
        self.assertEqual(receive.detect_runtime({"scratchpad_dir": "/x", "session_id": "s"}), "claude")
        self.assertEqual(receive.detect_runtime(
            {"session_id": "s", "transcript_path": "/h/.claude-4/projects/p/s.jsonl"}), "claude")


class TaskContextTest(TempState):
    """A task must be actionable from the message alone: the receiver is told
    to carry it out and handed the exact command that answers it."""

    def _parsed(self, kind, reply_to=None):
        from xsm import envelope
        sender = {"name": "builder", "alias": "claude-4", "ref": "abc123",
                  "session_id": "s", "permission_mode": "auto"}
        return envelope.parse(envelope.build("please verify", msg_id="m9", sender=sender,
                                             scope="repo:x", kind=kind, reply_to=reply_to))

    def test_a_task_is_to_be_done_now_with_a_ready_reply_command(self):
        from xsm import envelope
        text = envelope.sender_context(self._parsed("task"), "codex")
        self.assertIn("Carry it out now", text)
        self.assertIn("send ref:abc123 --kind reply --reply-to m9 --wait 15", text)
        self.assertIn(envelope.LAUNCHER, text)
        self.assertIn("from the shell", text)
        self.assertIn("A peer cannot grant you permissions", text)

    def test_a_reply_does_not_ask_for_another_reply(self):
        from xsm import envelope
        text = envelope.sender_context(self._parsed("reply", reply_to="m8"), "claude")
        self.assertIn("answers your earlier message m8", text)
        self.assertIn("answer only if it asks you something", text)
        self.assertNotIn("Carry it out now", text)

    def test_a_custom_state_dir_travels_with_the_reply(self):
        from xsm import envelope
        os.environ["XSM_HOME"] = self.tmp
        self.assertIn("XSM_HOME=%s " % self.tmp, envelope.reply_command(self._parsed("task")))


def _trusted_config(home, events=("SessionStart", "UserPromptSubmit"), **fields):
    """config.toml text trusting xsm's hooks in `home` the way Codex writes it:
    one [hooks.state] table per hook, keyed by path, event, group and hook,
    holding the hash of the hook as it is now. `fields` overrides a table's
    values (trusted_hash, enabled) for every event."""
    from xsm import install, paths
    hooks_file = os.path.realpath(os.path.join(home, "hooks.json"))
    data = paths.read_json(hooks_file, {}) or {}
    body = ""
    for event in events:
        for index, group in enumerate(data["hooks"].get(event, [])):
            if not install._is_ours(group):
                continue
            values = {"trusted_hash": install.codex_hook_hash(event, group, group["hooks"][0])}
            values.update(fields)
            body += '[hooks.state."%s:%s:%d:0"]\n' % (hooks_file, install.CODEX_EVENT_KEYS[event],
                                                       index)
            for key, value in values.items():
                body += "%s = %s\n" % (key, "true" if value is True else "false"
                                        if value is False else '"%s"' % value)
            body += "\n"
    return body


class CodexVisibilityTest(TempState):
    """A Codex thread that is open but never prompted has not run its hook, so
    it is not in the registry. The sender must be told that, not "no such
    session" — found when a /rename'd, unprompted Codex reviewer went unseen."""

    def _codex_home(self, trusted=True):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {}}, mode=0o644)
        install.apply(home, "codex")
        body = _trusted_config(home) if trusted else ""
        open(os.path.join(home, "config.toml"), "w").write(body)
        config.add_home(home, "codex")
        return home

    def test_trust_is_read_from_config_toml(self):
        from xsm import install
        self.assertEqual(install.codex_trust(self._codex_home(trusted=True)),
                         {"SessionStart": True, "UserPromptSubmit": True})
        self.assertEqual(install.codex_trust(self._codex_home(trusted=False)),
                         {"SessionStart": False, "UserPromptSubmit": False})

    def _with_open_thread(self, home, rollout=None):
        from xsm import registry
        registry._open_codex_threads = lambda h: [("t-open", "reviewer", self.tmp, rollout, 0, 0, os.getpid())]

    def test_unprompted_thread_is_reported_with_the_reason(self):
        from xsm import resolve
        home = self._codex_home(trusted=True)
        self._with_open_thread(home)
        found = resolve.resolve("reviewer")
        self.assertEqual(found.status, "unregistered")
        self.assertIn("no prompt yet", found.reason)

    def test_untrusted_hooks_are_named_as_the_cause(self):
        from xsm import resolve
        home = self._codex_home(trusted=False)
        self._with_open_thread(home)
        self.assertIn("not trusted", resolve.resolve("reviewer").reason)


class AdoptionTest(TempState):
    """An open Codex thread in a home whose xsm hooks are trusted is registered
    by the CLI before its first prompt, so it can be handed its first task.
    Measured: a never-prompted thread took a task through the queue, and its
    own hook registered it on that first turn."""

    def _home(self, trusted):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "codex-adopt")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {}}, mode=0o644)
        install.apply(home, "codex")
        body = _trusted_config(home) if trusted else ""
        open(os.path.join(home, "config.toml"), "w").write(body)
        config.add_home(home, "codex")
        return home

    def _open_thread(self):
        from xsm import registry
        registry._open_codex_threads = lambda h: [
            ("t-new", "fresh", self.tmp, None, 0, 0, os.getpid())]

    def test_trusted_home_is_adopted(self):
        from xsm import registry, resolve
        self._home(trusted=True)
        self._open_thread()
        adopted = registry.adopt_open_codex()
        self.assertEqual([r["name"] for r in adopted], ["fresh"])
        self.assertEqual(resolve.resolve("fresh").status, "resolved")

    def test_untrusted_home_is_not_adopted(self):
        from xsm import registry
        self._home(trusted=False)
        self._open_thread()
        self.assertEqual(registry.adopt_open_codex(), [])

    def test_a_stopped_workers_thread_is_not_handed_to_another_tui(self):
        """Issue #5: stopping a --once worker deleted its pointer; the next
        list adopted its thread for another TUI open in the same folder, and
        showed it live under that TUI's pid. The pointer now stays, ended."""
        from xsm import registry, workers
        home = self._home(trusted=True)
        registry.upsert("codex", home, "t-new", os.getpid(), self.tmp, name="h10-once")
        workers.save({"name": "h10-once", "runtime": "codex", "home": home,
                      "session_id": "t-new", "created": time.time()})
        workers.stop("h10-once")
        self._open_thread()                            # another TUI, alive, same folder
        self.assertEqual(registry.adopt_open_codex(), [])
        rec = registry.by_session("codex", "t-new") or {}
        self.assertEqual(rec["state"], "ended", "its pid is alive, but xsm stopped it")
        self.assertEqual(rec["end_reason"], "worker-stopped")

    def test_a_thread_the_daemon_has_not_loaded_is_not_adopted(self):
        """The folder match is a guess; where the home's daemon answers, a
        thread it has not loaded is open in none of its TUIs (issue #5)."""
        from xsm import codex_daemon, registry
        self._home(trusted=True)
        self._open_thread()
        with mock.patch.object(codex_daemon, "loaded_threads", lambda home: set()):
            self.assertEqual(registry.adopt_open_codex(), [])
        with mock.patch.object(codex_daemon, "loaded_threads", lambda home: {"t-new"}):
            self.assertEqual([r["name"] for r in registry.adopt_open_codex()], ["fresh"])

    def test_no_daemon_answer_keeps_adopting_as_before(self):
        from xsm import codex_daemon, registry
        self._home(trusted=True)
        self._open_thread()
        with mock.patch.object(codex_daemon, "loaded_threads", lambda home: None):
            self.assertEqual([r["name"] for r in registry.adopt_open_codex()], ["fresh"])

    def test_the_hook_takes_over_the_pointer(self):
        from xsm import registry
        home = self._home(trusted=True)
        self._open_thread()
        registry.adopt_open_codex()
        again = registry.upsert("codex", home, "t-new", os.getpid(), self.tmp, permission_mode="auto")
        self.assertNotIn("adopted", again)


class _EnvMixin:
    """Set or clear the runtimes' identity variables for one test. The suite
    itself may run inside a Claude or Codex shell, which sets them."""

    def _env(self, **values):
        for key, value in values.items():
            self.addCleanup(lambda k=key, v=os.environ.get(key):
                            os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _walk(self, pid, comm=None):
        """What the process walk finds: the nearest agent's pid (None is the
        Codex sandbox, where `ps` is refused) and that process's name."""
        from xsm import identity
        identity.ancestor_pid = lambda names, max_hops=10: pid
        identity.comm = lambda p: comm


class OwnRuntimeIdentityTest(_EnvMixin, TempState):
    """A shell that knows its own runtime id is identified by that id alone.
    Before (adversarial review, 2026-09-28): a Codex thread with no record yet
    fell through to the cwd rule and took the one live session in its folder,
    and a Codex that inherited a Claude session id took that Claude session —
    either way `xsm send` signed as someone else."""

    def setUp(self):
        super().setUp()
        self._env(CLAUDE_CODE_MESSAGING_SOCKET=None, CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "none"))
        self.chome = os.path.join(self.tmp, "homes", "claude")
        self.xhome = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(self.chome)
        os.makedirs(self.xhome)

    def test_an_unregistered_codex_thread_does_not_take_a_neighbour_by_cwd(self):
        from xsm import registry
        registry.upsert("codex", self.xhome, "t-neighbour", os.getpid(), self.tmp, name="neighbour")
        self._env(CLAUDE_CODE_SESSION_ID=None, CODEX_THREAD_ID="t-mine")
        self._walk(None)                                     # the Codex sandbox
        self.assertEqual(registry._me(None, self.tmp), (None, "none:codex-thread-unregistered"))
        # Where the walk runs, a pid match is no better: after /new the same
        # TUI's older thread has this pid.
        self._walk(os.getpid(), "codex")
        self.assertIsNone(registry.me(cwd=self.tmp))
        registry.upsert("codex", self.xhome, "t-mine", os.getpid(), self.tmp, name="mine")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "t-mine")

    def test_a_codex_carrying_only_a_registered_claude_id_does_not_take_it(self):
        from xsm import identity, registry
        identity.pid_alive = lambda pid: True
        registry.upsert("claude", self.chome, "s-claude", 1111, self.tmp, name="builder")
        self._env(CLAUDE_CODE_SESSION_ID="s-claude", CODEX_THREAD_ID="t-codex",
                  CODEX_SANDBOX="seatbelt")
        self._walk(None)                                     # the Codex sandbox
        self.assertEqual(registry._me(None, self.tmp), (None, "none:codex-thread-unregistered"))
        self._walk(2222, "codex")                            # nearest agent: an unregistered Codex
        self.assertIsNone(registry.me(cwd=self.tmp))
        self._walk(1111)                                     # a Claude run under that Codex
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "s-claude")

    def test_a_claude_after_clear_is_still_found_by_its_process(self):
        """/clear gives the same Claude process a new session id before its
        hook records it; the pid still names the session."""
        from xsm import registry
        registry.upsert("claude", self.chome, "s-before-clear", 1111, self.tmp, name="builder")
        self._env(CLAUDE_CODE_SESSION_ID="s-after-clear", CODEX_THREAD_ID=None)
        self._walk(1111, "claude")
        self.assertEqual(registry._me(None, self.tmp)[0]["session_id"], "s-before-clear")

    def test_a_claude_with_an_id_never_falls_back_to_cwd(self):
        from xsm import registry
        registry.upsert("claude", self.chome, "s-neighbour", os.getpid(), self.tmp, name="n",
                        socket=os.path.join(self.tmp, "none.sock"))
        registry.upsert("codex", self.xhome, "t-neighbour", os.getpid(), self.tmp, name="m")
        self._env(CLAUDE_CODE_SESSION_ID="s-mine", CODEX_THREAD_ID=None)
        self._walk(None)
        self.assertEqual(registry._me(None, self.tmp), (None, "none:unregistered-session"))

    def test_a_shell_with_neither_id_still_uses_the_folder(self):
        from xsm import registry
        registry.upsert("codex", self.xhome, "t-only", os.getpid(), self.tmp, name="only")
        self._env(CLAUDE_CODE_SESSION_ID=None, CODEX_THREAD_ID=None)
        self._walk(None)
        rec, how = registry._me(None, self.tmp)
        self.assertEqual((rec["session_id"], how), ("t-only", "cwd"))

    def test_a_codex_carrying_a_claude_id_is_told_about_its_thread(self):
        """It was told to install xsm into the Claude home it inherited."""
        from xsm import registry
        self._env(CLAUDE_CODE_SESSION_ID="s-claude", CODEX_THREAD_ID="t-codex",
                  CODEX_SANDBOX="seatbelt")
        self._walk(None)
        why = registry.unregistered_reason()
        self.assertIn("Codex thread (t-codex)", why)
        self.assertIn("next prompt", why)
        self.assertNotIn("--claude-home", why)
        self._walk(1111, "claude")                           # a Claude shell after all
        self.assertIn("--claude-home", registry.unregistered_reason())
        self._env(CODEX_SANDBOX=None)
        self._walk(None)                                     # and nothing tells them apart
        why = registry.unregistered_reason()
        self.assertIn("both a Claude Code session id (s-claude) and a Codex thread id (t-codex)",
                      why)
        self.assertNotIn("--claude-home", why)


class NestedRuntimeTest(_EnvMixin, TempState):
    """A shell carrying both runtimes' ids where the process walk cannot run.
    That was read as the Codex sandbox, but a Claude worker spawned by a Codex
    session kept the thread's id and CODEX_SANDBOX, and in its own sandbox
    `ps` is refused too — so it signed as the Codex parent (adversarial
    review, 2026-09-28)."""

    def setUp(self):
        super().setUp()
        from xsm import identity
        self.chome = os.path.join(self.tmp, "homes", "claude")
        self.xhome = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(self.chome)
        os.makedirs(self.xhome)
        self.alive = {1111, 2222}
        identity.pid_alive = lambda pid: pid in self.alive
        self._walk(None)

    def _register(self, codex_start=None, claude_start=None):
        from xsm import identity, registry
        starts = {1111: claude_start, 2222: codex_start}
        identity.lstart = lambda pid: starts.get(pid)
        registry.upsert("claude", self.chome, "s-worker", 1111, self.tmp, name="worker",
                        socket="/tmp/cc-socks/1111.sock")
        registry.upsert("codex", self.xhome, "t-parent", 2222, self.tmp, name="parent")
        identity.lstart = lambda pid: None                   # `ps` refused from here on

    def test_a_worker_carries_neither_runtimes_identity(self):
        import shlex
        from xsm import workers
        caller = {k: "x" for k in workers.CALLER_IDENTITY}
        self.assertTrue(set(workers.AGENT_MARKERS) <= set(workers.CALLER_IDENTITY))
        with mock.patch.dict(os.environ, caller):
            for runtime in ("claude", "codex"):
                env = workers._env({"name": "w", "runtime": runtime, "home": self.tmp})
                self.assertEqual([k for k in caller if k in env], [], runtime)
            seen = []

            def run(argv, **kw):
                seen.append(argv)
                return mock.Mock(returncode=0, stdout="%9 4242\n", stderr="")
            w = {"name": "p", "runtime": "codex", "home": self.tmp, "cwd": self.tmp,
                 "mode": "pane", "model": "m"}
            with mock.patch.object(workers.subprocess, "run", run), \
                    mock.patch.object(workers.time, "sleep", lambda s: None):
                workers._start_in_tmux(w, "%1")
        # tmux starts the pane from its server's environment, not ours, so each
        # one must be unset on the command line too.
        command = shlex.split(next(a for a in seen if a[:2] == ["tmux", "split-window"])[-1])
        for key in caller:
            self.assertIn(key, command[command.index("-u"):], key)

    def test_a_claude_started_by_a_codex_session_is_itself(self):
        from xsm import registry
        self._register(codex_start="Mon Sep 28 09:00:00 2026",
                       claude_start="Mon Sep 28 10:00:00 2026")
        self._env(CLAUDE_CODE_SESSION_ID="s-worker", CODEX_THREAD_ID="t-parent",
                  CODEX_SANDBOX="seatbelt", CLAUDE_CODE_MESSAGING_SOCKET="/tmp/cc-socks/1111.sock")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "s-worker")
        # The other way round — a Codex started from that Claude's shell.
        self._register(codex_start="Mon Sep 28 10:00:00 2026",
                       claude_start="Mon Sep 28 09:00:00 2026")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "t-parent")

    def test_what_the_inner_sessions_hook_saw_outweighs_start_order(self):
        """The outer Codex resumed, so its record started later than the Claude
        inside it, and start order gave that Claude's shell to the Codex
        (review, 2026-09-28). The Claude's own hook saw the thread's id."""
        from xsm import identity, registry
        starts = {1111: "Mon Sep 28 09:00:00 2026", 2222: "Mon Sep 28 10:00:00 2026"}
        identity.lstart = lambda pid: starts.get(pid)
        registry.upsert("claude", self.chome, "s-worker", 1111, self.tmp, name="worker",
                        inside="t-parent")
        registry.upsert("codex", self.xhome, "t-parent", 2222, self.tmp, name="parent",
                        inside=None)
        identity.lstart = lambda pid: None
        self._env(CLAUDE_CODE_SESSION_ID="s-worker", CODEX_THREAD_ID="t-parent")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "s-worker")
        # Adoption does not know it and leaves it as it was.
        registry.upsert("claude", self.chome, "s-worker", 1111, self.tmp)
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "s-worker")
        # The other way round: a Codex started from the Claude's shell.
        registry.upsert("claude", self.chome, "s-worker", 1111, self.tmp, inside=None)
        registry.upsert("codex", self.xhome, "t-parent", 2222, self.tmp, inside="s-worker")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "t-parent")

    def test_a_session_whose_process_is_gone_is_not_this_one(self):
        from xsm import registry
        self._register()
        self._env(CLAUDE_CODE_SESSION_ID="s-worker", CODEX_THREAD_ID="t-parent",
                  CLAUDE_CODE_MESSAGING_SOCKET="/tmp/cc-socks/1111.sock")
        self.alive = {1111}
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "s-worker")
        self.alive = {2222}
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "t-parent")

    def test_nothing_to_tell_them_apart_is_no_record_not_another_session(self):
        from xsm import registry
        self._register()
        self._env(CLAUDE_CODE_SESSION_ID="s-worker", CODEX_THREAD_ID="t-parent")
        self.assertEqual(registry._me(None, self.tmp), (None, "none:ambiguous-runtime"))
        self.assertIn("both a Claude Code session id", registry.unregistered_reason())
        # A sandbox marker is evidence: xsm's for its Claude workers, Codex's own.
        self._env(XSM_SANDBOXED="1")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "s-worker")
        self._env(XSM_SANDBOXED=None, CODEX_SANDBOX="seatbelt")
        self.assertEqual(registry.me(cwd=self.tmp)["session_id"], "t-parent")


class CodexHookTrustTest(TempState):
    """Codex runs a hook only when it is not switched off and its trusted_hash
    equals the hash of the hook as it is now (Codex 0.158). The mere text
    "trusted_hash" near the key read as trusted before (review, 2026-09-28),
    and the CLI adopted threads whose hooks never run."""

    def _home(self):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "codex-trust")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {}}, mode=0o644)
        install.apply(home, "codex")
        config.add_home(home, "codex")
        return home

    def _config(self, home, text):
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write(text)

    def test_the_hash_is_codexs_own(self):
        """The value Codex hashes (discovery.rs hook_hash, fingerprint.rs
        version_for_toml): the normalized hook as JSON with sorted keys and no
        spaces. Checked against real Codex homes when written (2026-09-28)."""
        from xsm import install
        group = {"hooks": [{"type": "command", "command": "python3 hook.py", "timeout": 10}]}
        # The identity Codex serializes: sorted keys, no spaces, defaults filled.
        text = ('{"event_name":"session_start","hooks":[{"async":false,'
                '"command":"python3 hook.py","timeout":10,"type":"command"}]}')
        import hashlib
        self.assertEqual(install.codex_hook_hash("SessionStart", group, group["hooks"][0]),
                         "sha256:" + hashlib.sha256(text.encode()).hexdigest())
        # No timeout means Codex's 600 s; UserPromptSubmit drops any matcher.
        bare = {"matcher": "x", "hooks": [{"type": "command", "command": "c"}]}
        self.assertEqual(install.codex_hook_hash("UserPromptSubmit", bare, bare["hooks"][0]),
                         install.codex_hook_hash("UserPromptSubmit", {"hooks": []},
                                                 {"type": "command", "command": "c",
                                                  "timeout": 600}))

    def test_matching_hash_is_trusted(self):
        from xsm import install
        home = self._home()
        self._config(home, _trusted_config(home))
        self.assertEqual(install.codex_trust(home), {"SessionStart": True, "UserPromptSubmit": True})

    def test_a_hook_changed_since_it_was_trusted_is_not(self):
        from xsm import install, paths
        home = self._home()
        self._config(home, _trusted_config(home))
        hooks = paths.read_json(os.path.join(home, "hooks.json"))
        for group in hooks["hooks"]["SessionStart"]:
            if install._is_ours(group):
                group["hooks"][0]["command"] = "/elsewhere/python3 " + group["hooks"][0]["command"]
        paths.write_json(os.path.join(home, "hooks.json"), hooks, mode=0o644)
        self.assertEqual(install.codex_trust(home), {"SessionStart": False, "UserPromptSubmit": True})
        self._config(home, _trusted_config(home, trusted_hash="sha256:" + "0" * 64))
        self.assertEqual(install.codex_trust(home), {"SessionStart": False, "UserPromptSubmit": False})

    def test_a_disabled_hook_is_not_trusted(self):
        from xsm import install
        home = self._home()
        self._config(home, _trusted_config(home, enabled=False))
        self.assertEqual(install.codex_trust(home), {"SessionStart": False, "UserPromptSubmit": False})
        self._config(home, _trusted_config(home, enabled=True))
        self.assertEqual(install.codex_trust(home), {"SessionStart": True, "UserPromptSubmit": True})

    def test_the_parse_without_tomllib_agrees(self):
        """The pinned interpreter may be 3.9, which has no tomllib."""
        import builtins
        from xsm import install
        home = self._home()
        real = builtins.__import__

        def no_tomllib(name, *args, **kwargs):
            if name == "tomllib":
                raise ImportError(name)
            return real(name, *args, **kwargs)

        for text, want in ((_trusted_config(home), True),
                           (_trusted_config(home, enabled=False), False),
                           (_trusted_config(home, trusted_hash="sha256:x"), False),
                           ("", False)):
            self._config(home, "# user settings\nmodel = 'x'\n\n" + text)
            with mock.patch.object(builtins, "__import__", no_tomllib):
                self.assertEqual(install.codex_trust(home),
                                 {"SessionStart": want, "UserPromptSubmit": want}, text)

    def test_the_parse_without_tomllib_reads_quoted_keys(self):
        """`"enabled" = false` is the same TOML as `enabled = false`, and the
        3.9 reader took it for a hook still switched on (review, 2026-09-28)."""
        import builtins
        from xsm import install
        home = self._home()
        real = builtins.__import__

        def no_tomllib(name, *args, **kwargs):
            if name == "tomllib":
                raise ImportError(name)
            return real(name, *args, **kwargs)

        trusted = _trusted_config(home)
        for text, want in ((trusted.replace("trusted_hash =", '"trusted_hash" ='), True),
                           (trusted.replace("trusted_hash =", "'trusted_hash'=")
                            .replace("\n\n", "\n'enabled'=true\n\n"), True),
                           (trusted.replace("\n\n", '\n"enabled" = false\n\n'), False),
                           (trusted.replace("\n\n", "\n'enabled'=false\n\n"), False),
                           (trusted.replace("\n\n", "\nenabled = [false]\n\n"), False)):
            self._config(home, text)
            with mock.patch.object(builtins, "__import__", no_tomllib):
                self.assertEqual(install.codex_trust(home),
                                 {"SessionStart": want, "UserPromptSubmit": want}, text)

    def test_a_home_trusting_only_session_start_is_not_adopted(self):
        """UserPromptSubmit is the gate every message passes; a home without
        it was adopted into a session that could never take one."""
        from xsm import install, paths, registry
        home = self._home()
        hooks = paths.read_json(os.path.join(home, "hooks.json"))
        hooks["hooks"]["UserPromptSubmit"] = [g for g in hooks["hooks"]["UserPromptSubmit"]
                                              if not install._is_ours(g)]
        paths.write_json(os.path.join(home, "hooks.json"), hooks, mode=0o644)
        self._config(home, _trusted_config(home))
        self.assertEqual(install.codex_trust(home), {"SessionStart": True})
        registry._open_codex_threads = lambda h: [("t-new", "fresh", self.tmp, None, 0, 0, os.getpid())]
        self.assertEqual(registry.adopt_open_codex(), [])
        self.assertIn("not trusted", registry.unregistered()[0]["why"])

    def test_a_home_whose_hooks_codex_will_not_run_is_not_adopted(self):
        from xsm import registry
        home = self._home()
        registry._open_codex_threads = lambda h: [("t-new", "fresh", self.tmp, None, 0, 0, os.getpid())]
        for text in (_trusted_config(home, enabled=False),
                     _trusted_config(home, trusted_hash="sha256:stale")):
            self._config(home, text)
            self.assertEqual(registry.adopt_open_codex(), [])
        self._config(home, _trusted_config(home))
        self.assertEqual([r["name"] for r in registry.adopt_open_codex()], ["fresh"])


class WhoListAgreeTest(_EnvMixin, TempState):
    """`list` adopts open Codex threads before it answers and `who` did not, so
    `who`, `list`, `who` said "not registered", then marked a row `you`, then
    "registered" (review, 2026-09-28)."""

    def test_who_adopts_what_list_would(self):
        from xsm import cli, config, install, paths, registry
        home = os.path.join(self.tmp, "codex-who")
        os.makedirs(home)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {}}, mode=0o644)
        install.apply(home, "codex")
        config.add_home(home, "codex")
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write(_trusted_config(home))
        registry._open_codex_threads = lambda h: [("t-open", "opened", self.tmp, None, 0, 0,
                                                   os.getpid())]
        self._env(CLAUDE_CODE_SESSION_ID=None, CLAUDE_CODE_MESSAGING_SOCKET=None,
                  CODEX_THREAD_ID="t-open")
        self._walk(None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["who"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("opened@", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class ListClearTest(TempState):
    def test_clear_forgets_stopped_sessions_only(self):
        from xsm import cli, registry
        here = os.path.join(self.tmp, "here")
        there = os.path.join(self.tmp, "there")
        os.makedirs(here)
        os.makedirs(there)
        home = os.path.join(self.tmp, "codex")
        os.makedirs(home)
        registry.upsert("codex", home, "live1", os.getpid(), here, name="alive")
        for sid, cwd in (("dead1", here), ("dead2", there)):
            rec = registry.upsert("codex", home, sid, 999999, cwd, name=sid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "clear", "--dir", here])
        self.assertIn("cleared 1", out.getvalue())
        left = {r["session_id"] for r in registry.records()}
        self.assertEqual(left, {"live1", "dead2"}, "live kept; other project untouched")
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["list", "clear", "-a", "--dir", here])
        self.assertEqual({r["session_id"] for r in registry.records()}, {"live1"})


class OutcomeTest(TempState):
    def test_outcome_belongs_to_a_reply(self):
        from xsm import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["send", "nobody", "--text", "x", "--outcome", "failed"])
        self.assertEqual(code, 2, "a note cannot close a task")
        self.assertIn("--outcome belongs to the reply", err.getvalue())

    def test_build_refuses_an_outcome_it_does_not_know(self):
        from xsm import envelope
        sender = {"name": "a", "alias": "h", "ref": "aaaaaa"}
        with self.assertRaises(ValueError, msg="strict when sending"):
            envelope.build("x", msg_id="m1", sender=sender, scope="s", kind="reply",
                           outcome="maybe")

    def test_a_reply_closes_the_task_on_the_senders_ledger(self):
        from xsm import envelope, ledger, receive
        ledger.queued("t1", {"name": "a", "ref": "aaaaaa"}, {"name": "b", "ref": "bbbbbb"},
                      "repo:x", "task", "run the tests")
        parsed = envelope.parse(envelope.build(
            "could not", msg_id="r1", sender={"name": "b", "alias": "h", "ref": "bbbbbb"},
            scope="repo:x", kind="reply", reply_to="t1", outcome="failed"))
        receive._close_task(parsed, receive._outcome(parsed))
        entry = ledger.status("t1")
        self.assertEqual(entry["outcome"], "failed", "the task's own entry outlives the worker")
        self.assertEqual(entry["status"], "queued", "the outcome is not a delivery state")
        self.assertEqual(entry["closed_by"]["ref"], "bbbbbb")

    def test_an_unknown_outcome_is_ignored_not_refused(self):
        from xsm import envelope, ledger, receive
        ledger.queued("t2", {"name": "a", "ref": "aaaaaa"}, {"name": "b", "ref": "bbbbbb"},
                      "repo:x", "task", "run the tests")
        parsed = envelope.parse("<%s>\n[xsm v1 id=r2 from=\"b@h\" ref=bbbbbb scope=\"repo:x\" "
                                "kind=reply outcome=partly reply-to=t2]\nhalf\n</%s>"
                                % (envelope.TAG, envelope.TAG))
        self.assertIsNone(receive._outcome(parsed), "lenient when receiving")
        receive._close_task(parsed, receive._outcome(parsed))
        self.assertIsNone(ledger.status("t2").get("outcome"))

    def test_the_reply_command_a_task_hands_back_carries_the_flag(self):
        from xsm import envelope
        parsed = envelope.parse(envelope.build(
            "do it", msg_id="t9", sender={"name": "a", "alias": "h", "ref": "aaaaaa"},
            scope="repo:x", kind="task"))
        context = envelope.sender_context(parsed)
        self.assertIn("--wait 15 --outcome succeeded", context)
        self.assertIn("--outcome failed", context, "the failure path has to be named too")
        note = envelope.parse(envelope.build(
            "fyi", msg_id="n9", sender={"name": "a", "alias": "h", "ref": "aaaaaa"},
            scope="repo:x", kind="note"))
        self.assertNotIn("--outcome", envelope.sender_context(note),
                         "a note closes nothing; the command it offers stays as it was")


class SkillLayoutTest(unittest.TestCase):
    """One skill takes the commands as arguments: `/xsm list` in Claude Code,
    `$xsm list` in Codex (2026-09-27). Codex has no slash commands of its own,
    and eight command skills beside `xsm` were eight skills that were not."""

    def setUp(self):
        from xsm import install
        self.dir = os.path.join(install.REPO, "skills", "xsm")
        self.skill = open(os.path.join(self.dir, "SKILL.md"), encoding="utf-8").read()

    def test_display_commands_run_with_table_and_are_copied_bare(self):
        for command in ("list", "who", "projects", "doctor"):
            self.assertIn("`%s`" % command, self.skill)
        self.assertIn("xsm <command> --table", self.skill)
        self.assertIn("xsm ledger --table --mine --last 5", self.skill)
        self.assertIn("xsm held list --table", self.skill)
        self.assertIn("copied exactly", self.skill)
        self.assertIn("not in a code block", self.skill)

    def test_no_wording_left_from_the_preexecuted_commands(self):
        """Those bodies said "do not call any tool" and wrapped the output in
        <<< >>>: without `!` pre-execution the model then runs nothing, or
        copies the markers (2026-09-23)."""
        self.assertNotIn("do not call any tool", self.skill)
        self.assertNotIn("<<<", self.skill)
        self.assertNotIn("!`", self.skill)
        self.assertNotIn("/xsm-", self.skill)

    def test_read_only_commands_are_the_only_ones_pre_approved(self):
        head = self.skill.split("\n---\n", 1)[0]
        self.assertIn("name: xsm\n", head)
        allowed = [line for line in head.splitlines() if line.startswith("allowed-tools:")][0]
        self.assertNotIn("send", allowed)
        # Exact commands, not prefixes: `xsm held:*` would also approve
        # `xsm held drop`, and `xsm list:*` would approve `xsm list clear`.
        self.assertNotIn(":*", allowed)
        for command in ("list --table", "who --table", "projects --table", "doctor --table",
                        "ledger --table --mine --last 5", "held list --table"):
            self.assertIn("Bash(xsm %s)" % command, allowed)
        # The rules match only what the body tells the model to run.
        body = self.skill.split("\n---\n", 1)[1]
        for line in ("xsm <command> --table", "xsm ledger --table --mine --last 5", "xsm held list --table"):
            self.assertIn(line, body)

    def test_the_guide_lives_under_references(self):
        guide = os.path.join(self.dir, "references", "guide.md")
        self.assertTrue(os.path.isfile(guide))
        self.assertIn("references/guide.md", self.skill)
        self.assertNotIn("/xsm-", open(guide, encoding="utf-8").read())
        self.assertLess(len(self.skill.splitlines()), 90, "the reference text belongs in the guide")
