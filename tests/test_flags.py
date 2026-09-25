"""Shared config-boolean parsing.

The failure this exists to prevent: `enabled: "false"` in YAML reads as
truthy under a bare `bool()`, so a run the operator believed was uncapped
gets killed by the spend limit, or a flow they believed was fixed starts
being planned by the researcher.
"""

import unittest

from long_exposure import (
    cycle_plan as cp,
    flags,
    model_profiles as mp,
    spend_limit as sl,
    startup_gate as g,
)


class TruthyTests(unittest.TestCase):
    def setUp(self):
        flags._reset_warnings()

    def test_real_bools_pass_through(self):
        self.assertIs(flags.truthy(True), True)
        self.assertIs(flags.truthy(False), False)

    def test_false_spellings(self):
        for value in ("false", "False", "FALSE", "no", "NO", "off", "0",
                      " false ", "none", "null", ""):
            self.assertIs(flags.truthy(value, True), False, value)

    def test_true_spellings(self):
        for value in ("true", "True", "TRUE", "yes", "on", "1", " true "):
            self.assertIs(flags.truthy(value, False), True, value)

    def test_numbers(self):
        self.assertIs(flags.truthy(1), True)
        self.assertIs(flags.truthy(0), False)
        self.assertIs(flags.truthy(2.5), True)

    def test_none_returns_the_default(self):
        self.assertIs(flags.truthy(None, True), True)
        self.assertIs(flags.truthy(None, False), False)

    def test_unrecognised_string_warns_and_takes_the_default(self):
        self.assertIs(flags.truthy("maybe", True), True)
        self.assertIs(flags.truthy("maybe", False), False)

    def test_unrecognised_type_takes_the_default(self):
        self.assertIs(flags.truthy(["true"], False), False)
        self.assertIs(flags.truthy({"a": 1}, True), True)

    def test_false_and_true_word_sets_do_not_overlap(self):
        self.assertEqual(set(flags.FALSE_WORDS) & set(flags.TRUE_WORDS), set())


class FeatureSwitchTests(unittest.TestCase):
    """Each new feature's `enabled` honours the spellings, both ways."""

    def setUp(self):
        flags._reset_warnings()

    def test_quoted_false_disables_the_spend_limit(self):
        cfg = {"usage_allowance": {
            "enabled": "false", "weekly_allowance_usd": 100, "run_pct": 10,
        }}
        self.assertIsNone(sl.cap_usd(cfg))

    def test_quoted_true_enables_the_spend_limit(self):
        cfg = {"usage_allowance": {
            "enabled": "true", "weekly_allowance_usd": 100, "run_pct": 10,
        }}
        self.assertEqual(sl.cap_usd(cfg), 10.0)

    def test_quoted_false_disables_cycle_planning(self):
        self.assertFalse(cp.enabled({"cycle_planning": {"enabled": "false"}}))
        self.assertTrue(cp.enabled({"cycle_planning": {"enabled": "true"}}))

    def test_quoted_false_disables_profiles(self):
        self.assertFalse(mp.enabled({"model_profiles": {"enabled": "off"}}))
        self.assertTrue(mp.enabled({"model_profiles": {"enabled": "yes"}}))

    def test_quoted_false_disables_the_gate(self):
        self.assertFalse(g.enabled({"startup_gate": {"enabled": "no"}}))
        self.assertTrue(g.enabled({"startup_gate": {"enabled": "on"}}))

    def test_quoted_false_allow_in_clones(self):
        loop = {"cycle_planning": {"enabled": True, "allow_in_clones": "false"}}
        self.assertFalse(cp.enabled(loop, is_clone=True))
        loop["cycle_planning"]["allow_in_clones"] = "true"
        self.assertTrue(cp.enabled(loop, is_clone=True))

    def test_quoted_false_auto_still_honours_an_explicit_profile(self):
        cfg = {
            "model": "claude-fable-5-1",
            "model_profiles": {
                "enabled": True, "auto": "false",
                "families": {"advanced": ["fable"]},
            },
        }
        self.assertEqual(mp.resolve(cfg), mp.STANDARD)


if __name__ == "__main__":
    unittest.main()
