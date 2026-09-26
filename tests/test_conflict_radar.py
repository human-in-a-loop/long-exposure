"""The conflict radar, against real git repositories.

These build actual repos rather than mocking `subprocess`, because every
interesting property here is a property of git's behaviour, not of our
argument strings. Mocking the thing under test would have hidden the finding
that motivated the two-signal design: `git merge-tree` reports CLEAN on a real
overlap when the overlapping work is uncommitted, which is every
long-exposure run today.

The other property worth this much setup is that the radar is read-only. It
runs on the cycle path against the operator's live workspace, so a bug that
moved HEAD or reverted a file would destroy work.
"""

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from long_exposure import conflict_radar as cr

ON = {"federation": {"conflict_radar": {"enabled": True,
                                        "shared_branch": "main"}}}
NO_FETCH = {"federation": {"conflict_radar": {"enabled": True,
                                              "shared_branch": "main",
                                              "fetch": False}}}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


def _have_merge_tree() -> bool:
    out = subprocess.run(["git", "merge-tree", "--write-tree", "-h"],
                         capture_output=True, text=True)
    return "--write-tree" in (out.stdout + out.stderr)


class RadarTestCase(unittest.TestCase):
    """A bare remote plus two clones sharing one base commit."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.T = Path(self._tmp.name)
        git("init", "-q", "--bare", str(self.T / "bare"), cwd=self.T)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.T / "bare")
        self.A = self._clone("A")
        (self.A / "data").mkdir()
        (self.A / "reports").mkdir()
        (self.A / "data/spectral.py").write_text("v1\n")
        (self.A / "reports/r1.md").write_text("v1\n")
        (self.A / "untouched.py").write_text("x\n")
        git("add", "-A", cwd=self.A)
        git("commit", "-qm", "base", cwd=self.A)
        git("push", "-q", "origin", "main", cwd=self.A)
        self.B = self._clone("B")

    def tearDown(self):
        self._tmp.cleanup()

    def _clone(self, name):
        git("clone", "-q", str(self.T / "bare"), str(self.T / name), cwd=self.T)
        path = self.T / name
        git("config", "user.email", f"{name}@x", cwd=path)
        git("config", "user.name", name, cwd=path)
        git("checkout", "-q", "-b", "main", cwd=path)
        return path

    def _remote_moves(self, rel="data/spectral.py", body="v2-from-B\n"):
        (self.B / rel).write_text(body)
        git("add", "-A", cwd=self.B)
        git("commit", "-qm", f"B changes {rel}", cwd=self.B)
        git("push", "-q", "origin", "main", cwd=self.B)


class TheUncommittedCaseTests(RadarTestCase):
    """The finding that shaped the design."""

    def test_merge_tree_alone_misses_an_uncommitted_overlap(self):
        self._remote_moves()
        (self.A / "data/spectral.py").write_text("v2-from-A-uncommitted\n")
        r = cr.scan(self.A, ON)
        self.assertTrue(r.available)
        self.assertEqual(r.merge_tree_conflicts, [],
                         "committed state does not conflict; HEAD is stale")
        self.assertEqual(r.dirty_overlap, ["data/spectral.py"],
                         "signal 2 is the one that fires on a normal run")
        self.assertTrue(r.any_overlap)

    def test_merge_tree_fires_once_the_work_is_committed(self):
        if not _have_merge_tree():
            self.skipTest("git too old for merge-tree --write-tree")
        self._remote_moves()
        (self.A / "data/spectral.py").write_text("v2-from-A\n")
        git("commit", "-qam", "A commits", cwd=self.A)
        r = cr.scan(self.A, ON)
        self.assertEqual(r.merge_tree_conflicts, ["data/spectral.py"])
        self.assertEqual(r.dirty_overlap, [], "nothing dirty now")

    def test_untracked_files_count_as_local_work(self):
        self._remote_moves(rel="data/brand_new.py", body="from B\n")
        (self.A / "data/brand_new.py").write_text("from A, untracked\n")
        r = cr.scan(self.A, ON)
        self.assertIn("data/brand_new.py", r.dirty_overlap)

    def test_a_staged_change_counts_as_local_work(self):
        self._remote_moves()
        (self.A / "data/spectral.py").write_text("staged by A\n")
        git("add", "data/spectral.py", cwd=self.A)
        r = cr.scan(self.A, ON)
        self.assertIn("data/spectral.py", r.dirty_overlap)


class DisjointWorkTests(RadarTestCase):
    def test_disjoint_work_produces_no_block(self):
        """A block every cycle would teach the researcher to skip the tag."""
        self._remote_moves()
        (self.A / "reports/r1.md").write_text("A's own edit\n")
        r = cr.scan(self.A, ON)
        self.assertTrue(r.available)
        self.assertEqual(r.remote_changed, 1, "the remote did move")
        self.assertFalse(r.any_overlap, "but not where A is working")
        self.assertIsNone(cr.build_block(self.A, ON))

    def test_a_clean_workspace_produces_no_block(self):
        self._remote_moves()
        self.assertIsNone(cr.build_block(self.A, ON))

    def test_no_remote_movement_produces_no_block(self):
        (self.A / "data/spectral.py").write_text("only A moved\n")
        r = cr.scan(self.A, ON)
        self.assertEqual(r.remote_changed, 0)
        self.assertIsNone(cr.build_block(self.A, ON))


class ReadOnlyTests(RadarTestCase):
    def test_the_scan_touches_nothing(self):
        """It runs against the operator's live workspace on the cycle path."""
        self._remote_moves()
        (self.A / "data/spectral.py").write_text("A's uncommitted work\n")

        def snapshot():
            return (
                git("rev-parse", "HEAD", cwd=self.A).stdout,
                git("branch", "--show-current", cwd=self.A).stdout,
                git("status", "--porcelain", cwd=self.A).stdout,
                git("stash", "list", cwd=self.A).stdout,
                (self.A / "data/spectral.py").read_text(),
                (self.A / "reports/r1.md").read_text(),
            )

        before = snapshot()
        cr.scan(self.A, ON)
        cr.build_block(self.A, ON)
        self.assertEqual(before, snapshot())

    def test_fetch_is_the_only_side_effect_and_is_skippable(self):
        self._remote_moves()
        r = cr.scan(self.A, NO_FETCH)
        self.assertFalse(r.fetched)
        # Without a fetch the local origin/main is the pre-move commit, so the
        # forecast is simply blind to B's change rather than wrong.
        self.assertEqual(r.remote_changed, 0)
        r2 = cr.scan(self.A, ON)
        self.assertTrue(r2.fetched)
        self.assertEqual(r2.remote_changed, 1)


