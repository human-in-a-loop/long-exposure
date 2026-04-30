---
created: {created}
run_id: {run_id}
agent: researcher
---

# Plan of Record — {title}

**Created:** {created}
**Run id:** {run_id}

## Directive (verbatim)

{directive}

## Goals

| Goal ID | Goal | Owner |
|---------|------|-------|
| G1      | (fill) | researcher |

## Milestones

| Milestone ID | Goal | Description | Success criteria (falsifiable) | Dependencies |
|--------------|------|-------------|--------------------------------|--------------|
| M-1          | G1   | (fill)      | (fill)                          | —            |

## Out of scope (explicit)

- (fill if relevant)

## Pointer to ledger

Every milestone status, history, and judgment lives in `promise_ledger.jsonl`,
filtered by `milestone_id`. Run `promise_check` to materialize the current
state for the human; agents call it via Bash:

    python3 -m long_exposure.tools.promise_check .

The directive section above is **immutable** after creation. Goals and
milestones tables are mutable, but every edit must emit a ledger event with
`milestone_id: "_plan/<descriptive-change-name>"` so the audit trail is
complete.
