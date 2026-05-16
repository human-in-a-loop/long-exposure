# Figures as First-Class Deliverables

In a multi-day research run, figures are the highest-bandwidth
artifacts the human-in-the-loop uses to interrogate trust. Without
explicit conditioning, agents tend to leave figure production
implicit ("I'd plot this if asked") and figures slip through the
cracks. The fix is **soft-guidance across all six conditioning
surfaces** — researcher, worker, auditor, reporter, curator, promise
ledger — plus a small amount of code support.

### Why this isn't a separate "figurer" agent

A dedicated agent would centralise figure-production decisions, but
figures aren't a separable concern: each agent already has the
context to decide what to plot, when to plot, and how to caption.
Adding a figurer would introduce coordination overhead without making
better figures. Conditioning + tracking is the simpler lever.

The same reasoning applies to interrogation kits (curated figure
suites for trust validation) — useful in a future regime of
genuinely opaque autonomous research, deferred today. See
 D-deferred.

### Conditioning across the six surfaces

| Agent / surface | What it's told |
|---|---|
| **Researcher** | When the next sub-topic warrants a figure, name it in the brief: which file, what type (line / scatter / heatmap / distribution / cross-comparison), what it should show. Treat as a commitment, not a suggestion. |
| **Worker** | Figures are first-class deliverables, not decorative add-ons. When the brief names a figure or your judgment is the result is comparative / distributional / temporal, produce it. Save with a caption embedded in the file metadata or a sibling `.txt`. List in `artifacts` field of ledger event. |
| **Auditor** | Cycle audit verifies named figures exist; flags missing figures as findings. Final auditor adds figure-coverage to its document stage report. |
| **Reporter** | Periodic and final reports embed figures inline (markdown image links). Reporter scans `working_dir` for figure files and pulls them into the narrative where relevant. |
| **Curator** | Bundles figures under `figures/` in the package ZIP. Manifest entries with `role: figure` are staged there explicitly (see `end-of-run-pipeline.md`). |
| **Promise ledger** | `artifacts` field lists figure paths produced by the event. The auditor's `promise_check` verifies each listed artifact exists. |

### File-organization convention

Figures co-locate with their source data — **NOT** a separate
`figures/` folder at the workspace level. Rationale: a plot's
provenance and the data it shows belong together; separating them
makes regeneration harder. The curator's package bundles figures into
`figures/` for browsing, but that's a presentation choice, not a
storage convention.

```
<workspace>/
 benchmark-01/
 raw_data.csv
 summary.csv
 plot_summary.png # ← figure here, with the data
 plot_residuals.png # ← also here
 notes.md
```

`org_check.py` allows image files anywhere in managed folders; it
flags them only at workspace root.

### Code support (small)

The actual code prerequisites are minimal (~50 LOC across 5 files):

- Reporter receives `working_dir` as input so it can scan for figures
 to embed.
- Curator's role enum includes `"figure"` for CURATION.yaml entries.
- `org_check.py` allows images in domain folders.
- `promise_check.py` verifies listed artifacts exist (this was
 generalised in the unified `_check_artifact_coherence` walk; see
 `workspace-conventions.md`).

The bulk of the work is soft-guidance text in
`exploration-score.yaml`.

### The unified `figure` CLI (shipped)

Long-exposure exposes a single Bash entry point — `figure` — that the worker
calls for visual output. One discoverable name, one allowlist entry
(`Bash(figure *)`), and a per-backend renderer module keep future DSLs as
plug-in additions.

| Subcommand | Backend | Figure category |
|---|---|---|
| `figure plot` | matplotlib (Python) | quantitative data plots (line / bar / scatter / hist / heatmap / contour / surface / time-series / Q-Q / ROC / faceted grids) |
| `figure flow` | D2 (Go binary; ELK layout default; PNG default) | flowcharts, sequence, state machines, class diagrams, ERDs, structural / architecture diagrams |
| `figure arch` | mingrammer/diagrams (Python over Graphviz) | cloud / system architecture with curated component icons (AWS / GCP / Kubernetes / OnPrem) |
| `figure check` | local validator | post-render sanity check (size > 1 KB, file exists) |
| `figure list` | — | discovery |

**Tool selection rationale:** `D2` was chosen as the primary
structural-diagram backend over Mermaid / Graphviz / PlantUML after
an internal evaluation against eight criteria (clean defaults, layout
engines, declarative source, license, ecosystem activity, output
formats, install footprint, render speed).

**Output format default: PNG.** Embeds cleanly into the existing
pandoc + tectonic toolchain. SVG is opt-in via `--format svg` on
`figure flow`. PNG rendering for D2 pulls Chromium (~165 MB,
one-time) on first invocation; pre-warm during deployment.

**The `<figure-tooling>` soft-guidance block** lives in the worker
role text in `exploration-score.yaml` immediately after
`<figure-discipline>`. It tells the worker which subcommand to use
for which figure type, the D2 brace-in-label gotcha, and the
"change structure, not coordinates" iteration discipline.

**Code locations:**

- CLI entry: `long_exposure/tools/figure.py`.
- Renderer modules: `long_exposure/tools/figure_renderers/`
 (`d2_runner.py`, `diagrams_runner.py`, `matplotlib_runner.py`).
- Console script registration: `pyproject.toml` `[project.scripts]`
 → `figure = "long_exposure.tools.figure:main"`.
- Worker allowlist: `Bash(figure *)` at score level in
 `exploration-score.yaml`.
- Soft-guidance: `<figure-tooling>` block in worker role text
 (search the score for the tag).

**Status of the three backends as of shipping date:**

- ✅ `figure plot` — fully working (matplotlib in venv).
- ✅ `figure flow` — fully working (D2 binary at `~/.local/bin/d2`).
- ⚠️ `figure arch` — implemented but blocked on missing `graphviz`
 system binary (`apt install graphviz` required). The subcommand
 emits a clear actionable error when `dot` is not on PATH.

### What's not covered

- **Static interrogation kits.** A curated figure suite the human
 uses to validate trust in long opaque runs. Useful when concept
 complexity exceeds what the human can fully internalise. Deferred —
 re-engagement criterion is "live runs produce concepts the operator
 cannot mentally simulate."
- **Dynamic figure generation on demand.** Out of scope; the agent
 decides during the cycle.
- **Figure quality validation.** Out of scope; the auditor is told to
 *check existence*, not assess aesthetic quality.

### Operational rules

1. Figures named in researcher brief → tracked as artifacts → verified
 by auditor.
2. Figures co-located with source data, not in a separate
 workspace-level `figures/` folder.
3. Curator's package places figures in `figures/` subdirectory for
 browsing.
4. Promise ledger `artifacts` field is the canonical record of what
 was produced.
5. No new agent role; soft-guidance plus minimal code support.

---
