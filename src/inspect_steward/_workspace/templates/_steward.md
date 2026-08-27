---
# Settings Steward acts on by itself, at 3am, with nobody watching.
#
# Only what affects *Steward* belongs up here. Anything that affects the eval —
# log_dir, model, max_samples, epochs, limits — is refused by name, because
# your definition is the single source of truth for those.
#
# Uncomment what you want. Anything left commented out uses Steward's default.

# max_tasks: 24       # never run more than this many tasks at once, fleet-wide
# max_workers: 8      # pack those tasks into this many worker processes
---

# _steward.md

Your standing rules for this project, in two halves. Above the `---` is what
Steward can execute unattended; below it is what an agent reads and applies
when one is in session. They live in one file so that a decision and its
reasoning stay next to each other.

**Steward never writes this file.** It proposes changes and you decide, because
promoting a one-off ruling into a standing rule is a judgement that widens what
you have committed to.

An empty file is a valid one: everything then escalates to you. Rules
accumulate as you notice yourself answering the same question twice, and the
run gets quieter as they do.

## What is expected here

Failures you already know about, so the agent does not wake you for them.

<!-- e.g. "Sandbox startup times out on roughly 1% of samples in this eval;
     that is the environment, not the model. Invalidate and re-run." -->

## What always reaches me

<!-- e.g. "Any anomaly class over 50 instances."
     e.g. "Anything touching the grader — I want to see grader failures even
     if they look transient." -->

## Pre-authorised

Rulings granted in advance. An agent acting on one of these is executing a
decision you have already made, not making its own.

<!-- e.g. "Re-run samples that failed on provider rate limits, up to twice,
     without asking." -->

## Concurrency envelope

The hard ceiling is `max_tasks` in the front matter above — Steward enforces
that one itself, and unset it means every task runs at once. This section is for
the room an agent may move in beneath it, which takes judgement rather than a
number.

<!-- e.g. "max_samples up to 40 per task, and back off on the first sign of
     provider throttling rather than riding the limit." -->

## Notes for whoever reads this next

<!-- What this eval set is for, and anything a third party picking up the
     directory cold would need in order to judge the results. -->
