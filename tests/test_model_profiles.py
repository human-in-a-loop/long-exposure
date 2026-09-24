"""Model capability profiles (Feature 2 of docs/advanced-model-modes-plan.md).

The load-bearing test here is the non-deprecation guarantee: with the feature
off, every routed role's prompt must be byte-identical to what shipped before
the feature existed. It is asserted, not asserted about.
"""

import unittest

from long_exposure import model_profiles as mp
from long_exposure.agent_routing import AGENT_TYPES
from long_exposure.orchestrator import (
    FRAMEWORK_PRESETS,
    assemble_system_prompt,
    load_config,
    render_stages_block,
)


def _cfg(**overrides):
    cfg = load_config()
    cfg.update(overrides)
    return cfg


def _advanced(model="claude-fable-5-1", **extra):
    block = {
        "enabled": True,
        "auto": True,
        "families": {"advanced": ["fable", "astra"]},
    }
    block.update(extra)
    return _cfg(model=model, model_profiles=block)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        mp._reset_warnings()

    def test_disabled_resolves_standard_even_for_an_advanced_model(self):
        cfg = _advanced(enabled=False)
        self.assertEqual(mp.resolve(cfg), mp.STANDARD)
        self.assertEqual(mp.knobs(cfg), {})

    def test_family_substring_match(self):
        for model in ("fable", "claude-fable-5-1", "CLAUDE-FABLE-9", "astra-x"):
            self.assertEqual(mp.resolve(_advanced(model=model)), mp.ADVANCED, model)

    def test_unknown_model_falls_back_to_default_not_advanced(self):
        self.assertEqual(mp.resolve(_advanced(model="claude-opus-4-8")), mp.STANDARD)

    def test_explicit_profile_beats_auto(self):
        cfg = _advanced(model="claude-opus-4-8", profile="advanced")
        self.assertEqual(mp.resolve(cfg), mp.ADVANCED)
        cfg = _advanced(model="claude-fable-5-1", profile="standard")
        self.assertEqual(mp.resolve(cfg), mp.STANDARD)

    def test_unknown_profile_name_falls_back_and_warns(self):
        cfg = _advanced(profile="turbo")
        self.assertEqual(mp.resolve(cfg), mp.STANDARD)

    def test_unknown_default_name_falls_back_to_standard(self):
        cfg = _advanced(model="claude-opus-4-8", default="ludicrous")
        self.assertEqual(mp.resolve(cfg), mp.STANDARD)

    def test_auto_off_ignores_the_family_match(self):
        cfg = _advanced(auto=False)
        self.assertEqual(mp.resolve(cfg), mp.STANDARD)

    def test_empty_model_id_does_not_match_a_family(self):
        self.assertEqual(mp.resolve(_advanced(model="")), mp.STANDARD)

    def test_family_order_is_deterministic_not_dict_order(self):
        """A model matching two families must resolve the same way every run."""
        cfg = _advanced(
            model="fable-astra",
            families={"advanced": ["fable"], "standard": ["astra"]},
        )
        self.assertEqual(
            {mp.resolve(cfg) for _ in range(20)}, {mp.resolve(cfg)},
        )


