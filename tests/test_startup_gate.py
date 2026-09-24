"""The startup gate (Feature 3 of docs/advanced-model-modes-plan.md).

Two properties carry most of the weight here: a non-TTY must never guess an
answer (a wrong model is discovered three hours and many dollars in), and Q1
must reach the `agent_models` routing table without flattening a deliberate
heterogeneous routing.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from long_exposure import startup_gate as g
from long_exposure.orchestrator import load_config


def _cfg(tmp: Path, **gate):
    cfg = load_config()
    block = {
        "enabled": True,
        "model_choices": ["opus", "fable", "sonnet"],
        "instances_root": str(tmp / "instances"),
        "registry_path": str(tmp / "runs.jsonl"),
        "max_runs_listed": 10,
    }
    block.update(gate)
    cfg["startup_gate"] = block
    return cfg


def _state(path: Path, **fields) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cycle": 1, **fields}))
    return path


def _scripted(*answers):
    it = iter(answers)

    def fake_input(_prompt):
        return next(it)

    return fake_input


class ConfigTests(unittest.TestCase):
    def test_shipped_config_ships_the_gate_off(self):
        self.assertFalse(g.enabled(load_config()))

    def test_absent_block_is_off(self):
        self.assertFalse(g.enabled({}))
        self.assertFalse(g.enabled(None))

    def test_defaults_fill_in(self):
        s = g.settings({"startup_gate": {"enabled": True}})
        self.assertEqual(s["model_choices"], ["opus", "fable", "sonnet"])
        self.assertEqual(s["max_runs_listed"], 10)


class ApplyModelTests(unittest.TestCase):
    """Q1's rule: reach the routing table, but do not flatten it."""

    def test_rewrites_every_role_still_on_the_old_default(self):
        cfg = load_config()
        rewritten = g.apply_model(cfg, "claude-fable-5-1")
        self.assertEqual(cfg["model"], "claude-fable-5-1")
        self.assertEqual(len(rewritten), 8)
        for routing in cfg["agent_models"].values():
            self.assertEqual(routing["model"], "claude-fable-5-1")

    def test_leaves_a_deliberate_heterogeneous_routing_alone(self):
        cfg = load_config()
        cfg["agent_models"]["worker"] = {
            "provider": "codex", "model": "gpt-5.5", "effort": "high",
        }
        cfg["agent_models"]["reporter"] = {
            "provider": "claude", "model": "sonnet", "effort": "medium",
        }
        rewritten = g.apply_model(cfg, "claude-fable-5-1")
        self.assertNotIn("worker", rewritten)
        self.assertNotIn("reporter", rewritten)
        self.assertEqual(cfg["agent_models"]["worker"]["model"], "gpt-5.5")
        self.assertEqual(cfg["agent_models"]["reporter"]["model"], "sonnet")
        self.assertEqual(cfg["agent_models"]["auditor"]["model"], "claude-fable-5-1")

    def test_a_role_with_no_model_set_is_adopted(self):
        cfg = load_config()
        cfg["agent_models"]["curator"] = {"provider": "claude", "effort": "low"}
        rewritten = g.apply_model(cfg, "fable")
        self.assertIn("curator", rewritten)
        self.assertEqual(cfg["agent_models"]["curator"]["model"], "fable")

    def test_no_agent_models_block_is_survivable(self):
        cfg = {"model": "opus"}
        self.assertEqual(g.apply_model(cfg, "fable"), [])
        self.assertEqual(cfg["model"], "fable")

    def test_routing_table_names_every_role_and_its_profile(self):
        cfg = load_config()
        cfg["model_profiles"] = {
            "enabled": True, "auto": True, "families": {"advanced": ["fable"]},
        }
        g.apply_model(cfg, "claude-fable-5-1")
        table = g.routing_table(cfg)
        for role in (
            "researcher", "worker", "auditor", "reporter",
            "final_auditor", "final_reporter", "manager", "curator",
        ):
            self.assertIn(role, table)
        self.assertIn("advanced", table)


