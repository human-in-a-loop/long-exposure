"""Vendor-neutral lifecycle hooks: the fence, the envelope, compaction.

Two properties carry most of the weight. Every hook must be a no-op outside
a long-exposure agent turn — a live test found the ungated envelope hook
nudging a bare `claude -p` turn into inventing an [OUTPUT:] label it was
never asked for. And the fence's fail-closed check must prove the fence
ENFORCES, not merely that a config entry exists.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from long_exposure import hooks_install as hi
from long_exposure.hooks import _io, compaction, envelope, fence


def _payload(command="", **extra):
    p = {
        "session_id": "s1",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    p.update(extra)
    return p


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
        self.assertIn("LONG_EXPOSURE_HARNESS_ROOT", env)

    def test_fence_scope_and_git_authority_come_from_config(self):
        from long_exposure.orchestrator import _add_hook_env

        env = {}
        _add_hook_env(env, {"hooks": {"fence": {"scope": "always"}},
                            "git_sync": {"harness_commits_only": True}})
        self.assertEqual(env["LONG_EXPOSURE_FENCE_SCOPE"], "always")
        self.assertEqual(env["LONG_EXPOSURE_GIT_HARNESS_ONLY"], "1")

        env = {}
        _add_hook_env(env, {"hooks": {"fence": {"scope": "nonsense"}}})
        self.assertNotIn("LONG_EXPOSURE_FENCE_SCOPE", env)


class FenceTests(unittest.TestCase):
    def test_denies_the_harness_root(self):
        root = fence.harness_root()
        deny, reason = fence.check(_payload(f"ls {root}/long_exposure"))
        self.assertTrue(deny)
        self.assertIn(root, reason)

    def test_denies_each_off_limits_path(self):
        for path in fence.denied_paths():
            deny, _ = fence.check(_payload(f"cat {path}/something"))
            self.assertTrue(deny, path)

    def test_denies_reads_not_just_writes(self):
        """A read of a private key is as bad as a write."""
        key = str(Path.home() / ".ssh" / "id_ed25519")
        self.assertTrue(fence.check(_payload(f"cat {key}"))[0])

    def test_allows_ordinary_commands(self):
        for cmd in ("python3 scripts/sweep.py", "pytest -q",
                    "ls data/", "git status", "git add -A",
                    "echo hello > out.txt"):
            self.assertFalse(fence.check(_payload(cmd))[0], cmd)

    def test_checks_path_carrying_tool_inputs_too(self):
        root = fence.harness_root()
        deny, _ = fence.check({
            "tool_name": "Write",
            "tool_input": {"file_path": f"{root}/long_exposure/x.py"},
        })
        self.assertTrue(deny)

    def test_empty_and_malformed_payloads_are_no_opinion(self):
        for p in ({}, {"tool_input": None}, {"tool_input": {}},
                  {"tool_input": {"command": ""}}):
            self.assertFalse(fence.check(p)[0], p)

    def test_selftest_marker_always_denies(self):
        deny, reason = fence.check(_payload(f"echo {fence.SELFTEST_MARKER}"))
        self.assertTrue(deny)
        self.assertIn("self-test", reason)

    def test_git_writes_blocked_only_when_harness_owns_commits(self):
        cmd = "git commit -m 'work'"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fence.ENV_GIT_AUTHORITY, None)
            self.assertFalse(fence.check(_payload(cmd))[0])
        with mock.patch.dict(os.environ, {fence.ENV_GIT_AUTHORITY: "1"}):
            deny, reason = fence.check(_payload(cmd))
            self.assertTrue(deny)
            self.assertIn("commit", reason)

    def test_read_only_git_stays_allowed_under_harness_authority(self):
        with mock.patch.dict(os.environ, {fence.ENV_GIT_AUTHORITY: "1"}):
            for cmd in ("git status", "git diff", "git log --oneline",
                        "git add -A", "git show HEAD"):
                self.assertFalse(fence.check(_payload(cmd))[0], cmd)

    def test_git_log_format_commit_is_not_a_commit(self):
        with mock.patch.dict(os.environ, {fence.ENV_GIT_AUTHORITY: "1"}):
            self.assertFalse(
                fence.check(_payload("git log --format=%H"))[0]
            )

    def test_default_scope_is_turn(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fence.ENV_SCOPE, None)
            self.assertEqual(fence.scope(), "turn")
        with mock.patch.dict(os.environ, {fence.ENV_SCOPE: "always"}):
            self.assertEqual(fence.scope(), "always")
        with mock.patch.dict(os.environ, {fence.ENV_SCOPE: "garbage"}):
            self.assertEqual(fence.scope(), "turn")

    def test_env_override_for_the_harness_root(self):
        with mock.patch.dict(os.environ,
                             {fence.ENV_HARNESS_ROOT: "/opt/le"}):
            self.assertEqual(fence.harness_root(), "/opt/le")

    def test_the_prompt_and_the_fence_have_not_drifted(self):
        """The template is the operator-facing statement of this list."""
        tpl = Path(
            "long_exposure/templates/operating-protocol-template.md"
        ).read_text()
        for name in fence.HOME_RELATIVE_DENY:
            self.assertIn(f"~/{name}", tpl, f"{name} missing from the prompt")
        self.assertIn("{harness_root}", tpl)


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
                res = hi.install(vendor, ("fence", "envelope", "compaction"), d)
                self.assertTrue(Path(res["config"]).is_file(), vendor)

    def test_gemini_skips_the_events_it_lacks(self):
        with TemporaryDirectory() as td:
            res = hi.install("gemini", ("fence", "envelope", "compaction"),
                             Path(td))
            self.assertIn("BeforeTool", res["events"])
            self.assertEqual(sorted(res["skipped"]), ["compaction", "envelope"])

    def test_codex_writes_hooks_json_not_settings(self):
        self.assertTrue(str(hi.config_path("codex")).endswith("hooks.json"))
        self.assertTrue(str(hi.config_path("claude")).endswith("settings.json"))

    def test_an_operators_own_hooks_survive_install(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            cfg = hi.config_path("claude", d)
            cfg.parent.mkdir(parents=True, exist_ok=True)
            mine = {"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "/usr/local/bin/my-own.sh"}]}
            ]}}
            cfg.write_text(json.dumps(mine))
            hi.install("claude", ("fence",), d)
            after = json.loads(cfg.read_text())
            commands = [
                h["command"]
                for e in after["hooks"]["PreToolUse"] for h in e["hooks"]
            ]
            self.assertIn("/usr/local/bin/my-own.sh", commands)
            self.assertTrue(
                any(hi.SHIM_PREFIX in Path(c).name for c in commands),
                f"our fence entry is missing: {commands}",
            )

    def test_reinstall_does_not_duplicate_our_entry(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            hi.install("claude", ("fence",), d)
            after = json.loads(hi.config_path("claude", d).read_text())
            ours = [
                h for e in after["hooks"]["PreToolUse"] for h in e["hooks"]
                if hi.SHIM_PREFIX in Path(h["command"]).name
            ]
            self.assertEqual(len(ours), 1)

    def test_uninstall_removes_only_ours(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            cfg = hi.config_path("claude", d)
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"hooks": [{"type": "command", "command": "/keep/me.sh"}]}
            ]}}))
            hi.install("claude", ("fence",), d)
            hi.uninstall("claude", d)
            after = json.loads(cfg.read_text())
            commands = [
                h["command"]
                for e in after.get("hooks", {}).get("PreToolUse", [])
                for h in e["hooks"]
            ]
            self.assertEqual(commands, ["/keep/me.sh"])
            self.assertFalse(hi.shim_path("claude", "fence", d).exists())

    def test_dry_run_writes_nothing(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d, dry_run=True)
            self.assertFalse(hi.config_path("claude", d).exists())

    def test_the_shim_is_executable_and_names_the_module(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            sp = hi.shim_path("claude", "fence", d)
            self.assertTrue(os.access(sp, os.X_OK))
            self.assertIn("long_exposure.hooks.fence", sp.read_text())

    def test_an_operator_script_under_a_similar_path_is_not_ours(self):
        """The marker is the shim basename, not a loose substring."""
        self.assertFalse(hi._is_ours({"hooks": [
            {"type": "command", "command": "/opt/long-exposure-tools/mine.sh"}
        ]}))
        self.assertTrue(hi._is_ours({"hooks": [
            {"type": "command", "command": "/x/long-exposure-fence.sh"}
        ]}))


class VerifyFenceTests(unittest.TestCase):
    """Fail-closed means EXERCISING the fence, not reading a config file."""

    def test_verify_passes_on_a_real_install(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            ok, detail = hi.verify_fence("claude", d)
            self.assertTrue(ok, detail)

    def test_verify_fails_when_not_installed(self):
        with TemporaryDirectory() as td:
            ok, detail = hi.verify_fence("claude", Path(td))
            self.assertFalse(ok)
            self.assertIn("missing", detail)

    def test_verify_fails_when_the_shim_lost_its_execute_bit(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            hi.shim_path("claude", "fence", d).chmod(0o644)
            ok, detail = hi.verify_fence("claude", d)
            self.assertFalse(ok)
            self.assertIn("executable", detail)

    def test_verify_fails_when_the_fence_does_not_deny(self):
        """A config entry existing is not the same as the fence working."""
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            sp = hi.shim_path("claude", "fence", d)
            sp.write_text("#!/bin/sh\nexit 0\n")   # installed, inert
            sp.chmod(0o755)
            ok, detail = hi.verify_fence("claude", d)
            self.assertFalse(ok)
            self.assertIn("not enforcing", detail)

    def test_the_shim_really_denies_when_run_as_a_subprocess(self):
        """End to end through the shim, as a vendor would invoke it."""
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            sp = hi.shim_path("claude", "fence", d)
            payload = json.dumps(_payload(
                f"cat {fence.harness_root()}/long_exposure/cli.py"
            ))
            env = dict(os.environ, **{_io.ENV_ACTIVE: "1"})
            proc = subprocess.run([str(sp)], input=payload, text=True,
                                  capture_output=True, timeout=30, env=env)
            out = json.loads(proc.stdout)
            self.assertEqual(
                out["hookSpecificOutput"]["permissionDecision"], "deny",
            )

    def test_the_shim_allows_an_ordinary_command(self):
        with TemporaryDirectory() as td:
            d = Path(td)
            hi.install("claude", ("fence",), d)
            sp = hi.shim_path("claude", "fence", d)
            env = dict(os.environ, **{_io.ENV_ACTIVE: "1"})
            proc = subprocess.run(
                [str(sp)], input=json.dumps(_payload("pytest -q")),
                text=True, capture_output=True, timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