class KnobTests(unittest.TestCase):
    def setUp(self):
        mp._reset_warnings()

    def test_advanced_sets_exactly_the_four_documented_knobs(self):
        self.assertEqual(
            set(mp.knobs(_advanced())),
            {
                "require_checkpoint_first",
                "checkpoint_format",
                "anti_patterns_enabled",
                "framework_verbosity",
            },
        )

    def test_overrides_beat_the_profile(self):
        cfg = _advanced(overrides={"anti_patterns_enabled": True})
        self.assertIs(mp.knobs(cfg)["anti_patterns_enabled"], True)

    def test_override_of_a_non_guidance_key_is_ignored(self):
        """A profile must not be able to reach working_directory or a model id."""
        cfg = _advanced(
            overrides={"working_directory": "/etc", "model": "haiku", "compact_db": "x"}
        )
        applied = mp.knobs(cfg)
        for forbidden in ("working_directory", "model", "compact_db"):
            self.assertNotIn(forbidden, applied)

    def test_unknown_verbosity_falls_back_to_full(self):
        cfg = _advanced(overrides={"framework_verbosity": "terse"})
        self.assertEqual(mp.knobs(cfg)["framework_verbosity"], mp.VERBOSITY_FULL)

    def test_apply_does_not_mutate_the_caller_config(self):
        cfg = _advanced()
        before = dict(cfg)
        mp.apply(cfg)
        self.assertEqual(cfg, before)

    def test_apply_is_identity_when_disabled(self):
        cfg = _advanced(enabled=False)
        self.assertEqual(mp.apply(cfg), dict(cfg))


class StagesBlockTests(unittest.TestCase):
    def setUp(self):
        self.stages = FRAMEWORK_PRESETS["staged"]["stages"]

    def test_full_is_the_default_and_unchanged(self):
        self.assertEqual(
            render_stages_block(self.stages),
            render_stages_block(self.stages, mp.VERBOSITY_FULL),
        )

    def test_lean_drops_exactly_three_element_types(self):
        full = render_stages_block(self.stages, mp.VERBOSITY_FULL)
        lean = render_stages_block(self.stages, mp.VERBOSITY_LEAN)
        for dropped in ("<exit-gates>", "<failure-modes>", "<depth-calibration>"):
            self.assertIn(dropped, full)
            self.assertNotIn(dropped, lean)
        # and keeps the parts that say what a stage IS
        for kept in ("<stage name=", "<purpose>", "<required-output>", "</stage>"):
            self.assertIn(kept, lean)

    def test_lean_states_an_exit_gate_policy(self):
        """Lean must stay coherent, not just short.

        The framework template's transition rules and the checkpoint
        envelope both still refer to "the stage's exit gates". Dropping the
        enumeration without redefining what a gate check means would tell
        the agent to answer a list that is not in its prompt.
        """
        lean = render_stages_block(self.stages, mp.VERBOSITY_LEAN)
        full = render_stages_block(self.stages, mp.VERBOSITY_FULL)
        self.assertIn("<exit-gate-policy>", lean)
        self.assertNotIn("<exit-gate-policy>", full)
        self.assertIn("derive them yourself", lean)

    def test_lean_keeps_every_stage(self):
        lean = render_stages_block(self.stages, mp.VERBOSITY_LEAN)
        self.assertEqual(lean.count("<stage name="), len(self.stages))

    def test_lean_is_shorter(self):
        self.assertLess(
            len(render_stages_block(self.stages, mp.VERBOSITY_LEAN)),
            len(render_stages_block(self.stages, mp.VERBOSITY_FULL)),
        )


