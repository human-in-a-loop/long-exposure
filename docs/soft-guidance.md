# Soft-Guidance Philosophy

The agents in long-exposure are conditioned through layered system
prompts: philosophy + framework + operating-protocol + per-agent role
text + runtime-injected blocks. None of these conditioning surfaces
*enforce* anything in code — they shape the agent's disposition. This
doc covers the design philosophy of soft-guidance, the canonical
research-wisdom phrasings that have been encoded, and the principles
that govern when to add new soft-guidance vs. write code.

---

## Why soft-guidance, not code enforcement

The system makes a recurring choice in favor of conventions over
enforcement. The reasons:

1. **Agents have context the harness doesn't.** A figure named in
   the brief might genuinely be impossible this cycle (data not
   ready); an agent that decides to skip and explain is more useful
   than a hard-failed cycle.
2. **Agents handle exceptions and gradients gracefully.**
   "Document null results with the same rigor as positive results"
   is a directive the agent applies to a continuum of cases. A code
   check would have to draw an arbitrary line.
3. **Soft guidance composes.** Multiple guidance blocks can shape
   the same decision. Hard checks force a single decision tree.
4. **Conventions degrade gracefully.** A run that ignores guidance
   still completes; a run that fails a hard check stops.

The price is that you can't prove from the code alone that a
convention is followed. The validators (`promise_check`,
`org_check`) close part of this gap by *surfacing* non-compliance
without gating.

---

## Where soft-guidance lives

| Layer | Source | Audience |
|---|---|---|
| **Philosophy template** (layer 1) | `long_exposure/templates/philosophy-template.md`; values from `orchestrator.PHILOSOPHY_PRESETS` | All agents inheriting that philosophy across all deployments |
| **Framework template** (layer 2) | `long_exposure/templates/framework-template.md`; values from `orchestrator.FRAMEWORK_PRESETS` | All agents inheriting that framework |
| **Operating protocol** (layer 3) | `long_exposure/templates/operating-protocol-template.md` | All agents in any score |
| **Per-agent role text** | `long_exposure/exploration-score.yaml` (XML blocks under each agent's `role:`) | The specific agent in this score |
| **Runtime-injected blocks** | Live guidance (cycle-by-cycle), agent-teams template (when active), context gems (proximity-ranked), parallel-cycle fan-out guidance | Per-call basis |

When deciding *where* to add a piece of soft-guidance, the test is
**scope**:

- **Universal across deployments?** → philosophy template (e.g.
  "null results are research assets").
- **Universal across this run only?** → score YAML role text (e.g.
  fan-out independence criteria specific to this domain).
- **Cycle-by-cycle dynamic?** → live guidance (e.g. current pool
  cap, recent failure-streak signal).

---

## Two canonical refinements

Two single-line soft-guidance additions that encode research wisdom
the system used to leave implicit. Both ship verbatim today.

### Refinement A — Multiple-methods fan-out (researcher role)

**Location:** `exploration-score.yaml`, researcher role,
`<handoff to="worker">` block, after the
`<parallel_cycle_fanout>` reference.

**Phrasing:**

> When fanning out branches that attack the same problem, prefer
> methods that are fundamentally independent (analytical vs.
> numerical, in-house code vs. external tooling, simulation vs.
> experimental) — convergent results across independent methods are
> the strongest validation.

**Why:** Fan-out branches are an opportunity to set up independent
verification, not just to parallelise the same approach. Convergent
results across genuinely independent methods give the auditor more
evidence sources for any given milestone, supporting `high` confidence
assessments.

### Refinement B — Null results valuation (worker philosophy)

**Location:** worker philosophy preset's `voice` field in
`orchestrator.PHILOSOPHY_PRESETS["efficient"]`.

**Phrasing:**

> Null results and invalidated hypotheses are foundational research
> findings — document them with the same rigor as positive results;
> they constrain the design space and prevent rediscovery in future
> cycles.

**Why:** Most of research is finding null results. A worker that
treats "the approach didn't work" as a failure to suppress, rather
than a finding to document, deprives the auditor of substance and
the next cycle's researcher of constraints. Pairs with the ledger's
first-class `invalidated` status — null results land as
`invalidated` events with confidence, not as silently-missing
events.

### Composition

These two refinements compose with the broader plan set:

- **With the plan + ledger.** Refinement B reinforces the unified
  status taxonomy: a null result becomes an `invalidated` event with
  `high`/`medium` confidence rather than disappearing from the
  trail. Refinement A makes fan-out branches more orthogonal, so
  the auditor can issue `validated` + `high` confidence when
  independent methods converge.
- **With the final auditor.** Refinement A increases the auditor's
  confidence calibration accuracy. Refinement B feeds the auditor's
  residual-debt list with documented null results.
- **With cross-cutting lessons.** Refinement B is the precondition
  for null-result lessons. Without "document null results
  rigorously", null results never make it into the ledger as
  substantive findings, so they never become candidates for lessons.

---

## Acceptance signals

The two refinements are doing their job when:

1. Research briefs identify INDEPENDENT METHODS as a fan-out
   criterion, not just INDEPENDENT TASKS.
2. Fan-out branches pair complementary approaches (analytical +
   numerical, in-house + external).
3. Worker `work_output` includes substantive "Issues and
   Uncertainties" sections documenting null results explicitly.
4. The promise ledger sees `invalidated` events with `high`/`medium`
   confidence (events that take responsibility for the null finding,
   not events that just disappear).
5. End-of-run lessons include null-result lessons ("we tried X, it
   doesn't work in this regime, here's why").

---

## When to add new soft-guidance

The bar is intentionally high. Each addition costs:

- Token budget in every relevant agent's system prompt.
- Cognitive load on contributors reading the score / templates.
- Risk of conflicting with another guidance block (the agent has to
  reconcile).

A new soft-guidance block clears the bar when:

1. The behavior the system needs is not a hard rule (so code
   enforcement is wrong) AND
2. The behavior is not already implied by an existing guidance block
   (so adding is the right gesture, not a duplicate) AND
3. The behavior is universal at the chosen scope (philosophy
   template = universal across deployments; score role = universal
   for this run; live guidance = universal this cycle).

If any of those fails: the right move is usually to refine an
existing block, not add a new one.

---

## Operational rules

1. Soft-guidance shapes disposition; it doesn't enforce. Validators
   (`promise_check`, `org_check`) surface non-compliance.
2. Place by scope: philosophy = all deployments, score = this run,
   live guidance = this cycle.
3. Single-sentence additions are preferred over multi-paragraph
   blocks — agents reconcile gradients better than they navigate
   nested directives.
4. Agents reconcile multiple guidance blocks; redundancy is a cost,
   not a feature.
5. Reversibility is a feature: every soft-guidance addition should
   be a one-line edit that can be reverted to leave the system
   identical to today.

---

## Code citations

- Philosophy + framework presets: `long_exposure/orchestrator.py`
  (`PHILOSOPHY_PRESETS`, `FRAMEWORK_PRESETS`).
- Template files: `long_exposure/templates/{philosophy,framework,operating-protocol,session-summary}-template.md`.
- Per-agent role text: `long_exposure/exploration-score.yaml`.
- Live guidance computation:
  `long_exposure/exploration.py:_compute_live_guidance` (and
  `_build_fanout_guidance` in `fanout.py`).
- Validator surfacing (not enforcement):
  `long_exposure/tools/{promise_check,org_check}.py`.
