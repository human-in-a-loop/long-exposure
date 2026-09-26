"""Vendor-neutral lifecycle hooks: the envelope and compaction.

These are correctness and observability hooks, not enforcement. A
`PreToolUse` path fence was built and removed: the harness treats the model
as a faithful collaborator, so a fence that only stops honest mistakes
duplicates the prompt, and one meant to stop an adversarial model could be
circumvented anyway.

The property that carries the most weight is the harness-turn gate. A live
test found the ungated envelope hook nudging a bare `claude -p` turn into
inventing an [OUTPUT:] label it was never asked for.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from long_exposure import hooks_install as hi
from long_exposure.hooks import _io, compaction, envelope


class HarnessTurnGateTests(unittest.TestCase):
    def test_gate_reads_the_env_var(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_io.ENV_ACTIVE, None)
            self.assertFalse(_io.harness_turn())
        with mock.patch.dict(os.environ, {_io.ENV_ACTIVE: "1"}):
            self.assertTrue(_io.harness_turn())
        with mock.patch.dict(os.environ, {_io.ENV_ACTIVE: "false"}):
            self.assertFalse(_io.harness_turn())

    def test_the_harness_sets_it_on_every_agent_turn(self):
        from long_exposure.orchestrator import _add_hook_env

        env = {}
        _add_hook_env(env, {}, agent_name="worker", cycle=3,
                      state_dir="/tmp/i", expected_output="work_output")
        self.assertEqual(env[_io.ENV_ACTIVE], "1")
        self.assertEqual(env["LONG_EXPOSURE_HOOK_AGENT"], "worker")
        self.assertEqual(env["LONG_EXPOSURE_HOOK_CYCLE"], "3")
        self.assertEqual(env["LONG_EXPOSURE_EXPECTED_OUTPUT"], "work_output")

    def test_no_enforcement_env_is_set(self):
        """The fence was removed; nothing here should police anything."""
        from long_exposure.orchestrator import _add_hook_env

        env = {}
        _add_hook_env(env, {"hooks": {"fence": {"scope": "always"}},
                            "git_sync": {"harness_commits_only": True}})
        for gone in ("LONG_EXPOSURE_FENCE_SCOPE",
                     "LONG_EXPOSURE_GIT_HARNESS_ONLY",
                     "LONG_EXPOSURE_HARNESS_ROOT"):
            self.assertNotIn(gone, env, gone)


class ConfigBlockTests(unittest.TestCase):
    """The `hooks:` block in config.yaml has to actually reach the hooks.

    It reaches them only through env vars: a hook is spawned by the vendor
    CLI, not by the harness, so it never sees config.yaml. This block was
    documented and inert for a while — nothing read it — which is the
    failure these tests pin.
    """

    def _env(self, config):
        from long_exposure.orchestrator import _add_hook_env

        env = {}
        _add_hook_env(env, config)
        return env

    def test_defaults_disable_nothing(self):
        for config in ({}, {"hooks": {}}, {"hooks": "not-a-dict"},
                       {"hooks": {"envelope": {"enabled": True},
                                  "compaction": {"enabled": True}}}):
            env = self._env(config)
            self.assertNotIn(envelope.ENV_DISABLE, env, repr(config))
            self.assertNotIn(compaction.ENV_DISABLE, env, repr(config))

    def test_disabling_the_envelope_reaches_the_hook(self):
        env = self._env({"hooks": {"envelope": {"enabled": False}}})
        self.assertEqual(env[envelope.ENV_DISABLE], "1")
        self.assertNotIn(compaction.ENV_DISABLE, env)
        with mock.patch.dict(os.environ, {_io.ENV_ACTIVE: "1", **env}):
            with mock.patch.object(_io, "read_payload",
                                   return_value={"last_assistant_message": "no block"}):
                with mock.patch.object(_io, "continue_turn") as nudged:
                    envelope.main()
        nudged.assert_not_called()

    def test_disabling_compaction_reaches_the_hook(self):
        env = self._env({"hooks": {"compaction": {"enabled": False}}})
        self.assertEqual(env[compaction.ENV_DISABLE], "1")
        self.assertNotIn(envelope.ENV_DISABLE, env)
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(_io, "read_payload",
                                   return_value={"hook_event_name": "PreCompact"}):
                with mock.patch.object(compaction, "record") as recorded:
                    compaction.main()
        recorded.assert_not_called()

    def test_a_quoted_false_still_disables(self):
        """`enabled: "false"` is truthy to a bare bool(); flags.truthy is not."""
        for word in ("false", "no", "off", "0", "FALSE"):
            env = self._env({"hooks": {"envelope": {"enabled": word}}})
            self.assertEqual(env.get(envelope.ENV_DISABLE), "1", word)

    def test_max_nudges_is_passed_through(self):
        env = self._env({"hooks": {"envelope": {"max_nudges": 3}}})
        self.assertEqual(env[envelope.ENV_MAX_NUDGES], "3")
        with mock.patch.dict(os.environ, env):
            self.assertEqual(envelope._max_nudges(), 3)

    def test_max_nudges_zero_means_never_nudge(self):
        env = self._env({"hooks": {"envelope": {"max_nudges": 0}}})
        self.assertEqual(env[envelope.ENV_MAX_NUDGES], "0")
        with mock.patch.dict(os.environ, {_io.ENV_ACTIVE: "1", **env}):
            with mock.patch.object(_io, "read_payload",
                                   return_value={"session_id": "s",
                                                 "last_assistant_message": "no block"}):
                with mock.patch.object(_io, "continue_turn") as nudged:
                    envelope.main()
        nudged.assert_not_called()

    def test_a_negative_max_nudges_is_clamped_not_rejected(self):
        env = self._env({"hooks": {"envelope": {"max_nudges": -2}}})
        self.assertEqual(env[envelope.ENV_MAX_NUDGES], "0")

    def test_an_unparseable_max_nudges_leaves_the_hook_default(self):
        env = self._env({"hooks": {"envelope": {"max_nudges": "lots"}}})
        self.assertNotIn(envelope.ENV_MAX_NUDGES, env)

    def test_max_nudges_absent_is_not_forced_to_a_number(self):
        env = self._env({"hooks": {"envelope": {"enabled": True}}})
        self.assertNotIn(envelope.ENV_MAX_NUDGES, env)

    def test_an_operators_own_off_var_is_not_cleared(self):
        """The env var is an escape hatch; config must not silently undo it."""
        from long_exposure.orchestrator import _add_hook_env

        env = {envelope.ENV_DISABLE: "1", compaction.ENV_DISABLE: "1"}
        _add_hook_env(env, {"hooks": {"envelope": {"enabled": True},
                                      "compaction": {"enabled": True}}})
        self.assertEqual(env[envelope.ENV_DISABLE], "1")
        self.assertEqual(env[compaction.ENV_DISABLE], "1")

    def test_the_shipped_config_enables_both_hooks(self):
        import yaml

        cfg = yaml.safe_load(
            (Path(__file__).resolve().parent.parent
             / "long_exposure" / "config.yaml").read_text()
        )
        block = cfg["hooks"]
        self.assertTrue(block["envelope"]["enabled"])
        self.assertTrue(block["compaction"]["enabled"])
        self.assertEqual(block["envelope"]["max_nudges"], 1)
        # Every key in the shipped block is one _add_hook_env knows about;
        # a key nothing reads is exactly the inert-config bug again.
        self.assertEqual(set(block), {"envelope", "compaction"})
        self.assertEqual(set(block["envelope"]), {"enabled", "max_nudges"})
        self.assertEqual(set(block["compaction"]), {"enabled"})


class EnvelopeTests(unittest.TestCase):
    def test_a_complete_block_passes(self):
        text = "prose\n[OUTPUT: report]\nbody here\n[END OUTPUT: report]"
        self.assertFalse(envelope.check(
            {"last_assistant_message": text}, expected="report")[0])

    def test_a_bare_close_tag_passes(self):
        """conductor._OUTPUT_RE accepts both spellings, so this must too."""
        text = "[OUTPUT: report]\nbody\n[END OUTPUT]"
        self.assertTrue(envelope.has_block(text, "report"))

    def test_a_missing_block_nudges(self):
        nudge, reason = envelope.check(
            {"last_assistant_message": "all done, see above"},
            expected="report")
        self.assertTrue(nudge)
        self.assertIn("[OUTPUT: report]", reason)

    def test_an_empty_block_nudges(self):
        """The incident shape: the marker present but the body elsewhere."""
        nudge, _ = envelope.check(
            {"last_assistant_message": "[OUTPUT: report][END OUTPUT: report]"},
            expected="report")
        self.assertTrue(nudge)

    def test_the_wrong_block_name_nudges(self):
        nudge, _ = envelope.check(
            {"last_assistant_message": "[OUTPUT: other]\nx\n[END OUTPUT: other]"},
            expected="report")
        self.assertTrue(nudge)

    def test_an_empty_final_message_is_left_alone(self):
        """A provider-level failure the harness already handles."""
        self.assertFalse(envelope.check(
            {"last_assistant_message": "  "}, expected="report")[0])

    def test_no_op_outside_a_harness_turn(self):
        """The flaw a live test found.

        A bare `claude -p` turn was nudged into inventing an
        [OUTPUT: result] label because the hook demanded a block the turn
        was never asked for. Gated on the harness-turn env var now.
        """
        import io as _stdio
        from contextlib import redirect_stdout

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_io.ENV_ACTIVE, None)
            buf = _stdio.StringIO()
            with mock.patch.object(
                _io, "read_payload",
                return_value={"last_assistant_message": "no block here"},
            ), redirect_stdout(buf):
                rc = envelope.main([])
            self.assertEqual(rc, _io.EXIT_OK)
            self.assertEqual(buf.getvalue().strip(), "",
                             "the hook spoke when it should have stayed quiet")

    def test_nudges_are_capped(self):
        with TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {
                _io.ENV_ACTIVE: "1",
                envelope.ENV_STATE_DIR: td,
                envelope.ENV_EXPECTED: "report",
                envelope.ENV_MAX_NUDGES: "1",
            }):
                payload = {"session_id": "abc",
                           "last_assistant_message": "nothing"}
                with mock.patch.object(_io, "read_payload", return_value=payload):
                    import io as _stdio
                    from contextlib import redirect_stdout

                    first = _stdio.StringIO()
                    with redirect_stdout(first):
                        envelope.main([])
                    self.assertIn("block", first.getvalue())

                    second = _stdio.StringIO()
                    with redirect_stdout(second):
                        envelope.main([])
                    self.assertEqual(
                        second.getvalue().strip(), "",
                        "a capped hook must let the turn end",
                    )


class CompactionTests(unittest.TestCase):
    def test_records_a_health_event_per_compaction(self):
        with TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {
                compaction.ENV_STATE_DIR: td,
                compaction.ENV_AGENT: "worker",
                compaction.ENV_CYCLE: "4",
            }):
                kind = compaction.record({
                    "hook_event_name": "PreCompact",
                    "trigger_reason": "auto",
                    "session_id": "abcdef123",
                })
            self.assertEqual(kind, compaction.KIND_PRE)
            log = Path(td) / "health_events.jsonl"
            self.assertTrue(log.is_file())
            row = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(row["kind"], compaction.KIND_PRE)
            self.assertEqual(row["agent"], "worker")
            self.assertEqual(row["cycle"], 4)
            self.assertIn("trigger=auto", row["detail"])

    def test_post_compact_gets_its_own_kind(self):
        with TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {compaction.ENV_STATE_DIR: td}):
                self.assertEqual(
                    compaction.record({"hook_event_name": "PostCompact"}),
                    compaction.KIND_POST,
                )

    def test_never_raises_on_a_bad_payload(self):
        for p in ({}, {"hook_event_name": None}, {"trigger_reason": 5}):
            compaction.record(p)


class InstallerTests(unittest.TestCase):
    def test_installs_all_three_vendors(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            for vendor in hi.VENDORS:
                res = hi.install(vendor, tuple(hi.HOOK_EVENTS), d)
                if res["events"]:
                    self.assertTrue(Path(res["config"]).is_file(), vendor)
                else:
                    self.assertFalse(Path(res["config"]).exists(), vendor)

    def test_gemini_skips_the_events_it_lacks(self):
        with TemporaryDirectory() as td:
            res = hi.install("gemini", tuple(hi.HOOK_EVENTS), Path(td))
            self.assertEqual(res["events"], {})
            self.assertEqual(sorted(res["skipped"]), ["compaction", "envelope"])

    def test_a_vendor_with_nothing_to_install_is_not_touched(self):
        """These files belong to the operator; do not create one for nothing."""
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("gemini", tuple(hi.HOOK_EVENTS), d)
            self.assertFalse(hi.config_path("gemini", d).exists())
            self.assertFalse((d / ".gemini").exists())

    def test_codex_writes_hooks_json_not_settings(self):
        self.assertTrue(str(hi.config_path("codex")).endswith("hooks.json"))
        self.assertTrue(str(hi.config_path("claude")).endswith("settings.json"))

    def test_an_operators_own_hooks_survive_install(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            cfg = hi.config_path("claude", d)
            cfg.parent.mkdir(parents=True, exist_ok=True)
            mine = {"hooks": {"Stop": [
                {"hooks": [
                    {"type": "command", "command": "/usr/local/bin/my-own.sh"}]}
            ]}}
            cfg.write_text(json.dumps(mine))
            hi.install("claude", ("envelope",), d)
            after = json.loads(cfg.read_text())
            commands = [
                h["command"]
                for e in after["hooks"]["Stop"] for h in e["hooks"]
            ]
            self.assertIn("/usr/local/bin/my-own.sh", commands)
            self.assertTrue(
                any(hi.SHIM_PREFIX in Path(c).name for c in commands),
                f"our fence entry is missing: {commands}",
            )

    def test_reinstall_does_not_duplicate_our_entry(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d)
            hi.install("claude", ("envelope",), d)
            after = json.loads(hi.config_path("claude", d).read_text())
            ours = [
                h for e in after["hooks"]["Stop"] for h in e["hooks"]
                if hi.SHIM_PREFIX in Path(h["command"]).name
            ]
            self.assertEqual(len(ours), 1)

    def test_uninstall_removes_only_ours(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            cfg = hi.config_path("claude", d)
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps({"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "/keep/me.sh"}]}
            ]}}))
            hi.install("claude", ("envelope",), d)
            hi.uninstall("claude", d)
            after = json.loads(cfg.read_text())
            commands = [
                h["command"]
                for e in after.get("hooks", {}).get("Stop", [])
                for h in e["hooks"]
            ]
            self.assertEqual(commands, ["/keep/me.sh"])
            self.assertFalse(hi.shim_path("claude", "envelope", d).exists())

    def test_dry_run_writes_nothing(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d, dry_run=True)
            self.assertFalse(hi.config_path("claude", d).exists())

    def test_the_shim_is_executable_and_names_the_module(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d)
            sp = hi.shim_path("claude", "envelope", d)
            self.assertTrue(os.access(sp, os.X_OK))
            self.assertIn("long_exposure.hooks.envelope", sp.read_text())

    def test_an_operator_script_under_a_similar_path_is_not_ours(self):
        """The marker is the shim basename, not a loose substring."""
        self.assertFalse(hi._is_ours({"hooks": [
            {"type": "command", "command": "/opt/long-exposure-tools/mine.sh"}
        ]}))
        self.assertTrue(hi._is_ours({"hooks": [
            {"type": "command", "command": "/x/long-exposure-envelope.sh"}
        ]}))


class VerifyTests(unittest.TestCase):
    """`--verify` is a smoke check: does the installed shim actually run?

    Not a gate. Both remaining hooks are non-blocking, so a broken one costs
    an unenforced nudge or a missing log line rather than a wrong run.
    """

    def test_verify_passes_on_a_real_install(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope", "compaction"), d)
            for hook in ("envelope", "compaction"):
                ok, detail = hi.verify("claude", hook, d)
                self.assertTrue(ok, detail)

    def test_verify_fails_when_not_installed(self):
        with TemporaryDirectory() as td:
            ok, detail = hi.verify("claude", "envelope", Path(td))
            self.assertFalse(ok)
            self.assertIn("missing", detail)

    def test_verify_fails_when_the_shim_lost_its_execute_bit(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d)
            hi.shim_path("claude", "envelope", d).chmod(0o644)
            ok, detail = hi.verify("claude", "envelope", d)
            self.assertFalse(ok)
            self.assertIn("executable", detail)

    def test_verify_fails_on_a_shim_that_errors(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d)
            sp = hi.shim_path("claude", "envelope", d)
            sp.write_text("#!/bin/sh\nexit 9\n")
            sp.chmod(0o755)
            ok, detail = hi.verify("claude", "envelope", d)
            self.assertFalse(ok)
            self.assertIn("exited 9", detail)

    def test_verify_fails_on_non_json_output(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d)
            sp = hi.shim_path("claude", "envelope", d)
            sp.write_text("#!/bin/sh\necho not-json\n")
            sp.chmod(0o755)
            ok, detail = hi.verify("claude", "envelope", d)
            self.assertFalse(ok)
            self.assertIn("non-JSON", detail)

    def test_the_envelope_shim_nudges_through_a_real_subprocess(self):
        """End to end through the shim, as a vendor would invoke it."""
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("envelope",), d)
            sp = hi.shim_path("claude", "envelope", d)
            env = dict(os.environ, **{
                _io.ENV_ACTIVE: "1",
                envelope.ENV_EXPECTED: "report",
                envelope.ENV_STATE_DIR: td,
            })
            proc = subprocess.run(
                [str(sp)], text=True, capture_output=True, timeout=30, env=env,
                input=json.dumps({
                    "hook_event_name": "Stop", "session_id": "s",
                    "last_assistant_message": "done, see above",
                }),
            )
            out = json.loads(proc.stdout)
            self.assertEqual(out["decision"], "block")
            self.assertIn("[OUTPUT: report]", out["reason"])


if __name__ == "__main__":
    unittest.main()
