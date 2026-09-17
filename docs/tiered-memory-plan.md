# Tiered narrative memory: the run memoir (L1) over existing L2/L3

Status: **implemented on `claude/long-exposure-tiered-memory`.** Never to
`main` without explicit instruction; see §14.

## 1. What this is, in one paragraph

Long-exposure already has two tiers of narrative memory — periodic reports
(L2) and compaction summaries in `sessions.db` (L3) — and neither is ever
*pushed* to a headless agent. This plan adds the missing top tier: a
single, small, agent-owned file, `MEMOIR.md`, rewritten with minimal edits
by the auditor at the end of every cycle and injected into the researcher
and worker at the start of the next. Every superseded version is archived
with a cycle number and timestamp so agents can back-reference, but only
the newest is ever placed in a context window. It holds the narrative and
negative knowledge that the plan and ledger structurally cannot — the
current thesis, what has been ruled out, what the run is standing on
unverified, what is parked — and it is advisory: where it disagrees with
the plan of record or the promise ledger, they win.

One new file, one new template, ~170 lines of code, zero new agents, zero
new LLM calls. Superseded versions are also stored as `record_type='memoir'`
rows in `sessions.db`, so `search_sessions` finds old memoirs alongside
compaction rows.

## 2. The gap this closes

`_compute_gems` — the only mechanism that pushes past-session memory into
a prompt — is called from exactly two sites, `orchestrator.py:4422` and
`:4784`, both with `interactive_repl=True`. The headless conductor
(`conductor.py:573`) never passes `gems_xml`. So in the mode long-exposure
does research in, the narrative memory an agent receives automatically is
the previous cycle's three outputs plus the plan and ledger summary. A
cycle-40 researcher sees cycle 39. Cycles 1–38 exist in thirteen periodic
reports and in compaction rows, reachable only if the agent chooses to
`Read` or call `search_sessions` — voluntary recall, under the same
context pressure that makes agents skip optional work.

## 3. The three tiers, mapped to what exists

| Tier | Artifact | Exists today? | Residency after this plan |
|---|---|---|---|
| **L1** — synthesized key memory | `MEMOIR.md` | **No** | Injected into researcher + worker every cycle |
| **L2** — narrator | `reports/report_cycles_N.md` (every 3 cycles) | Yes | Pull; named by L1's pointers |
| **L3** — detail | `sessions.db` compaction rows (FTS5), lemmas, cycle outputs, `memoir/history/` | Yes | Pull via `search_sessions` / `Read` / `Grep` |

Two of three tiers are already written and simply unused. L1 is the only
new artifact, and the archive of superseded L1 versions becomes part of
L3.

## 4. Decisions taken

| Decision | Choice |
|---|---|
| Cadence | Once per cycle: auditor rewrites at the end of cycle N; researcher and worker receive it at the start of cycle N+1 |
| Author | **The auditor**, with two sentences of added guidance: minimal edits, only what changed this cycle |
| Rewrite policy | In place. The superseded version is archived, stamped with cycle and timestamp, searchable — never injected |
| Who gets it in-window | **Researcher and worker only**, as an input. Nobody else |
| Auditor's view | The **file path**, not the content. It reads and edits the file with its own tools |
| Shape | **Fixed skeleton** (§5), each section capped, global token cap enforced by the harness |
| Authority | Advisory. Plan of record and promise ledger win on any conflict; the memoir is what gets corrected |
| Archive trigger | **Only when the auditor changed the file** (`file_signature` before vs after). Unchanged cycles leave no entry; a gap in cycle numbers means "unchanged" |
| Archive searchability | Files in `memoir/history/` **and** a `record_type='memoir'` row in `sessions.db`, using the `lemmas.py` pattern, so `search_sessions` surfaces them |
| Reporter | **Untouched.** No pointer in its role. It may `Read` the file on its own initiative like any workspace file; nothing tells it to |
| Branch | `claude/long-exposure-tiered-memory`, cut from `claude/long-exposure-benchmarking-pikxm2`; never to `main` without explicit instruction |

## 5. The skeleton

