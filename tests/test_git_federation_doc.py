"""The git-federation walkthrough must stay true to the code it cites.

`docs/git-federation.md` is a design for something not yet built, which makes
it *more* prone to rotting than a doc describing live behaviour: nothing
breaks when it goes stale. These tests pin the claims the design actually
rests on — the storage zones, the file shapes, and the two identity gaps —
so a rename or a refactor surfaces here instead of in a doc nobody re-reads.

They do not test any git code, because there is none. The first test asserts
exactly that, so the doc's opening claim stays honest.
"""

import json
import re
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "git-federation.md"


class TheDocsOpeningClaimTests(unittest.TestCase):
    def test_the_harness_still_runs_no_git_commands(self):
        """The doc opens by saying none of this exists. Keep that true.

        When git code does land, this test fails and the doc's status line
        has to be rewritten — which is the point.
        """
        offenders = []
        for path in (REPO / "long_exposure").rglob("*.py"):
            text = path.read_text()
            for m in re.finditer(r"""["'](git)["']|["']git\s+(\w+)""", text):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO)}:{line}")
        self.assertEqual(offenders, [], "git invocation found; update the doc's status")

    def test_the_doc_is_marked_as_not_built(self):
        head = DOC.read_text()[:400]
        self.assertIn("designed, not built", head.lower())


class StorageZoneTests(unittest.TestCase):
    """§2: only the workspace is shared. The other two zones are local."""

    def test_the_shared_files_are_under_the_workspace(self):
        from long_exposure import paths

        ws = Path("/tmp/ws-fed")
        self.assertEqual(paths.memoir_path(ws), ws / "MEMOIR.md")
        self.assertEqual(paths.memoir_history_dir(ws), ws / "memoir" / "history")
        self.assertEqual(paths.workspace_root({"working_directory": str(ws)}), ws)

    def test_the_ledger_lives_in_the_workspace_for_a_root_process(self):
        from long_exposure import workspace_bootstrap as wb

        ws = Path("/tmp/ws-fed")
        self.assertEqual(wb.resolve_ledger_path(ws), ws / "promise_ledger.jsonl")

    def test_sessions_db_is_outside_any_repo(self):
        """§2 zone 3: it needs no .gitignore entry because it cannot be
        inside working_directory."""
        text = (REPO / "long_exposure" / "mcp_search_server.py").read_text()
        self.assertIn('Path.home() / ".local" / "share" / "auto-compact"', text)

    def test_instance_dirs_are_gitignored(self):
        ignored = (REPO / ".gitignore").read_text().splitlines()
        self.assertIn("instances/", ignored)
        self.assertIn(".long-exposure/", ignored)


