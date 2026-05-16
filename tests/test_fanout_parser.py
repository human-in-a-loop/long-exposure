import os
import unittest
from unittest.mock import patch

from long_exposure.fanout import (
    _extract_branchial_diff,
    _render_divergence_table,
    _parse_fanout_block,
)


def block(*artifacts):
    branches = []
    for i, artifact in enumerate(artifacts):
        branches.append(
            "<branch>"
            f"<objective>objective {i}</objective>"
            f"<output_artifact>{artifact}</output_artifact>"
            "</branch>"
        )
    return "<parallel_cycle_fanout>" + "".join(branches) + "</parallel_cycle_fanout>"


class FanoutParserTests(unittest.TestCase):
    def test_valid_block_returns_branches(self):
        parsed = _parse_fanout_block(block("a.md", "b.md"))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["output_artifact"], "a.md")

    def test_single_branch_rejected(self):
        self.assertIsNone(_parse_fanout_block(block("a.md")))

    def test_unsafe_paths_rejected(self):
        self.assertIsNone(_parse_fanout_block(block("/tmp/a.md", "b.md")))
        self.assertIsNone(_parse_fanout_block(block("../a.md", "b.md")))

    def test_normalized_duplicate_rejected(self):
        self.assertIsNone(_parse_fanout_block(block("./a.md", "a.md")))

    def test_ancestor_descendant_rejected(self):
        self.assertIsNone(_parse_fanout_block(block("dir", "dir/file.md")))

    def test_clone_depth_cap_ignores_block(self):
        with patch.dict(os.environ, {"AGENT_FORK_ID": "abc"}, clear=False):
            self.assertIsNone(_parse_fanout_block(block("a.md", "b.md")))

    def test_extract_branchial_diff_accepts_fenced_json_and_filters_items(self):
        text = (
            "<branchial_diff>\n```json\n"
            '{"convergences": [{"claim": "agree", "branches": [0, 1]}, "bad"], '
            '"divergences": [{"subject": "x", "positions": ["bad", '
            '{"branches": [0], "claim": "a"}]}], '
            '"asymmetric_finds": "not-list", "failed_branches": []}'
            "\n```\n</branchial_diff>\nprose"
        )
        diff = _extract_branchial_diff(text)
        self.assertEqual(len(diff["convergences"]), 1)
        self.assertEqual(diff["asymmetric_finds"], [])
        rendered = _render_divergence_table(diff, [{"clone_k": 0, "state": "done"}])
        self.assertIn("## Branchial diff", rendered)
        self.assertIn("[c0] a", rendered)

    def test_extract_branchial_diff_rejects_malformed_payload(self):
        self.assertIsNone(_extract_branchial_diff("<branchial_diff>[]</branchial_diff>"))
        self.assertIsNone(_extract_branchial_diff("<branchial_diff>{</branchial_diff>"))

    def test_render_divergence_table_state_fallback(self):
        rendered = _render_divergence_table(
            None,
            [{"clone_k": 1, "state": "exited_no_report", "deliverable_status": "missing"}],
        )
        self.assertIn("Branch outcomes", rendered)
        self.assertIn("clone-1: exited_no_report", rendered)


if __name__ == "__main__":
    unittest.main()