### What is already pushed or held elsewhere — and therefore excluded

| Kind of content | Where it already lives | Pushed to agents? |
|---|---|---|
| How to work, stages, gates, anti-patterns | System prompt (4 layers) | Yes, every call |
| Operator steer, fan-out guidance | `live_guidance` | Yes, every cycle |
| Milestones, committed work | `plan_of_record` | **Yes**, every cycle |
| Claims with evidence, milestone state | `promise_ledger_summary` | **Yes**, every cycle |
| Last cycle's verdict and next-step guidance | `audit_report` | Yes |
| This cycle's brief / output | `research_brief`, `work_output` | Yes |
| Per-session decisions, threads, open questions | L3 compaction rows | Pull |
| Cycle-by-cycle narrative | L2 reports | Pull |
| Infra and env quirks, failed tool invocations | Lemmas (`failed_attempt` etc.) | Pull |
| Lessons | `lessons.md` | End of run |

So the memoir must carry **none** of: next steps, verdicts, milestones,
claims, evidence, quirks, or a running narrative. What remains is the
run-wide, slow-moving, *interpretive* layer — the things that get
destroyed by compaction and that a fresh fan-out clone most needs.

### The six sections

```markdown
# Run memoir

<!-- Advisory narrative back-reference. Where this disagrees with
     plan_of_record.md or the promise ledger, THEY win; fix this file.
     Superseded versions: memoir/history/. Keep every section within cap. -->

## Thesis                                    (≤ 3 sentences)
The run's current best answer or frame. Not a milestone, not a claim
with evidence — the interpretive stance everything else serves.

## Standing on                               (≤ 5 items)
Premises the run is PROCEEDING AS IF true but has not verified.
- <premise> — since cycle N — would change: <what breaks if wrong>
When one is verified it moves to the ledger and leaves this list.
When one breaks it moves to Ruled out.

## Ruled out                                 (≤ 8 items)
Research approaches tried and abandoned. NOT infra failures (lemmas).
- <approach> — <why it failed> — cycle N — see <report or path>

## Parked                                    (≤ 5 items)
Deliberately deferred, with the trigger that reopens it. Committed work
belongs in plan_of_record.md, not here.
- <what> — reopen when: <trigger> — parked cycle N

## Where to look                             (≤ 6 pointers)
The L2 reports, files and session ids that carry the current thesis.
- <path or session id> — <what it holds>

## Changed this cycle                        (1–3 lines, replaced every cycle)
What moved since the last memoir. Never accumulates.
```

Why each earns its place:

- **Thesis** is the one thing no other surface holds. The plan says what
  we will do; the ledger says what we have shown; nothing says what we
  currently *think the answer is*. It moves slowly, which suits minimal
  edits.
- **Standing on** is the ledger's complement. The ledger holds claims
  *with* evidence; this holds premises *without* it. The promotion rule
  — verified → ledger, broken → Ruled out — is what stops the memoir
  from quietly becoming a second truth store.
- **Ruled out** is the anti-repeat memory and the highest-value section.
  A 40-cycle run without it re-tries dead ends after every compaction.
  It is explicitly research dead ends, not the `failed_attempt` lemma
  category, which is infrastructure.
- **Parked** captures "not now, but when X" — a shape neither the plan
  (commitments) nor open-questions (unknowns) holds.
- **Where to look** is what makes this a *cache* rather than a summary:
  L1 carries the tags that make L2 and L3 navigable without a search.
- **Changed this cycle** is the diff. It makes the minimal-update
  discipline concrete (the auditor writes what changed, and that *is*
  the edit), and it is the first thing a researcher should read.

### Eviction

Per-section caps, and a global cap of 3,000 tokens (`memoir.max_tokens`).
When a section is over cap the auditor drops the entry least likely to be
re-tried or re-needed — by default the oldest — and the archive keeps it.
The harness enforces the global cap at injection (§7) so an undisciplined
edit cannot blow the prompt.

## 6. Cycle mechanics

