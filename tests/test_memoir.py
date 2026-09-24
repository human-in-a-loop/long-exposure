"""Run memoir (L1 narrative memory). Offline: every provider call is patched.

Covers the decisions in docs/tiered-memory-plan.md §4: contents injected into
researcher and worker only, the auditor gets the path, archive only on
change (file + sessions.db row), clones read but never write, and the
off switch strips the inputs rather than rendering [UNAVAILABLE].
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import memoir, paths, telemetry
from long_exposure import provider as _provider
from long_exposure.conductor import build_agent_prompt
from long_exposure.exploration import run_exploration
from auto_compact.db import init_db

from test_run_switches import _write_files


class MemoirModuleTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        self.ws.mkdir()
        paths.ensure_layout(self.ws)

    def tearDown(self):
        self.td.cleanup()

    def test_seed_writes_skeleton_once(self):
        self.assertTrue(memoir.seed_if_missing(self.ws))
        body = paths.memoir_path(self.ws).read_text()
        for heading in ("## Thesis", "## Standing on", "## Ruled out",
                        "## Parked", "## Where to look", "## Changed this cycle"):
            self.assertIn(heading, body)
        self.assertIn("THEY win", body)  # the ledger-wins rule travels with the file
        paths.memoir_path(self.ws).write_text("edited")
        self.assertFalse(memoir.seed_if_missing(self.ws))
        self.assertEqual(paths.memoir_path(self.ws).read_text(), "edited")

    def test_injection_has_header_and_seeds_lazily(self):
        value = memoir.read_for_injection(self.ws, {})
        self.assertTrue(value.startswith("[Run memoir — advisory"))
        self.assertIn("plan_of_record.md or the promise ledger, they win", value)
        self.assertIn("## Thesis", value)
        self.assertTrue(paths.memoir_path(self.ws).exists())

    def test_over_cap_is_truncated_with_marker_and_event(self):
        paths.memoir_path(self.ws).write_text("x" * 4000)
        seen = []
        with patch("long_exposure.memoir.health_events.append_event",
                   side_effect=lambda kind, **kw: seen.append(kind)):
            value = memoir.read_for_injection(self.ws, {"memoir": {"max_tokens": 100}})
        self.assertIn("[memoir over cap", value)
        self.assertLess(len(value), 4000)
        self.assertEqual(seen, ["memoir_over_cap"])
        # The live file is untouched — only the injected copy is cut.
        self.assertEqual(len(paths.memoir_path(self.ws).read_text()), 4000)

    def test_truncation_lands_on_a_paragraph_boundary(self):
        # 20 paragraphs of ~50 chars; cap 100 tokens = 400 chars.
        paras = [f"Paragraph {i:02d} " + "word " * 8 + "end." for i in range(20)]
        paths.memoir_path(self.ws).write_text("\n\n".join(paras))
        with patch("long_exposure.memoir.health_events.append_event"):
            value = memoir.read_for_injection(self.ws, {"memoir": {"max_tokens": 100}})
        body = value.split("\n\n", 1)[1]           # drop the injection header
        kept = body.split(memoir._TRUNCATED_MARKER)[0]
        self.assertTrue(kept.endswith("end."), kept[-40:])   # whole paragraphs only
        self.assertLessEqual(len(kept), 400)
        self.assertGreater(len(kept), 200)                    # used most of the budget
        self.assertNotIn("Paragraph 19", kept)

    def test_truncation_falls_back_to_hard_cut_without_boundaries(self):
        # One giant paragraph: no blank line to back up to → hard cut at cap*4.
        paths.memoir_path(self.ws).write_text("word " * 2000)
        with patch("long_exposure.memoir.health_events.append_event"):
            value = memoir.read_for_injection(self.ws, {"memoir": {"max_tokens": 100}})
        kept = value.split("\n\n", 1)[1].split(memoir._TRUNCATED_MARKER)[0]
        self.assertLessEqual(len(kept), 400)
        self.assertGreaterEqual(len(kept), 395)

    def test_truncation_ignores_a_boundary_that_wastes_the_budget(self):
        # A blank line only in the first tenth: backing up to it would drop
        # 90% of the allowed head, so the hard cut wins.
        text = "short intro\n\n" + "x" * 5000
        paths.memoir_path(self.ws).write_text(text)
        with patch("long_exposure.memoir.health_events.append_event"):
            value = memoir.read_for_injection(self.ws, {"memoir": {"max_tokens": 100}})
        kept = value.split("\n\n", 1)[1].split(memoir._TRUNCATED_MARKER)[0]
        self.assertGreaterEqual(len(kept), 395)

    def test_under_cap_is_not_truncated_and_no_event(self):
        paths.memoir_path(self.ws).write_text("short")
        seen = []
        with patch("long_exposure.memoir.health_events.append_event",
                   side_effect=lambda kind, **kw: seen.append(kind)):
            value = memoir.read_for_injection(self.ws, {"memoir": {"max_tokens": 3000}})
        self.assertNotIn("over cap", value)
        self.assertEqual(seen, [])

    def test_config_defaults_and_bad_values(self):
        self.assertTrue(memoir.enabled({}))
        self.assertTrue(memoir.enabled({"memoir": {}}))
        self.assertFalse(memoir.enabled({"memoir": {"enabled": False}}))
        self.assertEqual(memoir.max_tokens({}), 3000)
        self.assertEqual(memoir.max_tokens({"memoir": {"max_tokens": "nope"}}), 3000)
        self.assertEqual(memoir.max_tokens({"memoir": {"max_tokens": 0}}), 3000)
        self.assertEqual(memoir.max_tokens({"memoir": {"max_tokens": 500}}), 500)

    def test_path_input_is_the_file_this_process_may_write(self):
        # Root: the root memoir, bare.
        self.assertEqual(
            memoir.path_input_value(self.ws), str(paths.memoir_path(self.ws))
        )
        # Clone: its own shadow, not the root — so the auditor's "edit the
        # memoir at this path" guidance is true rather than contradicted.
        with tempfile.TemporaryDirectory() as inst:
            with patch.dict(os.environ, {"AGENT_FORK_ID": "ab12",
                                         "AGENT_INSTANCE_DIR": inst}):
                value = memoir.path_input_value(self.ws)
        self.assertIn(str(Path(inst) / "MEMOIR.md"), value)
        self.assertNotIn(str(paths.memoir_path(self.ws)), value)
        self.assertIn("BRANCH memoir", value)

    def test_shadow_path_only_resolves_inside_a_clone(self):
        self.assertIsNone(memoir.shadow_path())
        self.assertEqual(memoir.write_path(self.ws), paths.memoir_path(self.ws))
        with patch.dict(os.environ, {"AGENT_FORK_ID": "ab12",
                                     "AGENT_INSTANCE_DIR": "/tmp/clone-1"}):
            self.assertEqual(memoir.shadow_path(), Path("/tmp/clone-1/MEMOIR.md"))
            self.assertEqual(memoir.write_path(self.ws), Path("/tmp/clone-1/MEMOIR.md"))
        # A fork id without an instance dir cannot resolve a shadow.
        with patch.dict(os.environ, {"AGENT_FORK_ID": "ab12"}, clear=False):
            os.environ.pop("AGENT_INSTANCE_DIR", None)
            self.assertIsNone(memoir.shadow_path())

    def test_clone_reads_root_and_its_own_blank_shadow(self):
        memoir.seed_if_missing(self.ws)
        root = paths.memoir_path(self.ws)
        root.write_text(root.read_text().replace("(none yet)", "ROOT THESIS", 1))
        with tempfile.TemporaryDirectory() as inst:
            with patch.dict(os.environ, {"AGENT_FORK_ID": "ab12",
                                         "AGENT_INSTANCE_DIR": inst}):
                value = memoir.read_for_injection(self.ws, {})
                shadow = Path(inst) / "MEMOIR.md"
                self.assertTrue(shadow.exists())          # seeded lazily
                # Blank skeleton, NOT a copy of the root: branch-local delta.
                self.assertNotIn("ROOT THESIS", shadow.read_text())
                self.assertIn("## Thesis", shadow.read_text())
        self.assertIn("RUN MEMOIR (root, read-only in this branch)", value)
        self.assertIn("ROOT THESIS", value)
        self.assertIn("THIS BRANCH'S MEMOIR", value)

    def test_root_injection_has_no_branch_section(self):
        value = memoir.read_for_injection(self.ws, {})
        self.assertNotIn("THIS BRANCH", value)
        self.assertNotIn("read-only in this branch", value)

    def test_archive_only_on_content_change_with_file_and_row(self):
        memoir.seed_if_missing(self.ws)
        conn = init_db(Path(self.td.name) / "sessions.db")
        target = paths.memoir_path(self.ws)
        before = memoir.snapshot(self.ws)

        # Untouched → nothing.
        self.assertIsNone(memoir.archive_if_changed(self.ws, 3, before, conn))
        # Rewritten with IDENTICAL content (new mtime, same bytes) → still nothing.
        # This is the case a (size, mtime) signature would have archived.
        target.write_text(before)
        self.assertIsNone(memoir.archive_if_changed(self.ws, 3, before, conn))
        self.assertEqual(list(paths.memoir_history_dir(self.ws).iterdir()), [])

        # Changed → one archive whose content equals the live file, plus a row.
        target.write_text(before.replace("(none yet)", "spectral approach failed", 1))
        archived = memoir.archive_if_changed(self.ws, 3, before, conn)
        self.assertIsNotNone(archived)
        self.assertTrue(archived.name.startswith("cycle-000003_"))
        self.assertTrue(archived.name.endswith(".md"))
        self.assertEqual(archived.read_text(), target.read_text())

        rows = conn.execute(
            "SELECT record_type, topic, subtopic, summary_xml FROM sessions "
            "WHERE record_type = 'memoir'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "memoir")
        # Same spelling as the archive filename, so Grep and search agree.
        self.assertEqual(rows[0][2], "cycle-000003")
        self.assertIn("spectral approach failed", rows[0][3])
        # Findable through the same FTS table search_sessions queries.
        hit = conn.execute(
            "SELECT rowid FROM sessions_fts WHERE sessions_fts MATCH ?", ("spectral",)
        ).fetchall()
        self.assertEqual(len(hit), 1)
        conn.close()

    def test_missing_file_after_auditor_is_not_an_error(self):
        # An auditor that deletes the memoir: nothing to archive, no exception.
        self.assertIsNone(memoir.archive_if_changed(self.ws, 1, "was here", None))

    def test_memoir_rows_never_enter_gem_ranking(self):
        """Gem ranking skips lemma rows by denylist; memoir rows must be skipped
        too, or archived memoirs would compete for REPL gem slots."""
        from auto_compact.proximity import rank_sessions
        sessions = [
            {"id": "m1", "record_type": "memoir", "topic": "memoir",
             "subtopic": "cycle-000001", "created_at": "2026-09-17T00:00:00+00:00"},
            {"id": "c1", "record_type": "compaction", "topic": "memoir",
             "subtopic": "x", "created_at": "2026-09-17T00:00:00+00:00"},
        ]
        profile = {"topic_weights": {"_same_topic": 1.0, "_same_subtopic": 0.8},
                   "tool_weights": {}, "keyword_weights": {}}
        ranked = rank_sessions(
            sessions, profile, {"topic": "memoir", "subtopic": "x"}, min_score=0.0,
        )
        ids = [r["id"] for r in ranked]
        self.assertNotIn("m1", ids)
        self.assertIn("c1", ids)

    def test_strip_inputs(self):
        agents = {
            "researcher": {"inputs": ["directive", "run_memory"]},
            "auditor": {"inputs": ["directive", "memoir_path"]},
            "curator": {"inputs": ["directive"]},
            "odd": {},
        }
        memoir.strip_inputs(agents)
        self.assertEqual(agents["researcher"]["inputs"], ["directive"])
        self.assertEqual(agents["auditor"]["inputs"], ["directive"])
        self.assertEqual(agents["curator"]["inputs"], ["directive"])
        self.assertEqual(agents["odd"], {})


class MemoirFanOutTests(unittest.TestCase):
    """Fork and merge: shadows isolate the write, and the fold closes the
    two-cycle blind spot around every fan-out."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        self.inst = Path(self.td.name) / "instance"
        self.ws.mkdir(); self.inst.mkdir()
        paths.ensure_layout(self.ws)
        memoir.seed_if_missing(self.ws)

    def tearDown(self):
        self.td.cleanup()

    def _branch(self, fork: str, k: int, body: str) -> Path:
        d = self.inst / f"fork-{fork}" / f"clone-{k}"
        d.mkdir(parents=True, exist_ok=True)
        shadow = d / "MEMOIR.md"
        shadow.write_text(body)
        # Newer than the root memoir, as a real collapse would leave it.
        st = paths.memoir_path(self.ws).stat()
        os.utime(shadow, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return shadow

    def _touch_root_newer(self) -> None:
        """Simulate the root auditor's edit landing after the collapse."""
        root = paths.memoir_path(self.ws)
        root.write_text(root.read_text() + "\n- folded\n")
        newest = max(
            sh.stat().st_mtime_ns for sh in memoir._branch_shadows(self.inst)
        )
        st = root.stat()
        os.utime(root, ns=(st.st_atime_ns, newest + 1_000_000))

    def test_no_branches_is_an_explicit_empty_value(self):
        self.assertEqual(
            memoir.branch_memoirs_for_injection(self.ws, self.inst, {}),
            memoir._NO_BRANCHES,
        )
        # A clone never gets the fold input (root_instance_dir is None).
        self.assertEqual(
            memoir.branch_memoirs_for_injection(self.ws, None, {}),
            memoir._NO_BRANCHES,
        )

    def test_collapsed_branches_are_labelled_and_offered_once(self):
        self._branch("ab12", 1, "## Ruled out\n- spectral method — diverged\n")
        self._branch("ab12", 2, "## Ruled out\n- greedy packing — too slow\n")
        value = memoir.branch_memoirs_for_injection(self.ws, self.inst, {})
        self.assertIn("2 fan-out branch memoir(s)", value)
        self.assertIn("== BRANCH fork-ab12/clone-1 ==", value)
        self.assertIn("== BRANCH fork-ab12/clone-2 ==", value)
        self.assertIn("spectral method", value)
        self.assertIn("greedy packing", value)

        # Self-clearing: once the root auditor edits the memoir, the root file
        # is newer than the shadows and the input goes empty again. No state.
        self._touch_root_newer()
        self.assertEqual(
            memoir.branch_memoirs_for_injection(self.ws, self.inst, {}),
            memoir._NO_BRANCHES,
        )

    def test_fold_strips_boilerplate_and_skips_unedited_branches(self):
        """Measured on a real fan-out: the template comment is ~420 tokens, so
        three branches would burn a third of the fold budget on boilerplate,
        and a branch that learned nothing should cost nothing."""
        from long_exposure.workspace_bootstrap import render_template
        skeleton = render_template("memoir_template.md", created="2026-09-24T00:00:00")
        self._branch("ab12", 1, skeleton)                      # never edited
        self._branch("ab12", 2, skeleton.replace(
            "(none yet)", "branch 2 ruled out: tiling failed", 1))
        value = memoir.branch_memoirs_for_injection(self.ws, self.inst, {})
        self.assertIn("1 fan-out branch memoir(s)", value)     # not 2
        self.assertIn("clone-2", value)
        self.assertNotIn("clone-1", value)                     # skeleton skipped
        self.assertNotIn("<!--", value)                        # comment stripped
        self.assertNotIn("Advisory narrative back-reference", value)
        self.assertIn("tiling failed", value)

    def test_stale_and_empty_shadows_are_skipped(self):
        stale = self.inst / "fork-old" / "clone-1"
        stale.mkdir(parents=True)
        (stale / "MEMOIR.md").write_text("## Ruled out\n- ancient\n")
        os.utime(stale / "MEMOIR.md", (0, 0))          # older than the root
        self._branch("ab12", 1, "   \n  \n")            # whitespace only
        self.assertEqual(
            memoir.branch_memoirs_for_injection(self.ws, self.inst, {}),
            memoir._NO_BRANCHES,
        )

    def test_branch_block_is_capped_at_a_branch_boundary(self):
        for k in range(3):
            self._branch("ab12", k, f"## Ruled out\n- branch {k} " + "x" * 3000)
        seen = []
        with patch("long_exposure.memoir.health_events.append_event",
                   side_effect=lambda kind, **kw: seen.append(kind)):
            value = memoir.branch_memoirs_for_injection(
                self.ws, self.inst, {"memoir": {"max_tokens": 300}}
            )
        self.assertIn("memoir_branches_over_cap", seen)
        self.assertIn("[memoir over cap", value)
        self.assertLessEqual(len(value) // 4, 400)

    def test_root_frozen_invariant_holds_and_reports(self):
        before = memoir.snapshot(self.ws)
        # Untouched during the fork → invariant holds, no event.
        seen = []
        with patch("long_exposure.memoir.health_events.append_event",
                   side_effect=lambda kind, **kw: seen.append(kind)):
            self.assertTrue(
                memoir.assert_root_frozen_during_fork(self.ws, before, "ab12")
            )
        self.assertEqual(seen, [])

        # A clone wrote the root file anyway → violation is reported.
        root = paths.memoir_path(self.ws)
        root.write_text(before + "\n- a clone wrote here\n")
        seen = []
        with patch("long_exposure.memoir.health_events.append_event",
                   side_effect=lambda kind, **kw: seen.append(kind)):
            self.assertFalse(
                memoir.assert_root_frozen_during_fork(self.ws, before, "ab12")
            )
        self.assertEqual(seen, ["memoir_clone_write"])

    def test_archive_never_targets_a_shadow(self):
        """Archiving is root-only by construction: even called from inside a
        clone env, archive_if_changed reads the ROOT memoir, so a shadow can
        never land in the shared history or the DB under a clone's cycle."""
        root = paths.memoir_path(self.ws)
        before = memoir.snapshot(self.ws)
        root.write_text(before + "\n- root edit\n")
        with tempfile.TemporaryDirectory() as inst:
            shadow = Path(inst) / "MEMOIR.md"
            shadow.write_text("## Thesis\nbranch-only content\n")
            with patch.dict(os.environ, {"AGENT_FORK_ID": "ab12",
                                         "AGENT_INSTANCE_DIR": inst}):
                archived = memoir.archive_if_changed(self.ws, 5, before, None)
        self.assertIsNotNone(archived)
        self.assertNotIn("branch-only content", archived.read_text())
        self.assertIn("root edit", archived.read_text())


class MemoirWorkspaceHygieneTests(unittest.TestCase):
    """The memoir must not trip the validators agents are told to run, and
    must never ship in a curated package."""

    def test_org_check_is_silent_about_the_memoir(self):
        from long_exposure.tools import org_check
        from long_exposure.workspace_bootstrap import bootstrap_workspace
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            bootstrap_workspace(ws, "test directive", "run-x", 1)
            memoir.seed_if_missing(ws)
            before = memoir.snapshot(ws)
            paths.memoir_path(ws).write_text(before + "\n- x\n")
            memoir.archive_if_changed(ws, 1, before, None)
            findings = org_check.run(ws)
            texts = findings.errors + findings.warnings
            self.assertFalse(
                any("MEMOIR" in t or "memoir" in t for t in texts), texts
            )

    def test_curator_never_packages_the_memoir(self):
        from long_exposure.curator import _is_package_hard_excluded
        for rel in ("MEMOIR.md", "memoir/history/cycle-000001_x.md",
                    "memoir/history", "memoir"):
            self.assertTrue(_is_package_hard_excluded(rel), rel)
        # Neighbours are untouched.
        for rel in ("reports/final/final_report.md", "docs/memoirs-of-a-geisha.md",
                    "plan_of_record.md"):
            self.assertFalse(_is_package_hard_excluded(rel), rel)


class MemoirPromptRenderingTests(unittest.TestCase):
    """The input protocol renders the memoir for exactly the roles that
    declare it — the score, not the harness, decides who sees it."""

    def _prompt(self, agent_def, results, score_inputs):
        return build_agent_prompt(
            score_task="t", step_agent_name="x", agent_def=agent_def,
            results=results, score_inputs=score_inputs,
        )

    def test_researcher_sees_contents_auditor_sees_path_only(self):
        results = {"directive": "d", "run_memory": "[Run memoir — advisory]\n\n## Thesis\nX"}
        score_inputs = {"memoir_path": "/ws/MEMOIR.md"}
        researcher = self._prompt(
            {"inputs": ["directive", "run_memory"], "outputs": ["research_brief"]},
            results, score_inputs,
        )
        self.assertIn("[INPUT: run_memory]", researcher)
        self.assertIn("## Thesis", researcher)
        self.assertNotIn("memoir_path", researcher)

        auditor = self._prompt(
            {"inputs": ["directive", "memoir_path"], "outputs": ["audit_report"]},
            results, score_inputs,
        )
        self.assertIn("[INPUT: memoir_path]", auditor)
        self.assertIn("/ws/MEMOIR.md", auditor)
        self.assertNotIn("## Thesis", auditor)
        self.assertNotIn("[INPUT: run_memory]", auditor)


def _memoir_score_agents():
    return (
        "agents:\n"
        "  researcher:\n"
        "    inputs: [directive, audit_report, live_guidance, run_memory]\n"
        "    outputs: [research_brief]\n"
        "    role: researcher\n"
        "  worker:\n"
        "    inputs: [directive, research_brief, run_memory]\n"
        "    outputs: [work_output]\n"
        "    role: worker\n"
        "  auditor:\n"
        "    inputs: [directive, work_output, memoir_path, branch_memoirs]\n"
        "    outputs: [audit_report]\n"
        "    role: auditor\n"
    )


def _write_memoir_files(root: Path, *, config_extra: str = "", max_cycles: int = 2):
    """Like test_run_switches._write_files but with the memoir inputs declared."""
    workspace = root / "workspace"
    instance = root / "instance"
    workspace.mkdir()
    instance.mkdir()
    score = root / "score.yaml"
    score.write_text(
        "task: test directive\n"
        "loop:\n"
        f"  max_cycles: {max_cycles}\n"
        "  cycle_cooldown_seconds: 0\n"
        "  report_interval: 100\n"
        "  daily_sync_interval_hours: 0\n"
        "  fanout_enabled: false\n"
        "  end_of_run: false\n"
        + _memoir_score_agents()
        + "flow: [researcher, worker, auditor]\n"
    )
    config = root / "config.yaml"
    config.write_text(
        "llm_provider: local\n"
        "model: test\n"
        "local_model: test\n"
        "local_context_window: 32768\n"
        "context_window: 32768\n"
        "compact_threshold: 0.9\n"
        f"compact_db: {instance / 'sessions.db'}\n"
        f"working_directory: {workspace}\n"
        "checkpoint_format: standard\n"
        "require_checkpoint_first: false\n"
        "user_gate_approval: false\n"
        "anti_patterns_enabled: true\n"
        "telemetry:\n"
        "  enabled: false\n"
        + config_extra
    )
    return score, config, instance, workspace


def _agent_that_edits_memoir(seen: list, workspace: Path, *, edit: bool = True):
    """Fake agent that records what each role received and, as the auditor,
    makes a minimal edit to MEMOIR.md the way the real one would."""
    def fake(agent_name, agent_def, **kwargs):
        results = kwargs.get("results") or {}
        score_inputs = kwargs.get("score_inputs") or {}
        seen.append({
            "agent": agent_name,
            "run_memory": results.get("run_memory"),
            "memoir_path": score_inputs.get("memoir_path"),
            "branch_memoirs": results.get("branch_memoirs"),
            "declared": list(agent_def.get("inputs", [])),
        })
        if agent_name == "auditor" and edit:
            target = paths.memoir_path(workspace)
            body = target.read_text()
            cycle_tag = f"ruled out approach #{len([s for s in seen if s['agent']=='auditor'])}"
            target.write_text(body + f"\n- {cycle_tag}\n")
        return {
            "agent": agent_name,
            "outputs": {agent_def["outputs"][0]: f"{agent_name} output " + "x" * 2100},
            "usage": {"input_tokens": 100, "output_tokens": 2100},
            "duration_ms": 10,
            "status": "ok",
            "error": None,
            "cost_usd": None,
            "num_turns": 1,
            "tool_calls": 1,
        }
    return fake


class MemoirCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        _provider.configure_provider({"llm_provider": "claude"})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)
        _provider.configure_provider({"llm_provider": "claude"})

    def _run(self, score, config, inst):
        run_exploration(
            score_path=str(score), config_path=str(config),
            output_dir=inst / "output", state_path=inst / "exploration_state.json",
            task_override=None, instance_dir=inst,
        )

    def test_roles_receive_the_right_thing_and_archive_tracks_edits(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst, ws = _write_memoir_files(Path(td), max_cycles=2)
            with patch("long_exposure.exploration._call_exploration_agent",
                       _agent_that_edits_memoir(seen, ws)):
                self._run(score, config, inst)

            by_role = {}
            for s in seen:
                by_role.setdefault(s["agent"], []).append(s)
            self.assertEqual(len(by_role["auditor"]), 2)

            # Researcher and worker: contents in-window, with the header.
            for role in ("researcher", "worker"):
                for call in by_role[role]:
                    self.assertIsNotNone(call["run_memory"], role)
                    self.assertTrue(call["run_memory"].startswith("[Run memoir"), role)
                    self.assertIn("## Thesis", call["run_memory"])
            # Cycle 2's researcher sees cycle 1's auditor edit.
            self.assertIn("ruled out approach #1", by_role["researcher"][1]["run_memory"])
            self.assertNotIn("ruled out approach #1", by_role["researcher"][0]["run_memory"])

            # Auditor: the bare path, and NOT the contents.
            for call in by_role["auditor"]:
                self.assertEqual(call["memoir_path"], str(paths.memoir_path(ws)))
                self.assertNotIn("run_memory", call["declared"])

            # One archive per changed cycle, content == live file at that time.
            history = sorted(paths.memoir_history_dir(ws).iterdir())
            self.assertEqual([h.name[:13] for h in history], ["cycle-000001_", "cycle-000002_"])
            self.assertEqual(history[-1].read_text(), paths.memoir_path(ws).read_text())
            self.assertIn("ruled out approach #1", history[0].read_text())
            self.assertNotIn("ruled out approach #2", history[0].read_text())

            # And one sessions.db row per archive.
            conn = sqlite3.connect(inst / "sessions.db")
            rows = conn.execute(
                "SELECT subtopic FROM sessions WHERE record_type='memoir' ORDER BY subtopic"
            ).fetchall()
            conn.close()
            self.assertEqual([r[0] for r in rows], ["cycle-000001", "cycle-000002"])

    def test_unchanged_memoir_leaves_no_archive(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst, ws = _write_memoir_files(Path(td), max_cycles=2)
            with patch("long_exposure.exploration._call_exploration_agent",
                       _agent_that_edits_memoir(seen, ws, edit=False)):
                self._run(score, config, inst)
            self.assertEqual(list(paths.memoir_history_dir(ws).iterdir()), [])
            conn = sqlite3.connect(inst / "sessions.db")
            n = conn.execute("SELECT COUNT(*) FROM sessions WHERE record_type='memoir'").fetchone()[0]
            conn.close()
            self.assertEqual(n, 0)

    def test_clone_reads_but_never_writes(self):
        """Drives the REAL clone detection — the AGENT_FORK_ID /
        AGENT_INSTANCE_DIR pair the fan-out conductor sets, the same pair
        workspace_bootstrap.resolve_ledger_path uses — rather than patching
        _is_clone, so the shadow actually resolves the way it does in a
        spawned clone."""
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst, ws = _write_memoir_files(Path(td), max_cycles=1)
            with patch("long_exposure.exploration._call_exploration_agent",
                       _agent_that_edits_memoir(seen, ws)), \
                    patch.dict(os.environ, {"AGENT_FORK_ID": "ab12",
                                            "AGENT_INSTANCE_DIR": str(inst)}):
                self._run(score, config, inst)
            # The branch wrote its own shadow beside its state file.
            self.assertTrue((inst / "MEMOIR.md").exists())
            researcher = next(s for s in seen if s["agent"] == "researcher")
            self.assertIn("## Thesis", researcher["run_memory"])
            auditor = next(s for s in seen if s["agent"] == "auditor")
            # The clone auditor is pointed at its own shadow, never the root.
            self.assertIn("BRANCH memoir", auditor["memoir_path"])
            self.assertNotIn(
                str(paths.memoir_path(ws)) + "\n", auditor["memoir_path"]
            )
            # The fake auditor still wrote the root file, but the harness must
            # not archive it from a clone.
            self.assertEqual(list(paths.memoir_history_dir(ws).iterdir()), [])

    def test_disabled_strips_inputs_and_writes_nothing(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst, ws = _write_memoir_files(
                Path(td), max_cycles=1, config_extra="memoir:\n  enabled: false\n",
            )
            with patch("long_exposure.exploration._call_exploration_agent",
                       _agent_that_edits_memoir(seen, ws, edit=False)):
                self._run(score, config, inst)
            for call in seen:
                self.assertIsNone(call["run_memory"], call["agent"])
                self.assertIsNone(call["memoir_path"], call["agent"])
                self.assertIsNone(call["branch_memoirs"], call["agent"])
                for name in ("run_memory", "memoir_path", "branch_memoirs"):
                    self.assertNotIn(name, call["declared"])
            self.assertFalse(paths.memoir_path(ws).exists())

    def test_resumed_pre_memoir_workspace_is_seeded_lazily(self):
        """A workspace created before the feature has no MEMOIR.md; the first
        cycle after upgrade must still inject a skeleton rather than an error."""
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst, ws = _write_memoir_files(Path(td), max_cycles=1)
            (ws / "plan_of_record.md").write_text("# existing plan\n")  # looks like a resume
            with patch("long_exposure.exploration._call_exploration_agent",
                       _agent_that_edits_memoir(seen, ws, edit=False)):
                self._run(score, config, inst)
            researcher = next(s for s in seen if s["agent"] == "researcher")
            self.assertIn("## Thesis", researcher["run_memory"])
            self.assertTrue(paths.memoir_path(ws).exists())


if __name__ == "__main__":
    unittest.main()