class DegradationTests(RadarTestCase):
    """Every failure is "no opinion", never a raise and never a halt."""

    def test_not_a_git_repository(self):
        plain = self.T / "plain"
        plain.mkdir()
        r = cr.scan(plain, ON)
        self.assertFalse(r.available)
        self.assertIn("not a git repository", r.reason)

    def test_missing_directory(self):
        r = cr.scan(self.T / "nope", ON)
        self.assertFalse(r.available)
        self.assertIn("not a directory", r.reason)

    def test_no_such_shared_branch(self):
        cfg = {"federation": {"conflict_radar":
                              {"enabled": True, "shared_branch": "nope"}}}
        r = cr.scan(self.A, cfg)
        self.assertFalse(r.available)
        self.assertIn("no such ref", r.reason)

    def test_an_unreachable_remote_still_forecasts_and_says_it_is_stale(self):
        """Offline must not mean blind: yesterday's refs beat nothing."""
        self._remote_moves()
        cr.scan(self.A, ON)          # warm origin/main while the remote works
        git("remote", "set-url", "origin", "https://127.0.0.1:1/nope.git",
            cwd=self.A)
        (self.A / "data/spectral.py").write_text("A's work\n")
        cfg = {"federation": {"conflict_radar":
                              {"enabled": True, "shared_branch": "main",
                               "timeout_seconds": 5}}}
        r = cr.scan(self.A, cfg)
        self.assertTrue(r.available)
        self.assertTrue(r.stale)
        self.assertFalse(r.fetched)
        self.assertEqual(r.dirty_overlap, ["data/spectral.py"])
        self.assertIn("may be out of date", cr.build_block(self.A, cfg) or "")

    def test_detached_head(self):
        self._remote_moves()
        git("checkout", "-q", "--detach", cwd=self.A)
        r = cr.scan(self.A, NO_FETCH)
        self.assertTrue(r.available)

    def test_unrelated_histories_have_no_merge_base(self):
        orphan = self.T / "orphan"
        orphan.mkdir()
        git("init", "-q", str(orphan), cwd=self.T)
        git("config", "user.email", "o@x", cwd=orphan)
        git("config", "user.name", "O", cwd=orphan)
        (orphan / "f").write_text("x")
        git("add", "-A", cwd=orphan)
        git("commit", "-qm", "c", cwd=orphan)
        git("remote", "add", "origin", str(self.T / "bare"), cwd=orphan)
        git("fetch", "-q", "origin", "main", cwd=orphan)
        r = cr.scan(orphan, NO_FETCH)
        self.assertFalse(r.available)
        self.assertIn("no merge base", r.reason)

    def test_a_bad_timeout_value_falls_back_instead_of_crashing(self):
        for bad in ("lots", None, 0, -5, [1]):
            cfg = {"federation": {"conflict_radar":
                                  {"enabled": True, "shared_branch": "main",
                                   "timeout_seconds": bad}}}
            self.assertIsInstance(cr.scan(self.A, cfg), cr.Radar)