```
cycle N start
  ├─ read MEMOIR.md ──► results["run_memory"]     (researcher, worker input)
  ├─ score_inputs["memoir_path"] = <abs path>     (auditor input; read-only note in a clone)
  ├─ researcher  (run_memory in window)
  ├─ worker      (run_memory in window)
  └─ auditor     (memoir_path in window; edits MEMOIR.md in place, minimal)
       └─ after success, at root only:
            file_signature changed?  ──► copy to memoir/history/cycle-NNNN_<ts>.md
                                          + sessions.db row, record_type='memoir'
cycle N+1 start
  └─ read MEMOIR.md ──► …
```

Rules at the edges:

- **First cycle.** `MEMOIR.md` is seeded lazily from
  `templates/memoir_template.md` the first time a cycle injects it — not
  by `bootstrap_workspace`, whose fresh-start-only contract would leave a
  workspace that predates the feature without one on resume. Cycle 1's
  researcher and worker see the empty skeleton, which teaches the shape.
- **Injection header.** The harness prefixes the injected value with one
  line — `[Run memoir — last updated <mtime>; older versions in
  memoir/history/; advisory, ledger and plan win on conflict]` — so the
  live file stays purely agent-owned and no template text is needed.
- **Post-merge cycle.** Runs worker-only (no auditor, `exploration.py`
  post-merge mode), so no memoir write that cycle; the next full cycle's
  auditor catches up. Its `Changed this cycle` then covers two cycles,
  which is fine.
- **Fan-out clones.** They share the workspace, so they *read* the memoir
  for free. They must **never write it** — three clone auditors editing
  one file is a race. In a clone the `memoir_path` input carries a
  read-only note instead of the path, and the archive hook is gated on
  the root process. The root auditor folds the branches' merge reports
  into the memoir at the next full cycle.
- **Resume.** The file lives in the workspace; nothing is added to
  `exploration_state.json`. The archive filename is the only state.
- **Unchanged cycle.** No archive entry. "No update" is the normal
  outcome of minimal-edit discipline, not an error.
- **End of run.** The final auditor reads the workspace anyway and may
  mine `Ruled out` for `lessons.md` — free synergy, no wiring.

## 7. Guardrails

1. **Global cap enforced at injection.** If `MEMOIR.md` exceeds
   `memoir.max_tokens` (via `estimate_tokens`), inject the head, append
   `[memoir over cap — auditor must trim]`, and emit a
   `memoir_over_cap` health event. The agent still gets something; the
   prompt stays bounded.
2. **Ledger wins.** Stated in the template header, in the auditor's
   guidance, and in the injection header. Three places because it is the
   rule that keeps this from becoming a second source of truth.
3. **Root-only writes.** Gated on the existing clone predicate.
4. **Never crash the cycle.** Every read, inject and archive is
   best-effort with the same `try/except` shape as the plan/ledger
   injection block it sits beside.
5. **Off switch.** `memoir.enabled: false` removes the seed, the inputs
   (they degrade to `[UNAVAILABLE: run_memory]` if still declared — so
   the score inputs are added conditionally at load) and the archive hook.

## 8. Integration points

| Seam | Location | Change |
|---|---|---|
| Paths | `paths.py:180` `ensure_layout` + new `memoir_path()`, `memoir_history_dir()` | ~15 lines |
| Seed | `memoir.seed_if_missing`, called from `read_for_injection`; `templates/memoir_template.md` | ~15 lines + template |
| Inject | `exploration.py:~4140`, beside the plan/ledger injection block | ~20 lines |
| Auditor path input | Same block: `score_inputs["memoir_path"]` (root) or read-only note (clone) | ~6 lines |
| Archive | `exploration.py:~4306`, after the auditor's successful result lands | ~10 lines calling a helper |
| Helper module | New `long_exposure/memoir.py`: `read_for_injection`, `archive_if_changed`, `store_row` | ~100 lines |
| Score | `exploration-score.yaml`: `run_memory` in researcher/worker inputs, `memoir_path` in auditor inputs, two sentences in the auditor role | ~6 lines |
| Config | `config.yaml`: `memoir: {enabled: true, max_tokens: 1200}` | 3 lines |

