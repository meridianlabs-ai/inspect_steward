# Design

Three documents, read in this order. Each starts where the previous one ends.

| | covers | status |
|---|---|---|
| [configuration.md](configuration.md) | How a *definition* — any program culminating in one `eval_set()` call — becomes a **manifest** of resolved tasks, via capture mode. | Implemented |
| [execution.md](execution.md) | How a manifest becomes **running processes**: worker mode, the shared log directory, recovery, and the reconcile loop. | Protocol landed upstream; runner unbuilt |
| [workflow.md](workflow.md) | What a person actually **does** — `init` through `signoff` — the workspace, adjudication, notification, and resource tuning. | Sketch, actively being figured out |

## The shape in one paragraph

A definition is executed once under `INSPECT_EVAL_SET_CAPTURE` to enumerate its tasks. Steward then spawns one worker per task, each re-executing the definition under `INSPECT_EVAL_SET_SELECTION` so that side effects (registered models, `set_model_info`, dynamically built `Model` objects) are present in every process, but eval-set orchestration is skipped — Steward owns scheduling, retries, the log directory's metadata, and completion. A coding agent drives the loop by calling `steward tend` every ten minutes or so; each tend reconciles on-disk state against the manifest, spawns and reaps, and returns a compact summary. Anomalies are adjudicated with the human, and the run ends when a person signs off.

## Decisions that shape everything else

- **The definition is the single source of truth** for what an eval set is. No second config file — there is deliberately no `steward.yaml`.
- **Workers run `eval()`, not `eval_set()`.** Removing the competing orchestrator is what makes a flat, shared log directory safe, which is what keeps `inspect view` and `samples_df` working live and unmodified.
- **The coding agent is the only driver.** No daemon: the reconcile core is a pure function, the run claim is short-lived, and a missed tend pauses a run rather than breaking it.
- **A tend spawns and reaps; it never does long work itself.** Scans, task workers, and adjudication re-runs are all detached children.
- **`fail_on_error=False`.** Everything runs to the end; anomalies are settled afterwards rather than aborting a run mid-flight.
- **Mechanics ship with the package (`steward runbook`); policy lives with the project (`policy.md`).** Steward proposes changes to policy and never writes it.

## Where the open questions live

Each document ends with its own numbered list. Roughly: configuration.md holds definition-boundary questions, execution.md holds process, recovery, and scanning questions, and workflow.md holds product, adjudication, and resource-allocation questions. Two answers live across documents — execution.md's *what "resolved" means* is answered in workflow.md, and workflow.md's resource levers depend on execution.md's overrides channel.

## Upstream dependencies

execution.md's *Changes required in inspect_ai* is the single list of what Steward needs from Inspect. Items 1–3 have landed; the overrides channel, early pruning, public directory operations, and notification-outside-an-eval have not.