class BoundsTests(RadarTestCase):
    def test_max_paths_caps_each_signal(self):
        for i in range(30):
            (self.B / f"data/f{i}.py").write_text("from B\n")
        git("add", "-A", cwd=self.B)
        git("commit", "-qm", "B adds 30", cwd=self.B)
        git("push", "-q", "origin", "main", cwd=self.B)
        for i in range(30):
            (self.A / f"data/f{i}.py").write_text("from A\n")
        cfg = {"federation": {"conflict_radar":
                              {"enabled": True, "shared_branch": "main",
                               "max_paths": 5}}}
        r = cr.scan(self.A, cfg)
        self.assertEqual(len(r.dirty_overlap), 5)
        self.assertEqual(r.remote_changed, 30, "the count is not capped")

    def test_paths_with_spaces_survive_the_porcelain_parse(self):
        weird = "data/a file with spaces.py"
        self._remote_moves(rel=weird, body="from B\n")
        (self.A / weird).write_text("from A\n")
        r = cr.scan(self.A, ON)
        self.assertIn(weird, r.dirty_overlap)


class BlockTests(RadarTestCase):
    def test_the_block_names_the_ref_and_says_nothing_is_blocked(self):
        self._remote_moves()
        (self.A / "data/spectral.py").write_text("A's work\n")
        block = cr.build_block(self.A, ON)
        self.assertIn("<shared_branch_overlap>", block)
        self.assertIn("</shared_branch_overlap>", block)
        self.assertIn("origin/main", block)
        self.assertIn("FORECAST", block)
        self.assertIn("nothing is", block)
        self.assertIn("data/spectral.py", block)

    def test_the_block_groups_paths_into_regions(self):
        for rel in ("data/a.py", "data/b.py", "reports/c.md"):
            (self.B / rel).write_text("from B\n")
        git("add", "-A", cwd=self.B)
        git("commit", "-qm", "B", cwd=self.B)
        git("push", "-q", "origin", "main", cwd=self.B)
        for rel in ("data/a.py", "data/b.py", "reports/c.md"):
            (self.A / rel).write_text("from A\n")
        r = cr.scan(self.A, ON)
        self.assertEqual(sorted(r.slices()), ["data", "report"])

    def test_describe_reports_the_configured_state(self):
        self.assertEqual(cr.describe({}), "conflict radar: off")
        self.assertIn("origin/main", cr.describe(ON))


class WiringTests(RadarTestCase):
    """The harness-side gate."""

    def test_off_by_default(self):
        from long_exposure.exploration import _build_conflict_radar_block

        self._remote_moves()
        (self.A / "data/spectral.py").write_text("A's work\n")
        self.assertIsNone(_build_conflict_radar_block(self.A, {}))

    def test_a_quoted_false_still_disables(self):
        from long_exposure.exploration import _build_conflict_radar_block

        self._remote_moves()
        (self.A / "data/spectral.py").write_text("A's work\n")
        for word in ("false", "no", "off", "0"):
            cfg = {"federation": {"conflict_radar": {"enabled": word}}}
            self.assertIsNone(_build_conflict_radar_block(self.A, cfg), word)

    def test_enabled_produces_the_block(self):
        from long_exposure.exploration import _build_conflict_radar_block

        self._remote_moves()
        (self.A / "data/spectral.py").write_text("A's work\n")
        block = _build_conflict_radar_block(self.A, ON)
        self.assertIn("data/spectral.py", block or "")

    def test_an_unexpected_failure_is_swallowed(self):
        from unittest import mock
        from long_exposure.exploration import _build_conflict_radar_block

        with mock.patch.object(cr, "build_block", side_effect=RuntimeError("x")):
            self.assertIsNone(_build_conflict_radar_block(self.A, ON))

    def test_it_joins_the_existing_live_guidance_parts_list(self):
        src = (Path(__file__).resolve().parent.parent
               / "long_exposure" / "exploration.py").read_text()
        # Whitespace-insensitive: this pins the ORDER of the guidance parts,
        # not the indentation of the function they live in (which moved when
        # the block was extracted from run_exploration).
        import re
        flat = re.sub(r"\s+", " ", src)
        self.assertIn("anti_patterns_block, conflict_block, guidance", flat)


if __name__ == "__main__":
    unittest.main()