Roughly 170 lines of production code. Reuses `stage_io.file_signature`
(change detection), `stage_io.atomic_write_text` (archive write),
`estimate_tokens` (cap), and `store_session` exactly as `lemmas.py` uses
it for the archive row.

## 9. The auditor's two sentences

> After your audit, make MINIMAL edits to the run memoir at
> `[memoir_path]` — only what this cycle changed (a thesis shift, an
> approach ruled out, a premise confirmed or broken, something parked),
> leaving unchanged sections untouched and every section within its cap.
> The memoir is narrative back-reference, not a source of truth: where it
> disagrees with the plan of record or the promise ledger, they win and
> the memoir is what you correct.

## 10. Config

```yaml
memoir:
  enabled: true        # seeds MEMOIR.md, injects into researcher/worker, archives after the auditor
  max_tokens: 3000     # global cap enforced at injection; the template's per-section caps sit under it
```

Two knobs. No cadence knob (it is the cycle), no author knob (it is the
auditor), no per-role toggle (the roles are fixed by decision).

## 11. Tests

`tests/test_memoir.py`, offline, using the `_write_files` /
`_fake_agent_factory` harness from `tests/test_run_switches.py`:

- The first injection seeds the skeleton; a later cycle does not overwrite
  it; a workspace that predates the feature is seeded on its next cycle.
- Researcher and worker prompts contain `[INPUT: run_memory]` with the
  file's content and the injection header; the auditor's prompt contains
  `[INPUT: memoir_path]` and **not** the content; the reporter's prompt
  contains neither.
- A changed memoir after the auditor step produces exactly one
  `memoir/history/cycle-NNNN_*.md` whose content equals the live file;
  an unchanged one produces none.
- Over-cap content is truncated at injection with the marker and a
  `memoir_over_cap` health event; the live file is untouched.
- In a clone, `memoir_path` renders the read-only note and no archive is
  written.
- `memoir.enabled: false` → no seed, no inputs, no archive.
- One `record_type='memoir'` row per archive in `sessions.db`, findable
  via the FTS query path `search_sessions` uses; none when unchanged.

## 12. Docs

- `configuration-reference.md` — the `memoir` block.
- `workspace-conventions.md` — `MEMOIR.md` and `memoir/history/` in the
  layout, and the promotion rule (verified → ledger, broken → Ruled out).
- `persistence-and-gems.md` — the three tiers and what is pushed vs
  pulled, with the gems-are-REPL-only fact stated plainly.
- `architecture-overview.md` — one paragraph and the cycle diagram from §6.

## 13. Deliberately not built

- No memoirist agent. The auditor already judges what is true each cycle.
- No per-role memoirs. One run, one memoir.
- No LLM-side fold or summarisation call. The edit *is* the fold.
- No harness-side parsing of the sections. The skeleton is a convention
  the auditor keeps, enforced only by the global token cap.
- No ranking or retrieval over the archive. `Grep` and `search_sessions`
  are enough; the archive is small and cycle-numbered.
- No state in `exploration_state.json`.
- No change to compaction, gems, the ledger, the plan, or the reporter's
  role text.

## 14. Branch

This changes the prompt every researcher and worker sees, which would
invalidate the "stock configuration, four disclosed deviations" basis of
the ResearchClawBench run if it landed on
`claude/long-exposure-benchmarking-pikxm2`. Implementation goes on
`claude/long-exposure-tiered-memory`, cut from that branch, and never to
`main` without explicit instruction. The benchmark branch carries this
document as a plan only.

## 15. Resolved

| Question | Decision |
|---|---|
| Archive rows in `sessions.db` | **Yes, now.** `search_sessions` finds old memoirs alongside compaction rows |
| Reporter pointer | **No.** The reporter stays untouched |
| Archive trigger | **Only on change** |
| Branch | `claude/long-exposure-tiered-memory` |

Still a guess until a real run says otherwise: the per-section counts in
§5 (the global cap is set at 3,000 tokens by decision). The first `memoir/history/` will show
whether they bind.
