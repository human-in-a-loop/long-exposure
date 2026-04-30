"""Shared numeric limits used across long-exposure agents.

One value, multiple callers. Tuning here tunes everywhere.
"""

# 10 hours, in seconds. Wall-clock cap on the entire end-of-run synthesis
# pass for both the final auditor (auditing.py) and the final reporter
# (reporting.py). The document/finalize stage of either agent always
# runs even if the cap was hit during preceding stages — it's the commit
# step. See docs/end-of-run-pipeline.md (wall-cap section).
WALL_CAP_SECONDS = 36_000
