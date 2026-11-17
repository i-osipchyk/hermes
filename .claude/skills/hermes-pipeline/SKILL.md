---
name: hermes-pipeline
description: Drive the research pipeline for an idea — see the queue, run an idea to a verdict, read and answer the owner approvals the agents parked. Use when the user asks what is in the pipeline, wants an idea taken forward, asks what is waiting on them, or wants to answer an approval.
---

# Hermes: the pipeline front-end

The pipeline moves an idea from `raw` to a verdict by itself, one role at a time (Researcher,
Quant, Validator, Developer), and **parks** anything only the owner may decide. You are the thin
front-end: you read state and call the CLI. **Never edit `research/ideas.json`, an artifact or a
status by hand** — transitions are guarded in the library (ADR-0017) and the driver (ADR-0018)
verifies what each role wrote.

```bash
.venv/bin/hermes pipeline status                 # the queue, approvals waiting, each idea's next step
.venv/bin/hermes pipeline run <id> --dry-run     # what would happen; runs no agent, changes nothing
.venv/bin/hermes pipeline run <id>               # drive one idea until it parks, stalls or finishes
.venv/bin/hermes pipeline step <id>              # exactly one step (debugging)
.venv/bin/hermes pipeline context <id>           # everything one iteration needs about an idea
.venv/bin/hermes pipeline approvals --pending    # the owner's decisions, with question + evidence
.venv/bin/hermes pipeline answer <approval> approve|deny --note "why"   # owner only
```

## What `run` tells you

It stops, never waits. Exit `0`: finished, parked on an approval (it names it), reached `--until`
or `--max-steps` (run again to resume), or `owner` (a validated idea waits for the forward-test
decision). Exit `2`: **stalled** — a role failed verification three times and the idea parked on an
`other` approval; read `research/ideas/<id>/run.jsonl` and the artifact it names. Exit `3`:
**paused** on the subscription limit; run again later, nothing is repeated.

## Answering approvals

Read the question, the recommendation and the evidence; `answer` it. An `advisor` approval carries a
computed cost range (low/high) and cannot be approved if the model is unpriced. A `dev_merge` or
`engine_semantics` approval merges the Developer's branch **locally** when you approve (both gates
are re-run first; a failure refuses the merge) — read the diff for `engine_semantics`. Denying an
advisor or forward-test approval sends the idea back where it was; `--reject` rejects it.

## Say it correctly

A surviving idea is **ready to forward test**, never "deployable" — Hermes cannot forward test.
Quote the evidence tier. An axis with `ran: false` was not measured: say so and name the command.
