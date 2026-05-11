# Local Setup

Environment, installation, and dev-setup for long-exposure. For
day-to-day operation, see [`usage-guide.md`](usage-guide.md). For the
conceptual map, see [`architecture-overview.md`](architecture-overview.md).

---

## Requirements

| What | Why | Required |
|---|---|---|
| **Python 3.10+** | Runtime | Yes |
| A supported provider CLI on `$PATH`: **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)**, Codex CLI, or Gemini CLI | Model backend for `llm_provider: claude`, `codex`, or `gemini` | Yes |
| **`pyyaml`** | Config + score YAML parsing | Yes (pip-installed) |
| **`prompt_toolkit`** | Interactive orchestrator REPL | Yes (pip-installed) |
| **`anthropic`** | Python dependency installed with the package because the bundled `auto_compact` standalone CLI imports it. Long-exposure itself does not call the SDK through this path. | Yes (pip-installed); no Anthropic API key is required for normal provider-CLI runs |
| **`pandoc`** + **[`tectonic`](https://tectonic-typesetting.github.io/)** | PDF rendering of in-cycle and final reports | Yes for standard report output. `long-exposure-setup` installs/checks them where the platform package manager supports it. If absent at runtime, markdown still lands and PDF render failures are surfaced. |
| **Wolfram Engine** | Wolfram Language scripts the worker may run | Optional — set `wolfram_path: ""` in `config.yaml` if absent |
| **`matplotlib`** (Python) | `figure plot` subcommand backend (quantitative data plots). Hard dep since Plan E. | Yes (auto-installed by `uv sync` / `pip install -e .`) |
| **D2 binary** ([install](https://d2lang.com/tour/install/)) | `figure flow` subcommand backend (flowcharts, sequence, state, ERD, structural diagrams) | Optional but needed for any `figure flow` invocation. PNG rendering pulls headless Chromium (~165 MB, one-time) on first invocation |
| **`diagrams`** (Python; mingrammer/diagrams) | `figure arch` subcommand backend (cloud / system architecture with iconography) — Python side | Optional. `uv sync --extra figures-arch` installs it. |
| **`graphviz`** system binary (`dot`) | `figure arch` layout engine (the `diagrams` library invokes `dot`) — system side | Optional but required alongside `diagrams`. `apt install graphviz` (Debian/Ubuntu), `brew install graphviz` (macOS), or set `GRAPHVIZ_DOT=/path/to/dot`. ~10 MB. See Plan F. |

The auto-compact package is bundled in the same repo; no separate
install needed.

### Installing the optional `figure` CLI dependencies

If you'll use the `figure` CLI for first-class figure outputs (Plan C),
install the renderer dependencies. As of Plan E matplotlib
is a hard dep and is installed by `uv sync` automatically; only D2 and
the arch backend remain operator-side.

#### `figure plot` (matplotlib) — already installed

Hard dep since Plan E; `uv sync` puts it in the venv.
Skip ahead.

#### `figure flow` (D2)

```bash
# Single Go binary, installs to ~/.local/bin
curl -fsSL https://d2lang.com/install.sh | sh -s -- --prefix ~/.local

# Pre-warm Chromium so first PNG render isn't slowed by a ~165 MB download
echo 'a -> b' | ~/.local/bin/d2 - /tmp/_warm.png && rm /tmp/_warm.*
```

#### `figure arch` (mingrammer/diagrams + graphviz)

```bash
# Python library — opt-in extra
uv sync --extra figures-arch

# System binary — required by the diagrams layout engine (Plan F)
sudo apt-get install graphviz       # Debian / Ubuntu
# brew install graphviz             # macOS
# pacman -S graphviz                # Arch
# yum install graphviz              # RHEL / CentOS
# Or set env: export GRAPHVIZ_DOT=/path/to/dot
```

### Verifying

```bash
.venv/bin/figure list                          # subcommand discovery
.venv/bin/figure plot --help                   # matplotlib reachable (hard dep)
echo 'a -> b' > /tmp/t.d2 && .venv/bin/figure flow /tmp/t.d2 --out /tmp/t.png
.venv/bin/figure check /tmp/t.png              # post-render sanity

# Optional: only after installing --extra figures-arch + graphviz
command -v dot && .venv/bin/figure arch --help

# Optional but recommended: end-to-end arch smoke (writes /tmp/test_arch.png)
cat > /tmp/test_arch.py <<'EOF'
from diagrams import Diagram
from diagrams.aws.compute import EC2
with Diagram("smoke", show=False, filename="/tmp/test_arch"):
    EC2("hello")
EOF
.venv/bin/figure arch /tmp/test_arch.py && \
  .venv/bin/figure check /tmp/test_arch.png && \
  rm /tmp/test_arch.{py,png}
```

`figure plot` and `figure flow` are sufficient for ~95% of figure use
cases. `figure arch` is only needed for cloud / architecture diagrams
with curated iconography.

---

## Install

```bash
git clone <repo> long-exposure
cd long-exposure

# Reproducible install (recommended)
uv run long-exposure-setup --yes

# Or via pip
pip install -e .
long-exposure-setup --skip-uv-sync --yes
```

`long-exposure-setup` runs `uv sync` unless `--skip-uv-sync` is passed,
checks Python imports, reports the active package provenance, checks the
configured provider CLI, checks `pandoc` and `tectonic`, and installs missing
system binaries through the detected package manager when supported:
`apt-get`, `dnf`, `yum`, `pacman`, `zypper`, `brew`, or `winget`.
If the platform is unsupported, it prints the exact missing tools and exits
non-zero instead of guessing.

Either install gives you the `long-exposure` console script. The preferred
entry point is provider-neutral:

```bash
long-exposure launch "<directive>"
```

Claude/Codex/Gemini slash commands or skills should be thin adapters that
route to that command.

### Verifying

```bash
# 1. Deterministic environment check
long-exposure-doctor

# JSON output is useful for CI, issue reports, or agentic debugging.
long-exposure-doctor --json

# Use the same provider config that the run will use.
long-exposure-doctor --config long_exposure/config.yaml
```

The doctor prints the Python executable, imported package root, editable install
metadata, `PYTHONPATH` conflicts, the selected provider CLI status, and missing
required tools. It fails for a missing selected provider CLI; other installed
provider CLIs are reported for awareness only.

```bash
# 2. Claude CLI works in non-interactive mode
claude -p "say ok" --output-format json

# Expect: JSON envelope with "is_error":false. If this fails,
# the rest of long-exposure cannot run.

# Optional Codex provider smoke
codex exec --yolo --json -m gpt-5.5 \
  -C /path/to/trusted/workspace -o /tmp/codex-ok.txt \
  "Reply with exactly: ok" && cat /tmp/codex-ok.txt

# Optional Gemini provider smoke (Google-account / Code Assist path)
npm install -g @google/gemini-cli
GOOGLE_GENAI_USE_GCA=true gemini --skip-trust \
  --output-format json -p "Reply with exactly: ok"

# Long-exposure's Gemini default is gemini-3-flash-preview with a 1M
# context-window assumption. Gemini native subagents are disabled in
# long-exposure; whole-cycle fan-out still runs multiple independent
# Gemini CLI sessions concurrently.

# 3. long-exposure imports cleanly
python3 -c "import long_exposure.exploration; print('ok')"

# 4. Score YAML loads + validates
python3 -c "
from long_exposure.exploration import load_exploration_score
load_exploration_score('long_exposure/exploration-score.yaml')
print('score validates')
"

# 5. Focused local test suite
uv run python -m unittest discover -s tests -v

# Optional Wolfram smoke. `wolfram-batch` is bundled with long-exposure
# and is compatible with `wolfram -script FILE.wls`.
printf 'Print[$Version]\nPrint[2+2]\n' >/tmp/wolfram-smoke.wls
.venv/bin/wolfram-batch -script /tmp/wolfram-smoke.wls
```

---

## Directory layout

```
long-exposure/
├── long_exposure/                  # Core package
│   ├── orchestrator.py             # provider CLI subprocess + 4-layer prompt
│   ├── conductor.py                # Score loader + agent prompt assembly
│   ├── exploration.py              # Cycle loop + signal handling
│   ├── fanout.py                   # Parallel-cycle fan-out + barrier
│   ├── pool.py                     # Multi-account pool
│   ├── reporting.py                # Reporter + final reporter + render_pdf
│   ├── auditing.py                 # Final auditor
│   ├── curator.py                  # ZIP package builder
│   ├── workspace_bootstrap.py      # POR / ledger / folder skeleton
│   ├── health_events.py            # Off-nominal events log
│   ├── mcp_search_server.py        # MCP server for session search
│   ├── limits.py                   # WALL_CAP_SECONDS
│   ├── config.yaml                 # Deployment-wide knobs
│   ├── exploration-score.yaml      # Cycle definition + agent roles
│   ├── data/                       # Runtime state (auto-created)
│   │   ├── sessions.db             # SQLite session store + FTS5
│   │   ├── exploration_state.json  # Cycle state (overwritten each cycle)
│   │   ├── mcp_config.json         # MCP server config
│   │   └── health_events.jsonl     # Off-nominal events
│   ├── templates/                  # System-prompt templates
│   │   ├── philosophy-template.md
│   │   ├── framework-template.md
│   │   ├── operating-protocol-template.md
│   │   ├── session-summary-template.md
│   │   ├── plan_of_record_template.md
│   │   └── structure_template.md
│   └── tools/                      # Validators (called via Bash by agents)
│       ├── promise_check.py
│       ├── org_check.py
│       └── ledger_append.py
├── auto_compact/                   # Bundled context-persistence package
│   ├── db.py                       # SQLite + FTS5 + WAL
│   ├── compact.py                  # Depth-aware XML summary
│   └── proximity.py                # Gem ranking
├── docs/                           # Documentation (this doc included)
├── run_final_reporter.py           # Stand-alone final-reporter + curator entry
└── pyproject.toml
```

The bundled `auto_compact/` exposes its own `auto-compact` CLI (uses
the Anthropic SDK directly — needs `ANTHROPIC_API_KEY`). Long-exposure
itself does NOT use that CLI; it consumes `auto_compact` purely as a
Python library, and model calls go through the configured provider CLI
(`claude`, `codex`, or `gemini`).

---

## Execution modes

Long-exposure has three top-level execution modes. The exploration
mode is the one most users want.

| Mode | Entry | What it is |
|---|---|---|
| **Exploration** | `long-exposure launch "<directive>"` or low-level `long-exposure start "<directive>"` | The continuous researcher → worker → auditor loop. CLI adapters should route here. See [`usage-guide.md`](usage-guide.md). |
| **Conductor** | `python -m long_exposure.conductor <score.yaml>` | Run a multi-agent score *once* (sequential or parallel steps; no loop, no persistent sessions). For one-shot multi-agent flows. |
| **Orchestrator** | `python -m long_exposure.orchestrator` | Interactive single-agent REPL with auto-compact. Type messages, get responses; context survives compaction. |

For the architecture of each, see
[`architecture-overview.md`](architecture-overview.md). For the
configuration knobs that govern any mode, see
[`configuration-reference.md`](configuration-reference.md).

### Standalone end-of-run

If an exploration crashed before its end-of-run pipeline ran (or you
want to re-render against updated sources):

```bash
python run_final_reporter.py --state long_exposure/data/exploration_state.json
```

Loads saved state, runs `_run_final_reporter` then `_run_curator`,
re-saves state with the outputs. Also accepts `--score`, `--config`,
`--instance-dir`.

---

## Working directory and the workspace

Long-exposure agents read, write, and execute files in a single
**working directory** (also called the *workspace*), set by
`working_directory` in `long_exposure/config.yaml`. File tools
(`Read`, `Write`, `Edit`, `Glob`, `Grep`) are scoped to this directory
by Claude Code's path-permission enforcement. Bash is unrestricted
by default; you can pattern-restrict it (see
[`configuration-reference.md`](configuration-reference.md)).

For the workspace folder skeleton, plan-of-record, promise ledger,
and validation conventions, see
[`workspace-conventions.md`](workspace-conventions.md).

---

## Concurrent named instances

Multiple `long-exposure` processes can run on the same machine if each
has its own **instance directory** (`--instance-dir DIR` or
`AGENT_INSTANCE_DIR=DIR`). The instance dir holds the per-session
state file, output folder, MCP config, and signal files.

Shared across all instances:

- `sessions.db` — gem store; gems written by any session are
  immediately visible to every other session's MCP search.
- `~/.claude-pool-state.json` — multi-account pool state.
- `~/.claude-accounts-state.json` — legacy single-account index.

Per-instance:

- `<DIR>/exploration_state.json`, `<DIR>/output/`,
  `<DIR>/mcp_config.json`, `<DIR>/long-exposure.{stop,clear,guide}`,
  `<DIR>/health_events.jsonl`.

See [`usage-guide.md`](usage-guide.md) "Concurrent Sessions" for
operational examples.

---

## Multi-account setup

Long-exposure can run across multiple Claude Code config directories
(accounts) with a pool that rotates on rate-limit. To set up:

1. Create additional accounts: `mkdir -p ~/.claude-acctN`,
   `CLAUDE_CONFIG_DIR=~/.claude-acctN claude` → `/login` → `/exit`.
2. Verify each in non-interactive mode:
   `CLAUDE_CONFIG_DIR=~/.claude-acctN claude -p "say ok" --output-format json`.
3. Export the pool env var:
   `CLAUDE_ACCOUNT_POOL=~/.claude,~/.claude-acctA,~/.claude-acctB`.
4. Run normally — `pool.init_pool()` registers new accounts on first
   observation.

Single-account mode (the default if `CLAUDE_ACCOUNT_POOL` is unset) is
fine for small runs. For multi-day campaigns, ≥2 accounts is strongly
recommended — see [`multi-account-pool.md`](multi-account-pool.md).

---

## File reference

| File | Purpose | Edited by |
|---|---|---|
| `long_exposure/config.yaml` | Deployment-wide knobs (model, paths, permissions, compaction, agent-teams, proximity profiles) | Operator |
| `long_exposure/exploration-score.yaml` | Cycle definition (agents, flow, loop knobs, per-agent role text) | Operator (rare) |
| `long_exposure/templates/*.md` | System-prompt templates (philosophy / framework / protocol / session summary / plan-of-record skeleton / STRUCTURE skeleton) | Contributor |
| `long_exposure/data/sessions.db` | Single source of truth (SQLite, accumulates across runs) | System |
| `long_exposure/data/exploration_state.json` | Current cycle state (overwritten) | System |
| `long_exposure/data/health_events.jsonl` | Off-nominal events log (silent fallbacks, rescues, retries) — `tail -n 50` to surface | System |
| `long_exposure/data/long-exposure.{stop,clear,guide,pause-for-user}` | Signal files | Operator / manager |
| `long_exposure/data/manager_assessments/` | Cron manager assessment logs | Manager |
| `long_exposure/data/manager_notifications.jsonl` | Structured manager notices for launchers/status | Manager |
| `<instance>/telemetry/events.jsonl` | Opt-in passive telemetry events | System |
| `long_exposure/data/mcp_config.json` | MCP server config (points at sessions.db) | System |
| `long_exposure/data/fork-<id>/` | Fan-out fork directories (per-clone instance dirs, merge reports, shadow ledgers) | System |
| `~/.claude-pool-state.json` | Multi-account pool state | System |
| `~/.claude-accounts-state.json` | Legacy single-account rotation index | System |
| `<workspace>/plan_of_record.md` | Run contract (researcher-authored) | Researcher agent + operator |
| `<workspace>/promise_ledger.jsonl` | Append-only judgment history | All agents |
| `<workspace>/STRUCTURE.md` | Workspace folder layout (researcher-authored cycle 1) | Researcher agent |
| `<workspace>/MANIFEST.md` | Curated artifact list | Reporter / curator agents |
| `<workspace>/reports/cycles/` | Periodic reports | Reporter agent |
| `<workspace>/reports/final/` | Final reporter scratch | Final reporter agent |
| `<workspace>/audits/final/` | Final auditor scratch and sidecars | Final auditor agent |
| `<workspace>/final_report.{md,pdf}` | End-of-run synthesis | Final reporter agent |
| `<workspace>/final_audit_report.{md,pdf}` | Run-scope audit | Final auditor agent |
| `<workspace>/final_audit_summary.json` | Structured audit record | Final auditor agent |
| `<workspace>/<slug>_package.zip` | Curator's handoff bundle | Curator agent |

---

## Health-check command

```bash
long-exposure-doctor
```

Each backend prints `OK` or `missing`. Use this after install to spot
gaps fast: a `missing` row for `dot` means install graphviz (Plan F);
a `missing` for `diagrams` means run `uv sync --extra figures-arch`
(Plan E); a `missing` for `matplotlib` means `uv sync` failed
(matplotlib is now a hard dep, Plan E); a `missing` for `d2` means run
the install script in [Installing the optional `figure` CLI dependencies](#installing-the-optional-figure-cli-dependencies).

---

## Architecture detail

This doc covers setup. For:

- **How control flow works** (cycle, three roles, four-layer prompt) →
  [`architecture-overview.md`](architecture-overview.md)
- **Configuration knobs** → [`configuration-reference.md`](configuration-reference.md)
- **Multi-account pool internals** → [`multi-account-pool.md`](multi-account-pool.md)
- **Fan-out + agent-teams** → [`parallelism.md`](parallelism.md)
- **sessions.db + auto-compact + gems** → [`persistence-and-gems.md`](persistence-and-gems.md)
- **End-of-run pipeline** → [`end-of-run-pipeline.md`](end-of-run-pipeline.md)
- **Workspace conventions (POR, ledger, folder layout)** →
  [`workspace-conventions.md`](workspace-conventions.md)
- **Soft-guidance philosophy** → [`soft-guidance.md`](soft-guidance.md)
