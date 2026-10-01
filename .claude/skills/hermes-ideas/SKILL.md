---
name: hermes-ideas
description: Generate, capture and triage trading ideas into the backlog with their provenance — step 1 of the research framework. Use when the user wants ideas for strategies, has read something they want to test, describes a discretionary observation, asks what to research next, or wants to see the idea pipeline.
---

# Hermes: idea generation (step 1)

"Idea generation is the most crucial stage for any quant" — and it is the stage with the
least infrastructure, because ideas arrive from a book or a session and then live in
someone's head until they are forgotten. The backlog (`research/ideas.json`) exists so
they survive, and so the *source* of your wins is measurable.

```bash
.venv/bin/hermes ideas                                  # the pipeline
.venv/bin/hermes ideas --open                           # what still needs work
.venv/bin/hermes ideas --hit-rate                       # which sources pay off
.venv/bin/hermes ideas add "<title>" --source book --detail "<which book>" \
    --hypothesis "<the claim, in Hermes vocabulary>"
.venv/bin/hermes ideas set <id> --status quantified
```

Statuses move forward only on evidence: `raw` → `quantified` → `testing` → `validated`
or `rejected`. `parked` is for ideas blocked on something external (data Hermes cannot
reach, an instrument with no source) — **not** for ideas you have gone cold on. The
distinction matters: parked is a to-do, rejected is a result.

## Where ideas come from

Record `--source` honestly; it is what makes `--hit-rate` mean anything later.

- **`book`** — strategies from trading literature. Test every one yourself: published
  performance is usually optimistic, sometimes because the edge decayed after
  publication, often because the backtest was generous. Treat the book as the *idea*
  and your own run as the *evidence*. The win is rarely the whole strategy; it is
  usually one mechanism you can lift into something else.
- **`discretionary`** — things you noticed trading manually, on a simulator or small
  size. The highest-quality source and the slowest: it takes weeks of screen time, and
  what comes out is a hunch that still needs quantifying. Record it the day you notice
  it, with what you actually saw.
- **`trader`** / **`course`** — other people's frameworks. Expect to lift snippets, not
  whole systems; a cookie-cutter copy rarely survives contact with your own costs,
  instruments and sizing. Note *whose* idea it was.
- **`data`** — something you found by looking at the data (`hermes-explore-data`). The
  most dangerous source, because you found it by searching: the same search that found
  it will find noise, so it needs the most out-of-sample evidence before you believe it.

## What makes a captured idea useful

A title alone is a sticky note. Capture, at minimum:

- **the claim** — what precedes what, on which instrument, over what horizon;
- **the mechanism** — *why* this should be true. An idea with no mechanism cannot be
  told apart from a pattern you found by looking, and it gives you no way to judge which
  variants are faithful to it. "Momentum persists because institutions fill large orders
  over days" survives a bad backtest with its reasoning intact; "the 13-day EMA works"
  does not.
- **the provenance** — source plus detail.

Do not quantify at capture time. Capture is cheap and should stay cheap; quantification
(`hermes-strategy` §2) is where the real work starts, and doing it too early filters out
ideas before you have enough of them.

## Generating, not just recording

When the user asks for ideas rather than offering one, generate from **mechanism**, not
from indicator combinations. Ask what inefficiency could exist on their instrument and
horizon — who is forced to trade, when, and why; what is mispriced around a scheduled
event; which participants have a structural constraint — and then propose ideas that
exploit it. Twenty variations on a moving-average cross are one idea, and the correlation
matrix (`hermes-portfolio`) will say so.

Reach for **`hermes-explore-data`** when the question is what the instrument actually
does before designing for it.

## Hand off

Capture, then route: an idea worth working on goes to **`hermes-research`**, which pins
the hypothesis, quantifies it, and runs the loop. Set `--status testing` and record the
run keys with `--run-key` as they come in, so the idea's verdict is traceable to the
evidence that produced it.

Completion criterion: every idea discussed is either in the backlog with its provenance
and mechanism, or explicitly declined with a reason.
