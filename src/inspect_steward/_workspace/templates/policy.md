# policy.md

Your standing rules for this project. **Steward never writes this file** — it
proposes changes and you decide, because promoting a one-off ruling into a
standing rule is a judgement that widens what you have committed to.

An empty policy is a valid policy: everything then escalates to you. Rules
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

The bounds the agent may tune inside without asking.

<!-- e.g. "max_samples up to 40 per worker. Do not exceed 8 workers on this
     host." -->

## Notes for whoever reads this next

<!-- What this eval set is for, and anything a third party picking up the
     directory cold would need in order to judge the results. -->