class PromptAssemblyTests(unittest.TestCase):
    def setUp(self):
        mp._reset_warnings()

    def test_off_is_byte_identical_for_every_routed_role(self):
        """The non-deprecation guarantee.

        An absent block, an explicitly disabled block, and an enabled block
        resolving to `standard` must all produce the same prompt as before
        the feature existed — for each of the eight routed agent types, with
        an advanced model id in play so a leaky implementation is caught.
        """
        base_cfg = load_config()
        for role_name in AGENT_TYPES:
            role = f"[ROLE: {role_name}]"
            absent = assemble_system_prompt(dict(base_cfg), role=role)

            disabled = dict(base_cfg)
            disabled["model"] = "claude-fable-5-1"
            disabled["model_profiles"] = {
                "enabled": False,
                "auto": True,
                "families": {"advanced": ["fable"]},
            }
            standard = dict(base_cfg)
            standard["model_profiles"] = {"enabled": True, "profile": "standard"}

            self.assertEqual(
                absent, assemble_system_prompt(disabled, role=role), role_name,
            )
            self.assertEqual(
                absent, assemble_system_prompt(standard, role=role), role_name,
            )

    def test_advanced_thins_ceremony(self):
        full = assemble_system_prompt(load_config(), role="[ROLE: worker]")
        lean = assemble_system_prompt(_advanced(), role="[ROLE: worker]")
        self.assertIn("== ANTI-PATTERNS ==", full)
        self.assertNotIn("== ANTI-PATTERNS ==", lean)
        self.assertIn("<exit-gates>", full)
        self.assertNotIn("<exit-gates>", lean)
        # `minimal` keeps a one-line gate-check; what it drops is the
        # narrative pair and the multi-line gate answers.
        self.assertIn("<what-i-did>", full)
        self.assertNotIn("<what-i-did>", lean)
        self.assertIn("<next-action>", full)
        self.assertNotIn("<next-action>", lean)
        self.assertLess(len(lean), len(full))

    def test_advanced_never_thins_machinery(self):
        """Philosophy, protocol rules and the role block survive intact."""
        lean = assemble_system_prompt(_advanced(), role="[ROLE: worker] MARKER")
        for kept in (
            "[ROLE: worker] MARKER",        # role block, layer 3.5
            '<philosophy preset=',          # layer 1 preset text
            "== WHO YOU ARE ==",            # layer 1 body
            "== TOKEN ECONOMICS ==",        # layer 1 body
            '<framework preset=',           # layer 2 wrapper survives
            "== STAGES ==",                 # layer 2 keeps its stages
            "== DIRECTORY BOUNDARIES ==",   # layer 3 path fence
            "== COMPACTION PROTOCOL ==",    # layer 3 mechanism
            "== CHECKPOINT DISCIPLINE ==",  # layer 3 mechanism
            "== WOLFRAM EXECUTION ==",      # layer 3 tool contract
        ):
            self.assertIn(kept, lean, kept)

    def test_per_agent_resolution_from_the_routed_model(self):
        """Two roles, two models, one run: different profiles, same code path.

        `build_agent_config` already resolves each role's `model`, so the
        assembler sees a per-agent config and needs no extra argument.
        """
        block = {
            "enabled": True,
            "auto": True,
            "families": {"advanced": ["fable"]},
        }
        researcher = _cfg(model="claude-fable-5-1", model_profiles=block)
        auditor = _cfg(model="claude-opus-4-8", model_profiles=block)
        self.assertEqual(mp.resolve(researcher), mp.ADVANCED)
        self.assertEqual(mp.resolve(auditor), mp.STANDARD)

        r_prompt = assemble_system_prompt(researcher, role="[ROLE: researcher]")
        a_prompt = assemble_system_prompt(auditor, role="[ROLE: auditor]")
        self.assertNotIn("== ANTI-PATTERNS ==", r_prompt)
        self.assertIn("== ANTI-PATTERNS ==", a_prompt)

    def test_two_roles_on_the_same_model_get_the_same_profile(self):
        """Prompt-cache safety: resolution is a pure function of the config."""
        block = {"enabled": True, "auto": True, "families": {"advanced": ["fable"]}}
        one = _cfg(model="claude-fable-5-1", model_profiles=block)
        two = _cfg(model="claude-fable-5-1", model_profiles=block)
        self.assertEqual(mp.resolve(one), mp.resolve(two))
        self.assertEqual(
            assemble_system_prompt(one, role="[ROLE: x]"),
            assemble_system_prompt(two, role="[ROLE: x]"),
        )


class ShippedConfigTests(unittest.TestCase):
    def test_shipped_config_ships_the_feature_off(self):
        cfg = load_config()
        self.assertFalse(mp.enabled(cfg))
        self.assertEqual(mp.knobs(cfg), {})

    def test_shipped_families_name_the_advanced_models(self):
        families = mp.settings(load_config())["families"]
        self.assertIn("fable", families["advanced"])
        self.assertIn("astra", families["advanced"])


if __name__ == "__main__":
    unittest.main()
