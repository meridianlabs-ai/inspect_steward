# Error Handling – Inspect Steward

## Overview

Inspect’s default behavior is to fail fast so that you can see and fix a problem. Steward runs unattended for hours at a time, so it does the opposite: errors never halt a run, retries are automatic, and anything a retry cannot fix becomes a decision that waits in a queue.

Nothing is dropped silently. Every error is either retried or turned into a decision, and a run cannot be signed off while a decision is unanswered.

| Stage | What happens | Who acts |
|----|----|----|
| [Retry](#retry) | Samples are retried in the worker, and failed tasks are respawned | Steward, unattended |
| [Surface](#surface) | What survives retry is grouped into a class and raised as a decision | Steward and your agent |
| [Decide](#decide) | A ruling: a disposition, a reason, and a name | you |
| [Codify](#codify) | Standing rules so the same question stops being asked | your agent, in `_steward.yaml` |
| [Resolve](#resolve) | Signoff refuses while anything is still undecided | you |

Transcript scanning follows the same stages, because a finding becomes a decision the same way an error does. See [Scanners](./scanners.html.md).

## Retry

Steward runs workers with `fail_on_error=False`, so a sample that errors never fails its task. Every task runs to completion and its errored samples are collected in the log. Steward decides what to do about them by reading the logs, so a task that stopped on its first error would look like a structural failure and be re-launched.

This means that completion is not success. A log can finish `success` while carrying errored samples, so a task whose provider was down for its entire duration still completes.

Retries then happen at two levels:

- Samples are retried inside the worker (by default up to 3 times; set `retry_on_error` in your [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call to choose your own number, including 0).

- Tasks are retried by Steward. Once `stall_after` attempts get nowhere (default `2`) the task is marked stalled and becomes a decision.

## Surface

Samples that failed the same way are grouped into a class, so a provider outage is one question instead of two hundred. A stalled task is classed the same way, on the exception in its log or on the worker’s output if it died before writing one, so it arrives with its evidence attached.

Classes appear under the anomalies heading in `steward status` and `status.md`, with instance counts, an example message, and any prior rulings. A class is raised once, and again only if it materially changes: the population crosses an order of magnitude, or a re-run fails.

Your agent works the queue from here. `steward investigate` marks a class as being worked, and `steward propose` bundles related classes into one question with the evidence attached.

## Decide

Agents will prompt you for a ruling with one of six answers:

| Disposition | What it means | What happens to the data |
|----|----|----|
| `rerun` | The failure was transient and the samples should run again | Steward re-runs them and records whether they passed |
| `exclude` | Leave the samples out of scoring | The report says how many were excluded |
| `zero` | Count the samples as failures | Scored zero, with a visible mark |
| `score` | The recorded results stand as they are | Scored as-is, marked |
| `accept` | The data stands with a caveat | Requires a stated effect, which is the sentence the report carries. Not available for errored samples |
| `dismiss` | Looked, nothing here | No mark; the record keeps who looked and why |

The next tend carries out a `rerun`. Samples in a running task are requeued in place, and a landed log has the ruled samples invalidated so the task re-runs them and reuses everything else. Authorized re-runs are scheduled ahead of ordinary queued work.

If the same failure comes back after a ruling, it opens as a new question and carries the old decision with it.

## Codify

“When you find yourself answering the same question twice, your agent will propose recording it in \_steward.yaml so it stops being asked. For example:

    _steward.yaml

``` yaml
preauthorized:
  'error:ReadTimeout@*': rerun
```

When a new class matches a pattern, the tend records a ruling with `by: policy` naming the pattern and applies it in the same turn. Nothing is pre-authorized by default, and if a pre-authorized re-run fails, the pattern stops matching that class until a person has looked.

Anything more nuanced than a pattern goes in `policies:` as prose, which your agent applies when one is in session:

    _steward.yaml

``` yaml
policies:
  - If more than 10% of the samples in a task error out, pause the run and
    notify me right away.
  - Sandbox startup timeouts under 1% are expected here; invalidate and re-run.
```

Use a policy to stop a run on error rates. Pausing is reversible and stops the whole run, which is usually better than failing one task.

## Resolve

`steward signoff` refuses while any error is still undecided, and lists every reason at once so that fixing one and being refused for the next never happens.

The refusal tells you what to answer. It is not a quality bar: `accept` and `dismiss` are answers, and “2 samples accepted as errored, the provider was down all night” is a signed statement about the results.

The classes you accepted become the entries in `anomalies.md`, each with its reason, who decided, when, and the effect on the numbers.

## Stuck Samples

One condition never becomes an error: a sample that is alive but not moving, such as a `bash` command that never returns. Nothing errors, nothing scores, and the task’s clock keeps running.

A running sample with no activity for longer than `stuck_after` (default `5h`) is reported, one item per task, naming the samples and the tool call they are inside. Streaming counts as activity, so a slow but healthy `generate` is never stuck. A sample parked on an approval is waiting on you, and a provider backoff with a live deadline is waiting on purpose.

`stuck_after` only reports, and never cancels. To bound how long a sample may work, set `working_limit` in your definition.

What to do about a stuck sample is an escalation ladder. The item carries the applicable rung as a command ready to run:

| Rung | Command | What it costs |
|----|----|----|
| Cancel the tool call | `inspect ctl sample cancel-tool-call TASK ID EPOCH` | The call fails inside the sample, which continues |
| Cancel the sample | `inspect ctl sample cancel TASK ID EPOCH` | The sample stops, recorded as you choose with `--action` |
| Requeue the sample | `inspect ctl sample requeue TASK ID EPOCH` | The sample’s work is discarded and it runs again from the start |

A cancel is a request. If one was asked for and the call still has not stopped, the item says so, which is your signal to climb a rung.

You can grant the first rung in advance. `stuck_cancel: true` lets your agent cancel any stuck tool call itself, and `stuck_cancel: [bash]` admits only those tools. The higher rungs stay yours, since they discard work.

A stuck sample is not an anomaly and there is nothing to rule on. It is reported while it holds and gone when the sample moves.
