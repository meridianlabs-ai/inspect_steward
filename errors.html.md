# Error Handling – Inspect Steward

## Overview

Errors under Steward come in two tiers, and they are handled by different mechanisms with different owners:

| Tier | What happens | Who decides what’s next |
|----|----|----|
| **Sample errors** | The task keeps running; errored samples are collected in the log | You (or the agent, within your policy) |
| **Task failures** | The task is re-launched, up to `stall_after` attempts | Steward, then you once the limit is hit |

## Sample errors

A sample that errors never halts its task: Steward runs every task to completion (`continue_on_fail` is forced on in worker mode, and a definition’s own setting has no effect under Steward). This is an invariant rather than an option because supervision depends on it — Steward reconciles the run by comparing logs against the manifest, and a task that halted on its first error would be indistinguishable from a structural failure, re-launched, and halted again.

The consequence is that **completion is not success**: a log can finish `success` while carrying errored samples, so a task whose provider was down for its entire duration completes “successfully” with a meaningless score. Read the reported status, not the exit code.

Two things govern what happens to errored samples:

- **Automatic retry** belongs to your definition: set `retry_on_error` in your [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call and it is honored exactly as written; when a definition says nothing, Steward workers default to 3 attempts per sample. Retrying samples release their concurrency slot and re-enter at the back of the queue, so retries never block healthy samples — they just tend to finish late in the task.
- **Everything past automatic retry is a decision**: re-run the samples, accept them as failed, or exclude them from scoring. The agent groups errored samples into classes (a provider outage is one question, not two hundred) and either acts within your standing policy or asks you.

To stop a run when error rates climb, write it as a policy rule — e.g. *“if more than 10% of the samples in a task error out, pause the run and notify me”* — which pauses the whole run reversibly rather than failing one task.

## Task failures

A task that fails outright — an import error, an OOM, a scorer that throws, a worker process that dies — is re-launched by the next turn of the monitor. Attempts are counted, and after `stall_after` of them finish nothing new (default: 2) the task is marked **stalled**: it will not be re-launched again, and it is reported as needing a person.

The limit exists because a structural failure re-runs to the same place at full price — a definition that will not import fails identically on attempt ten. Steward never retries past the limit on its own; raising `stall_after` in `_steward.yaml` is how you buy more patience for a genuinely flaky environment (a sandbox fleet earns more of it than a deterministic scorer bug).

When a person rules on a stalled task — fixing the definition and re-launching, or invalidating samples for a re-run — the attempt history before that ruling is cleared: a decision to try again is made by the only party entitled to make one, and what happened before it no longer counts against the task.

## Where errors surface

Sample error counts and stalled tasks both appear in `steward status` and in the questions the agent brings you, each raised once rather than repeated every interval. Decisions and their reasons are recorded in the journal as they happen, so how the run’s errors were handled is part of the run’s record.
