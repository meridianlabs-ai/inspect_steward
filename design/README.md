# Design

Nine documents. **Start with [roadmap.md](roadmap.md)** for what exists and what is being built, then [plan.md](plan.md) for the order it gets built in; the rest say why. The first three read in order, each starting where the previous ends; scheduling gives execution's reconcile loop its policy, agent describes the only thing that operates any of it, testing says how the whole thing is held to its claims, and hawk is one platform's integration and depends on all of them.

| | covers | status |
|---|---|---|
| [roadmap.md](roadmap.md) | **What is built, what is next, and what is deliberately not being built.** Four milestones, the deferral list with reasons, and when each upstream item bites. | The cut and sequence settled |
| [plan.md](plan.md) | **The order of construction.** Thirty-two independently designable and testable steps, each citing the sections its design pass starts from, with the milestone gates located precisely and Hawk's in-pod stage after ship. | Sequence and gates settled; step internals are per-step work |
| [configuration.md](configuration.md) | How a *definition* — any program culminating in one `eval_set()` call — becomes a **manifest** of resolved tasks, via capture mode. | **Implemented** |
| [execution.md](execution.md) | How a manifest becomes **running processes**: worker mode, the shared log directory, recovery, and the reconcile loop. | Protocol landed upstream; runner unbuilt |
| [scheduling.md](scheduling.md) | What `reconcile` actually **decides**: one task per process, launching everything up to a core-count ceiling, spawning task-major, how the three concurrency knobs are set, when scan passes run, and why a failed task is adjudicated rather than retried. | Settled |
| [workflow.md](workflow.md) | What a person actually **does** — `init` through `signoff` — the project, convergence, the anomaly lifecycle, notification, and what a run leaves behind. | Settled but for two narrow questions |
| [agent.md](agent.md) | The **coding agent** as a system component: its four judgement responsibilities, its three possible postures over a timer-guaranteed run, cold pickup, what it may do unasked, and what it must never do. | Responsibilities, postures, and bounds settled; tend schema open |
| [testing.md](testing.md) | How the design's **recovery claims** are held to account: four layers from a pure-function table to agent scenarios, and fault injection as the primary mode rather than a hardening pass. | Layering and faults settled; agent tier sketched |
| [hawk.md](hawk.md) | Running under **Hawk**: its config as a definition, its infra config at runtime, a blocking `launch`, and the relay an external agent drives it through. | Stage 0 implemented (configs read and run); runtime stages sketched |

## 1. The shape in one paragraph

A definition is executed once under `INSPECT_EVAL_SET_CAPTURE` to enumerate its tasks. Steward then spawns one worker per task, each re-executing the definition under `INSPECT_EVAL_SET_SELECTION` so that side effects (registered models, `set_model_info`, dynamically built `Model` objects) are present in every process, but eval-set orchestration is skipped — Steward owns scheduling, retries, the log directory's metadata, and completion. A timer calls `steward tend` every ten minutes or so; each tend reconciles on-disk state against the manifest, spawns and reaps, and returns a compact summary. A coding agent reads those summaries — reactively, periodically, or in bulk when it next attaches — and supplies the judgement the mechanical layer refuses to. Anomalies are adjudicated with the human, and the run ends when a person signs off.

## 2. Decisions that shape everything else

- **The definition is the single source of truth** for what an eval set is. No second config file — there is deliberately no `steward.yaml`.
- **A workspace is a project, not a run.** One evolving definition, one log directory holding its current results, one archive holding everything superseded, one journal. Work converges toward the manifest rather than happening in identified episodes, so amending is just `launch` again.
- **Steward never deletes an eval log.** Superseded, removed, and failed logs move to a sibling archive. Curating the directory is allowed; destroying a result is not.
- **Workers run `eval()`, not `eval_set()`.** Removing the competing orchestrator is what makes a flat, shared log directory safe, which is what keeps `inspect view` and `samples_df` working live and unmodified.
- **A timer guarantees the mechanical tend; the agent supplies judgement.** No long-lived supervisor — `reconcile` is a pure function and the claim is held for the seconds a tend takes, so a timer, an agent, or both can call it. That split is what makes an absent agent survivable: the fleet keeps converging and only decisions accumulate. The agent tunes, groups, investigates, and writes ([agent.md](agent.md)), and may be attached, periodic, or merely transient.
- **A tend spawns and reaps; it never does long work itself.** Scans, task workers, and adjudication re-runs are all detached children.
- **`fail_on_error=False`.** Everything runs to the end; anomalies are settled afterwards rather than aborting a run mid-flight. Because that absorbs every sample-shaped failure, a task that fails anyway has failed structurally — so it is adjudicated, never restarted automatically.
- **One automatic tier, then adjudication.** A sample gets the `retry_on_error` attempts its definition asked for; every further attempt, at sample or task level, is a ruling. `policy.md` may grant that ruling in advance, but nothing is pre-authorized by default, and Steward has **no** automatic response to an error class.
- **Mechanics ship with the package (`steward runbook`); policy lives with the project (`policy.md`).** Steward proposes changes to policy and never writes it.
- **The workspace syncs outward on every tend.** Runs happen on machines with no git and sometimes no internet, where an S3 bucket is the only observability channel. The sync is exclusionary (everything top-level but dotfiles, directories, and agent bootstrap) and never raises.
- **A platform is a definition type, not a second architecture.** Flow's CLI and Hawk's runner are both programs culminating in one `eval_set()` call, so Steward runs *their* entrypoints and intercepts at the boundary rather than re-deriving what they do.

## 3. Where the open questions live

Each document ends with its own numbered list, and [roadmap.md](roadmap.md) says which of them block anything (very few do). Roughly: configuration.md holds definition-boundary questions, execution.md holds process, recovery, and scanning questions, workflow.md holds product and adjudication questions, agent.md holds ones about the driver and its cadence, testing.md holds ones about the harness itself, and hawk.md holds the ones that only arise inside someone else's platform. Answers cross documents in three places — execution.md's *what "resolved" means* is answered in workflow.md, workflow.md's smoke run depends on the selection overrides described in execution.md, and configuration.md's deferred Hawk support is answered in hawk.md.

## 4. Citing a section

Every heading below `#` is numbered — `## 4.` for a section, `### 4.2` for a subsection — so a development plan can cite **`execution.md §4.2`** and mean one thing. Numbers follow document order, so inserting a section renumbers what follows it: cite the number *and* the title when the reference has to survive an edit.

## 5. Upstream dependencies

execution.md's *Changes required in inspect_ai* is the single list of what Steward needs from Inspect. Items 1–4 have landed (capture, selection, error-handling overrides, operational overrides). Outstanding: early pruning, public directory operations, notification-outside-an-eval, the dataset `limit` override a smoke run needs, a `max_sandboxes` override, and a capture manifest that records each task's sandbox type. [roadmap.md](roadmap.md) orders these by when they bite — **only notification is on the critical path, and it has a workaround.**
