"""The git-federation walkthrough must stay true to the code it cites.

`docs/git-federation.md` describes a sync layer that is still unbuilt, which
makes it *more* prone to rotting than a doc describing live behaviour: nothing
breaks when it goes stale. These tests pin the claims the design rests on —
the storage zones, the file shapes, the identity fix — so a rename or a
refactor surfaces here instead of in a doc nobody re-reads.

The load-bearing one is `GitIsReadOnlyTests`. The conflict radar made the
harness run git for the first time, and the property that makes that safe is
that it only ever READS: no commit, no push, no rebase, no checkout, no reset.
That invariant is worth a test with teeth, because the radar runs on the cycle
path against the operator's live workspace, where a write would destroy work.
"""

import json
import re
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "git-federation.md"


# git subcommands the harness may invoke. Everything here either reads, or —
# in `fetch`'s single case — writes only remote-tracking refs and no working
# file. A subcommand that is not on this list is a write to the operator's
# workspace, and adding one is a decision, not a refactor.
READ_ONLY_GIT = {
    "rev-parse", "merge-base", "merge-tree", "diff", "status", "log",
    "show", "cat-file", "ls-files", "ls-remote", "ls-tree", "for-each-ref",
    "describe", "config",
    # The documented exception: updates refs/remotes/*, touches no file.
    "fetch",
}

FORBIDDEN_GIT = {
    "commit", "push", "rebase", "merge", "checkout", "switch", "reset",
    "clean", "rm", "mv", "restore", "stash", "apply", "am", "cherry-pick",
    "revert", "tag", "branch", "worktree", "gc", "prune", "filter-branch",
    "update-ref", "symbolic-ref", "init", "clone", "add",
}


class GitIsReadOnlyTests(unittest.TestCase):
    """The harness reads git. It must never write to the operator's tree."""

    def test_git_is_invoked_from_exactly_one_place(self):
        """Every git call funnels through `conflict_radar._git`.

        This is what makes the rest of this class checkable: one function with
        one timeout and one never-raises contract, instead of git calls
        scattered across the package.
        """
        files = set()
        for path in (REPO / "long_exposure").rglob("*.py"):
            if re.search(r"""\[\s*["']git["']\s*[,\]]""", path.read_text()):
                files.add(str(path.relative_to(REPO)))
        self.assertEqual(files, {"long_exposure/conflict_radar.py"}, files)

    def test_only_read_only_subcommands_are_invoked(self):
        """The real call shape is `_git(["<sub>", ...])`, so check that.

        An earlier version of this test only matched `["git", "<sub>"`, which
        every call in this package sidesteps — `_git` supplies the "git" itself.
        It would have passed a `_git(["commit", ...])` without complaint.
        """
        text = (REPO / "long_exposure" / "conflict_radar.py").read_text()
        subs = set()
        for m in re.finditer(r"""_git\(\s*\[\s*["']([\w-]+)["']""", text):
            subs.add(m.group(1))
        self.assertTrue(subs, "found no _git call sites; did the shape change?")
        self.assertEqual(subs - READ_ONLY_GIT, set(),
                         f"non-read-only subcommand invoked: {subs - READ_ONLY_GIT}")
        self.assertEqual(subs & FORBIDDEN_GIT, set())

    def test_the_guard_would_actually_catch_a_write(self):
        """A test that cannot fail is not a test. Prove the pattern bites."""
        sample = '_git(["commit", "-m", "x"], workspace)'
        found = set(re.findall(r"""_git\(\s*\[\s*["']([\w-]+)["']""", sample))
        self.assertEqual(found, {"commit"})
        self.assertTrue(found & FORBIDDEN_GIT)

    def test_no_forbidden_subcommand_appears_anywhere_in_the_package(self):
        """Belt and braces: catch a write built by string concatenation too."""
        offenders = []
        for path in (REPO / "long_exposure").rglob("*.py"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                for m in re.finditer(r"""["']git\s+([a-z-]+)""", line):
                    if m.group(1) in FORBIDDEN_GIT:
                        offenders.append(f"{path.relative_to(REPO)}:{i} {m.group(1)}")
        self.assertEqual(offenders, [], f"git write found: {offenders}")

    def test_the_radar_declares_itself_read_only(self):
        from long_exposure import conflict_radar

        self.assertIn("read-only", (conflict_radar.__doc__ or "").lower())

    def test_the_sync_layer_is_still_unbuilt(self):
        """The radar reads. Nothing fetches-rebases-commits-pushes a cycle,
        and the doc still says so."""
        text = DOC.read_text().lower()
        self.assertIn("not built", text)
        self.assertIn("read-only", text)


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
        """The radar's block joins this list rather than adding a stage."""
        text = (REPO / "long_exposure" / "exploration.py").read_text()
        self.assertIn("p for p in (fanout_guide, sibling_block, anti_patterns_block,", text)
        self.assertIn("conflict_block, guidance)", text)

    def test_the_cycle_boundary_transaction_point_still_exists(self):
        text = (REPO / "long_exposure" / "exploration.py").read_text()
        self.assertIn("# Status file + state", text)


if __name__ == "__main__":
    unittest.main()