class UnionMergeSafetyTests(unittest.TestCase):
    """§5.1: union merge is correct here, and these are the three reasons."""

    def test_every_ledger_line_is_newline_terminated(self):
        """A union merge can only join two objects if one lacks a newline."""
        text = (REPO / "long_exposure" / "workspace_bootstrap.py").read_text()
        body = text.split("def append_ledger_event", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('+ "\\n"', body)

    def test_event_ids_are_uuids_so_they_never_collide_across_machines(self):
        from long_exposure import workspace_bootstrap as wb
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            eid = wb.emit_run_start_event(ws, "run-x", "a directive")
            uuid.UUID(eid)   # raises if not a UUID
            row = json.loads((ws / "promise_ledger.jsonl").read_text().strip())
        self.assertEqual(row["event_id"], eid)

    def test_the_reader_sorts_so_byte_order_does_not_matter(self):
        """Union merge yields insertion order, not chronological order."""
        from long_exposure import workspace_bootstrap as wb
        import tempfile

        def ev(eid, ts, mid):
            return {"event_id": eid, "ts": ts, "run_id": "r", "cycle": 1,
                    "agent": "worker", "milestone_id": mid,
                    "status": "validated",
                    "confidence": {"level": "high"}, "narrative": eid}

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            # Deliberately out of order, the way a union merge leaves them:
            # all of A's lines, then all of B's.
            for e in (ev("a1", "2026-01-02T00:00:00Z", "m/a1"),
                      ev("a2", "2026-01-04T00:00:00Z", "m/a2"),
                      ev("b1", "2026-01-03T00:00:00Z", "m/b1")):
                wb.append_ledger_event(ws, e)
            summary = wb.summarize_ledger(ws)
        order = [line for line in summary.splitlines() if line.startswith("- [")]
        stamps = [re.search(r"(2026-\d\d-\d\d)", line).group(1) for line in order]
        self.assertEqual(stamps, sorted(stamps), "summarize_ledger must sort by ts")


class IdentityGapTests(unittest.TestCase):
    """§7.1: CLOSED. `operator` is stamped and readers key on it.

    The behavioural tests live in `tests/test_federation.py`; these two pin
    the parts of §7.1 the walkthrough still describes as-is.
    """

    def test_run_id_still_carries_no_operator_and_no_randomness(self):
        """Deliberately unchanged. §7.1 lists three symptoms of one cause,
        and the fix was the `operator` field, not a new run_id format —
        changing run_id would invalidate the registry, the state file and
        every telemetry row for no additional benefit."""
        from long_exposure import workspace_bootstrap as wb

        rid = wb.derive_run_id()
        self.assertRegex(rid, r"^run-\d{4}-\d\d-\d\dT\d{6}Z$")
        self.assertEqual(rid, wb.derive_run_id(),
                         "two runs in the same second share a run_id")

    def test_ledger_events_now_carry_an_operator(self):
        from long_exposure import workspace_bootstrap as wb
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            wb.emit_run_start_event(ws, "run-x", "a directive")
            row = json.loads((ws / "promise_ledger.jsonl").read_text().strip())
        self.assertTrue(row.get("operator"))

    def test_milestone_id_was_not_prefixed(self):
        """§7.1 proposed `<operator>/<milestone>`; implementing it showed why
        a separate field is right. RESERVED_NAMESPACES matches milestone
        PREFIXES, which an operator segment would defeat."""
        from long_exposure.tools.promise_check import RESERVED_NAMESPACES
        from long_exposure import workspace_bootstrap as wb
        import tempfile

        self.assertIn("_run/", RESERVED_NAMESPACES)
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            wb.emit_run_start_event(ws, "run-x", "a directive")
            row = json.loads((ws / "promise_ledger.jsonl").read_text().strip())
        self.assertEqual(row["milestone_id"], "_run/start")
        self.assertTrue(row["milestone_id"].startswith(RESERVED_NAMESPACES))

    def test_a_settled_milestone_from_one_operator_hides_the_others(self):
        """The §7.1 collision, in the shape that actually loses information.

        `summarize_ledger` keeps the latest event per milestone_id. Two
        operators reaching the same milestone with confident, settled results
        means the earlier one — including a CONTRADICTING result — is absent
        from the summary rather than flagged.
        """
        from long_exposure import workspace_bootstrap as wb
        import tempfile

        def ev(eid, ts, narrative, run):
            return {"event_id": eid, "ts": ts, "run_id": run, "cycle": 3,
                    "agent": "auditor", "milestone_id": "spectral/bound",
                    "status": "validated",
                    "confidence": {"level": "high", "assessor": "auditor"},
                    "narrative": narrative}

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            wb.append_ledger_event(ws, ev("a", "2026-01-02T00:00:00Z",
                                          "ALICE bound is 3.2", "run-alice"))
            wb.append_ledger_event(ws, ev("b", "2026-01-03T00:00:00Z",
                                          "BOB bound is 7.9", "run-bob"))
            summary = wb.summarize_ledger(ws)
        self.assertIn("distinct milestones: 1", summary)
        self.assertIn("BOB", summary)
        self.assertNotIn("ALICE", summary, "the earlier operator is dropped")

    def test_in_progress_events_survive_the_collision(self):
        """The backfill §7.1 credits: a duplicated _run/start is NOT lost.

        Worth pinning separately, because the doc would be overstating the
        gap if it claimed every duplicated milestone loses an event.
        """
        from long_exposure import workspace_bootstrap as wb
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            wb.emit_run_start_event(ws, "run-alice", "alice's directive")
            wb.emit_run_start_event(ws, "run-bob", "bob's directive")
            summary = wb.summarize_ledger(ws)
        shown = [l for l in summary.splitlines() if l.startswith("- [_run/start]")]
        self.assertEqual(len(shown), 2, "in-progress backfill keeps both")


class GuidanceSeamTests(unittest.TestCase):
    """§4.1: the federation block would join an existing list, not a new one."""

    def test_the_live_guidance_parts_list_is_where_the_doc_says(self):
        text = (REPO / "long_exposure" / "exploration.py").read_text()
        self.assertIn(
            "p for p in (fanout_guide, sibling_block, anti_patterns_block, guidance)",
            text,
        )

    def test_the_cycle_boundary_transaction_point_still_exists(self):
        text = (REPO / "long_exposure" / "exploration.py").read_text()
        self.assertIn("# Status file + state", text)


if __name__ == "__main__":
    unittest.main()
