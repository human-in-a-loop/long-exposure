import os
import unittest
from unittest.mock import patch

from long_exposure.fanout import _parse_fanout_block


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


if __name__ == "__main__":
    unittest.main()
