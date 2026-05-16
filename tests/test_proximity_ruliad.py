import unittest
from datetime import datetime, timezone

from auto_compact.proximity import compute_ancestor_set, rank_sessions


class ProximityRuliadTests(unittest.TestCase):
    def _session(self, sid, parent=None, topic="alpha", record_type="exploration"):
        return {
            "id": sid,
            "parent_id": parent,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "summary_xml": "<objective>test</objective>",
            "topic": topic,
            "subtopic": "same",
            "tools": "",
            "keywords": "",
            "record_type": record_type,
            "fork_id": None,
        }

    def test_rank_sessions_excludes_lemmas(self):
        sessions = [
            self._session("lemma", topic="alpha", record_type="lemma"),
            self._session("normal", topic="alpha"),
        ]
        ranked = rank_sessions(
            sessions,
            {"topic_weights": {"_same_topic": 1.0}, "tool_weights": {}, "keyword_weights": {}},
            {"topic": "alpha"},
            min_score=0.0,
        )
        self.assertEqual([r["id"] for r in ranked], ["normal"])

    def test_ancestor_bonus_is_opt_in_and_bonus_only(self):
        sessions = [
            self._session("old"),
            self._session("mid", parent="old"),
            self._session("current", parent="mid"),
            self._session("other"),
        ]
        self.assertEqual(compute_ancestor_set(sessions, "current"), {"mid", "old"})
        base_profile = {
            "topic_weights": {"_same_topic": 1.0, "_ancestor": 0.0},
            "tool_weights": {},
            "keyword_weights": {},
        }
        boosted_profile = {
            "topic_weights": {"_same_topic": 1.0, "_ancestor": 1.0},
            "tool_weights": {},
            "keyword_weights": {},
        }
        base = rank_sessions(
            sessions, base_profile, {"topic": "alpha"},
            exclude_id="current", ancestor_anchor_id="current", min_score=0.0,
        )
        boosted = rank_sessions(
            sessions, boosted_profile, {"topic": "alpha"},
            exclude_id="current", ancestor_anchor_id="current", min_score=0.0,
        )
        self.assertEqual({r["id"] for r in base}, {"old", "mid", "other"})
        self.assertIn(boosted[0]["id"], {"old", "mid"})
        self.assertTrue(
            next(r for r in boosted if r["id"] == "other")["score"]
            <= boosted[0]["score"]
        )


if __name__ == "__main__":
    unittest.main()