class RegistryTests(unittest.TestCase):
    def test_register_then_list(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            sp = _state(tmp / "a" / "exploration_state.json", cycle=12)
            g.register_run(cfg, run_id="run-A", instance_dir=tmp / "a",
                           state_path=sp, task="study the widget")
            runs = g.list_runs(cfg)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_id"], "run-A")
            self.assertTrue(runs[0]["resumable"])
            self.assertEqual(runs[0]["cycle"], 12)

    def test_newest_first(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            for name in ("a", "b", "c"):
                sp = _state(tmp / name / "exploration_state.json")
                g.register_run(cfg, run_id=name, instance_dir=tmp / name,
                               state_path=sp, task=name)
            self.assertEqual([r["run_id"] for r in g.list_runs(cfg)], ["c", "b", "a"])

    def test_a_resumed_run_supersedes_its_earlier_entry(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            sp = _state(tmp / "a" / "exploration_state.json")
            g.register_run(cfg, run_id="run-A", instance_dir=tmp / "a",
                           state_path=sp, task="first")
            g.register_run(cfg, run_id="run-A", instance_dir=tmp / "a",
                           state_path=sp, task="second")
            runs = g.list_runs(cfg)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["task"], "second")

    def test_a_vanished_state_file_is_a_tombstone_not_a_silent_drop(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            g.register_run(cfg, run_id="gone", instance_dir=tmp / "g",
                           state_path=tmp / "g" / "exploration_state.json",
                           task="deleted")
            runs = g.list_runs(cfg)
            self.assertEqual(len(runs), 1)
            self.assertFalse(runs[0]["resumable"])
            self.assertIn("cannot resume", g._run_label(runs[0]))

    def test_a_torn_line_does_not_hide_the_rest_of_the_registry(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            sp = _state(tmp / "a" / "exploration_state.json")
            g.register_run(cfg, run_id="good", instance_dir=tmp / "a",
                           state_path=sp, task="ok")
            with (tmp / "runs.jsonl").open("a") as fh:
                fh.write('{"run_id": "torn", "state_pa\n')
            self.assertEqual([r["run_id"] for r in g.list_runs(cfg)], ["good"])

    def test_missing_registry_is_not_an_error(self):
        with TemporaryDirectory() as td:
            self.assertEqual(g.list_runs(_cfg(Path(td))), [])

    def test_max_runs_listed_bounds_the_menu(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp, max_runs_listed=3)
            for i in range(8):
                sp = _state(tmp / f"r{i}" / "exploration_state.json")
                g.register_run(cfg, run_id=f"r{i}", instance_dir=tmp / f"r{i}",
                               state_path=sp, task="t")
            self.assertEqual(len(g.list_runs(cfg)), 3)

    def test_instances_root_scan_is_the_fallback(self):
        """Runs that predate the registry are still offerable."""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            _state(tmp / "instances" / "old-run" / "exploration_state.json",
                   cycle=44, run_id="legacy", task="from before the registry")
            runs = g.list_runs(cfg)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_id"], "legacy")
            self.assertEqual(runs[0]["cycle"], 44)
            self.assertTrue(runs[0]["resumable"])

    def test_registry_wins_over_the_scan_when_both_exist(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            _state(tmp / "instances" / "old" / "exploration_state.json",
                   run_id="legacy")
            sp = _state(tmp / "a" / "exploration_state.json")
            g.register_run(cfg, run_id="registered", instance_dir=tmp / "a",
                           state_path=sp, task="t")
            self.assertEqual([r["run_id"] for r in g.list_runs(cfg)],
                             ["registered"])

    def test_clones_are_never_registered(self):
        """A fork is not a resumable run."""
        import os
        from unittest.mock import patch

        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _cfg(tmp)
            sp = _state(tmp / "a" / "exploration_state.json")
            with patch.dict(os.environ, {"AGENT_FORK_ID": "fork-xyz"}):
                g.register_run(cfg, run_id="clone", instance_dir=tmp / "a",
                               state_path=sp, task="branch work")
            self.assertEqual(g.list_runs(cfg), [])


class AskTests(unittest.TestCase):
    def _base(self, tmp):
        cfg = _cfg(tmp)
        (tmp / "ws").mkdir()
        return cfg

    def test_every_flag_suppresses_its_question(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            answers = g.ask(
                cfg,
                flags={"model": "fable", "workspace": str(tmp / "ws"),
                       "resume": "fresh"},
                tty=False,
            )
            self.assertEqual(answers["model"], "fable")
            self.assertEqual(answers["resume"], g.FRESH)

    def test_non_tty_without_a_flag_aborts_and_names_the_flag(self):
        with TemporaryDirectory() as td:
            cfg = self._base(Path(td))
            with self.assertRaises(g.GateAbort) as ctx:
                g.ask(cfg, flags={}, tty=False)
            self.assertEqual(ctx.exception.missing_flag, "--gate-model")
            self.assertIn("--gate-model", str(ctx.exception))
            self.assertIn("--no-gate", str(ctx.exception))

    def test_abort_has_its_own_exit_code(self):
        self.assertEqual(g.GateAbort.EXIT_CODE, 4)

    def test_fresh_spellings_all_normalize_to_the_sentinel(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            for spelling in ("fresh", "FRESH", "Fresh", "new"):
                answers = g.ask(
                    cfg,
                    flags={"model": "opus", "workspace": str(tmp / "ws"),
                           "resume": spelling},
                    tty=False,
                )
                self.assertEqual(answers["resume"], g.FRESH, spelling)

    def test_resume_flag_pointing_at_a_vanished_state_file_is_refused(self):
        """Accepting it would start fresh at that path, losing the intent."""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            with self.assertRaises(g.GateAbort) as ctx:
                g.ask(
                    cfg,
                    flags={"model": "opus", "workspace": str(tmp / "ws"),
                           "resume": str(tmp / "nope" / "exploration_state.json")},
                    tty=False,
                )
            self.assertIn("cannot resume", str(ctx.exception))

    def test_resume_flag_for_a_run_absent_from_the_registry_still_works(self):
        """The path is the truth, not the listing."""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            sp = _state(tmp / "unlisted" / "exploration_state.json")
            answers = g.ask(
                cfg,
                flags={"model": "opus", "workspace": str(tmp / "ws"),
                       "resume": str(sp)},
                tty=False,
            )
            self.assertEqual(answers["resume"], str(sp))

    def test_a_missing_workspace_aborts(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            with self.assertRaises(g.GateAbort) as ctx:
                g.ask(cfg, flags={"model": "opus",
                                  "workspace": str(tmp / "nope"),
                                  "resume": "fresh"}, tty=False)
            self.assertIn("does not exist", str(ctx.exception))

    def test_q4_appears_only_when_the_spend_limit_is_enabled(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            flags = {"model": "opus", "workspace": str(tmp / "ws"),
                     "resume": "fresh"}
            self.assertNotIn("usage_run_pct", g.ask(cfg, flags=flags, tty=False))

            cfg["usage_allowance"] = {
                "enabled": True, "weekly_allowance_usd": 400, "run_pct": 0,
            }
            with self.assertRaises(g.GateAbort) as ctx:
                g.ask(cfg, flags=flags, tty=False)
            self.assertEqual(ctx.exception.missing_flag, "--gate-usage-pct")

            answered = g.ask(cfg, flags={**flags, "usage_run_pct": 20}, tty=False)
            self.assertEqual(answered["usage_run_pct"], 20.0)

    def test_no_previous_runs_means_fresh_without_asking(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            answers = g.ask(
                cfg,
                flags={"model": "opus", "workspace": str(tmp / "ws")},
                tty=False,   # would abort if Q3 had to be asked
            )
            self.assertEqual(answers["resume"], g.FRESH)

    def test_interactive_menu_selection(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            # Q1 -> choice 2 (fable); Q2 -> "other" then the path; Q3 -> no runs
            answers = g.ask(
                cfg,
                input_fn=_scripted("2", "3", str(tmp / "ws")),
                tty=True,
            )
            self.assertEqual(answers["model"], "fable")
            self.assertEqual(answers["workspace"], str((tmp / "ws").resolve()))

    def test_a_typed_value_is_accepted_without_counting_menu_positions(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            answers = g.ask(
                cfg,
                flags={"workspace": str(tmp / "ws"), "resume": "fresh"},
                input_fn=_scripted("sonnet"),
                tty=True,
            )
            self.assertEqual(answers["model"], "sonnet")

    def test_an_out_of_range_choice_reprompts(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._base(tmp)
            answers = g.ask(
                cfg,
                flags={"workspace": str(tmp / "ws"), "resume": "fresh"},
                input_fn=_scripted("99", "", "1"),
                tty=True,
            )
            self.assertEqual(answers["model"], "opus")


class PersistenceTests(unittest.TestCase):
    def test_answers_round_trip(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            path = g.answers_path(tmp, tmp / "exploration_state.json")
            answers = {"model": "fable", "workspace": str(tmp),
                       "resume": g.FRESH, "usage_run_pct": 20.0}
            saved = g.save_answers(path, answers)
            loaded = g.load_answers(saved)
            self.assertEqual(loaded["model"], "fable")
            self.assertEqual(loaded["usage_run_pct"], 20.0)
            self.assertIn("answered_at", loaded)

    def test_answers_path_prefers_the_instance_dir(self):
        self.assertEqual(
            g.answers_path("/inst", "/elsewhere/exploration_state.json"),
            Path("/inst") / g.ANSWERS_FILENAME,
        )
        self.assertEqual(
            g.answers_path(None, "/elsewhere/exploration_state.json"),
            Path("/elsewhere") / g.ANSWERS_FILENAME,
        )

    def test_loading_a_missing_or_corrupt_file_is_none(self):
        with TemporaryDirectory() as td:
            self.assertIsNone(g.load_answers(Path(td) / "nope.json"))
            bad = Path(td) / "bad.json"
            bad.write_text("{not json")
            self.assertIsNone(g.load_answers(bad))
            arr = Path(td) / "arr.json"
            arr.write_text("[1,2]")
            self.assertIsNone(g.load_answers(arr))


class ApplyAnswersTests(unittest.TestCase):
    def test_applies_model_workspace_and_percentage(self):
        cfg = load_config()
        cfg["usage_allowance"] = {
            "enabled": True, "weekly_allowance_usd": 400, "run_pct": 0,
        }
        summary = g.apply_answers(cfg, {
            "model": "claude-fable-5-1",
            "workspace": "/tmp",
            "usage_run_pct": 25.0,
        })
        self.assertEqual(cfg["model"], "claude-fable-5-1")
        self.assertEqual(cfg["working_directory"], "/tmp")
        self.assertEqual(cfg["usage_allowance"]["run_pct"], 25.0)
        self.assertIn("$100.00", summary["spend_limit"])

    def test_empty_answers_change_nothing(self):
        cfg = load_config()
        before = json.dumps(cfg, sort_keys=True, default=str)
        g.apply_answers(cfg, {})
        self.assertEqual(json.dumps(cfg, sort_keys=True, default=str), before)


class CliWiringTests(unittest.TestCase):
    def test_launch_has_every_gate_flag(self):
        from long_exposure.cli import build_parser

        help_text = build_parser().format_help()
        src = Path("long_exposure/cli.py").read_text()
        for flag in ("--no-gate", "--gate-model", "--gate-workspace",
                     "--gate-resume", "--gate-usage-pct"):
            self.assertIn(flag, src, flag)
        self.assertIn("launch", help_text)

    def test_only_launch_runs_the_gate(self):
        """start / resume must stay headless for cron and clone spawns."""
        src = Path("long_exposure/cli.py").read_text()
        launch_start = src.index("def _launch(")
        launch_end = src.index("def _cli_install(")
        self.assertIn("_gate.ask(", src[launch_start:launch_end])
        self.assertEqual(src.count("_gate.ask("), 1)

    def test_gate_abort_maps_to_its_exit_code(self):
        src = Path("long_exposure/cli.py").read_text()
        self.assertIn("_gate.GateAbort.EXIT_CODE", src)

    def test_run_exploration_reads_persisted_answers(self):
        src = Path("long_exposure/exploration.py").read_text()
        self.assertIn("_gate.load_answers(", src)
        self.assertIn("_gate.apply_answers(", src)
        self.assertIn("_gate.register_run(", src)

    def test_answers_are_applied_before_routing_is_merged(self):
        """Otherwise the model answer never reaches the score's agent defs."""
        src = Path("long_exposure/exploration.py").read_text()
        self.assertLess(
            src.index("_gate.apply_answers("),
            src.index("agent_routing.apply_agent_models(config, score)"),
        )


if __name__ == "__main__":
    unittest.main()
