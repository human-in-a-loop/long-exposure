"""The docs' config blocks must match what the code actually reads.

Documentation drifts silently: a renamed key or a changed default leaves the
docs confidently wrong, and for the benchmark plan that means a
pre-registration describing a configuration nobody ran. These tests parse the
YAML out of the docs and check it against the modules that consume it.
"""

import re
import unittest
from pathlib import Path

import yaml

from long_exposure import (
    cycle_plan as cp,
    model_profiles as mp,
    spend_limit as sl,
    startup_gate as g,
)
from long_exposure.orchestrator import load_config


def _yaml_blocks(path: str) -> list[dict]:
    text = Path(path).read_text()
    out = []
    for raw in re.findall(r"```yaml\n(.*?)```", text, re.DOTALL):
        clean = re.sub(r"<[A-Z_]+>", "PLACEHOLDER", raw)
        try:
            parsed = yaml.safe_load(clean)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


class ShippedConfigMatchesDefaultsTests(unittest.TestCase):
    """The shipped config.yaml must not document keys the code ignores."""

    def test_no_unknown_keys_in_the_new_feature_blocks(self):
        cfg = load_config()
        for name, defaults in (
            ("model_profiles", mp.DEFAULTS),
            ("usage_allowance", sl.DEFAULTS),
            ("startup_gate", g.DEFAULTS),
        ):
            block = cfg.get(name)
            self.assertIsInstance(block, dict, name)
            unknown = set(block) - set(defaults)
            self.assertEqual(unknown, set(), f"{name} documents unread keys")

    def test_score_cycle_planning_has_no_unknown_keys(self):
        score = yaml.safe_load(
            Path("long_exposure/exploration-score.yaml").read_text()
        )
        block = score["loop"]["cycle_planning"]
        self.assertEqual(set(block) - set(cp.DEFAULTS), set())

    def test_shipped_values_match_the_module_defaults(self):
        """A default changed in one place and not the other is a silent trap."""
        cfg = load_config()
        score = yaml.safe_load(
            Path("long_exposure/exploration-score.yaml").read_text()
        )
        self.assertEqual(
            cfg["model_profiles"]["default"], mp.DEFAULTS["default"],
        )
        for key in ("max_worker_chain", "max_turns_per_cycle",
                    "audit_floor_cycles", "allow_in_clones",
                    "worker_may_request_audit"):
            self.assertEqual(
                score["loop"]["cycle_planning"][key], cp.DEFAULTS[key], key,
            )


class BenchmarkPlanConsistencyTests(unittest.TestCase):
    """docs/rcb-benchmark-plan.md is a pre-registration: it must be exact."""

    @classmethod
    def setUpClass(cls):
        cls.merged = {}
        for block in _yaml_blocks("docs/rcb-benchmark-plan.md"):
            cls.merged.update(block)
        cls.text = Path("docs/rcb-benchmark-plan.md").read_text()

    def test_the_plan_has_parseable_config_blocks(self):
        self.assertTrue(self.merged, "no YAML blocks parsed from the plan")
        self.assertIn("model", self.merged)
        self.assertIn("loop", self.merged)

    def test_the_documented_profile_is_inert_on_the_benchmark_model(self):
        """The plan's central claim about profiles, checked not asserted.

        If someone adds the benchmark model to the advanced family, the plan's
        §5.5 becomes false and the run would measure thinned guidance while
        the write-up says it did not.
        """
        cfg = {
            "model": self.merged.get("model"),
            "model_profiles": self.merged.get("model_profiles"),
        }
        self.assertTrue(mp.enabled(cfg), "the plan enables profiles")
        self.assertEqual(
            mp.resolve(cfg), mp.STANDARD,
            "the plan says the profile is inert on this model",
        )
        self.assertEqual(
            mp.knobs(cfg), {},
            "the plan says no guidance knob is set for this run",
        )

    def test_cycle_planning_is_on_at_the_root_and_off_in_clones(self):
        loop = self.merged.get("loop") or {}
        self.assertTrue(cp.enabled(loop), "the plan enables cycle planning")
        self.assertFalse(
            cp.enabled(loop, is_clone=True),
            "the plan says planning is root-only so branches stay comparable",
        )

    def test_the_spend_limit_and_gate_are_off(self):
        self.assertIsNone(
            sl.cap_usd(self.merged), "the plan keeps main's unlimited budget",
        )
        self.assertFalse(
            g.enabled(self.merged), "the adapter is non-interactive",
        )

    def test_the_plan_documents_no_key_the_code_ignores(self):
        loop = self.merged.get("loop") or {}
        for name, block, defaults in (
            ("model_profiles", self.merged.get("model_profiles"), mp.DEFAULTS),
            ("usage_allowance", self.merged.get("usage_allowance"), sl.DEFAULTS),
            ("startup_gate", self.merged.get("startup_gate"), g.DEFAULTS),
            ("cycle_planning", loop.get("cycle_planning"), cp.DEFAULTS),
        ):
            if not isinstance(block, dict):
                continue
            self.assertEqual(
                set(block) - set(defaults), set(),
                f"the plan documents a {name} key the code does not read",
            )

    def test_the_plan_names_the_health_events_it_says_it_will_collect(self):
        registry = Path("long_exposure/health_events.py").read_text()
        for kind in ("cycle_plan_rejected", "cycle_plan_audit_forced",
                     "cycle_plan_audit_requested"):
            self.assertIn(kind, self.text, f"plan should mention {kind}")
            self.assertIn(kind, registry, f"{kind} must be registered")

    def test_the_plan_does_not_claim_thinned_guidance(self):
        """The one sentence that would be actively misleading."""
        lowered = self.text.lower()
        self.assertIn("inert on this model", lowered)
        self.assertIn("not* measure the thinned guidance", lowered)


if __name__ == "__main__":
    unittest.main()
