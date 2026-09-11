"""Shared numeric limits used across long-exposure agents.

One value, multiple callers. Tuning here tunes everywhere.
"""

# 10 hours, in seconds. Wall-clock cap on the entire end-of-run synthesis
# pass for both the final auditor (auditing.py) and the final reporter
# (reporting.py). The document/finalize stage of either agent always
# runs even if the cap was hit during preceding stages — it's the commit
# step. See docs/end-of-run-pipeline.md (wall-cap section).
WALL_CAP_SECONDS = 36_000

# Token step used to decide how many body/verify/test stages a final
# synthesis pass needs. Delta runs apply this to newly observed artifacts.
#
# One stage per ~100k tokens of input. The final auditor floors this at 1
# and caps it at `auditing._N_MAX` (5), so it runs 4..12 stages; the final
# reporter floors it at 1 and stays uncapped so a very large workspace is
# still covered section by section. Raised from 20k on 2026-09-11: at 20k a
# multi-day workspace produced dozens of stages (one LLM call each), which
# dominated end-of-run cost without adding coverage.
FINAL_STAGE_TOKEN_THRESHOLD = 100_000

# Legacy compatibility floor for detecting a pre-marker baseline. New
# successful final outputs write explicit *.committed markers and detection
# prefers those markers over byte-size heuristics.
DELTA_DETECT_MIN_BYTES = 1_000
