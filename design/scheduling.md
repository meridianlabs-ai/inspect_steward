# Scheduling

**Status: in progress. The process model, the worker pool, and failure handling are settled; ordering and the concurrency allocation are not.**

[execution.md](execution.md) establishes that the architecture is a pure function — `reconcile(manifest, inflight, log_dir) -> (actions, summary)` — and argues at length for its *properties*: testable, idempotent, driver-independent. It does not say what the function decides. This document is that half.

## The process model

### One task, one process

A worker runs exactly one task. Not "one by default" — always.

**The binding argument is the GIL, and it is easy to wave away incorrectly.** Evals are usually described as I/O-bound, and they mostly are: a sample waiting on a model API costs nothing but a slot. But the work that is *not* waiting is all Python bytecode — transcript construction for long agentic runs, JSON serialization, `.eval` zip compression on write, non-model scorers, sandbox subprocess management — and that work is serialized within a process however high `max_samples` goes. **One process saturates one core.** Raising sample concurrency past that point buys queueing, not throughput.

Two further properties come free and are worth naming because each was argued for separately elsewhere:

- **Failure isolation.** One task per process means one crash costs one task. [hawk.md](hawk.md) makes this the headline benefit of Steward over a single-runner-pod model, where an OOM restarts the world.
- **Scheduling granularity.** The unit Steward spawns, reaps, retunes, and adjudicates is the unit the manifest enumerates. Nothing has to be mapped onto anything.

This overturns a trade-off recorded in [workflow.md](workflow.md), which weighed "fewer, larger workers pay less per-worker startup cost" against finer granularity. That comparison was measuring the wrong quantities: startup cost is seconds, paid once, while the CPU ceiling is permanent. The trade was never as close as it looked.

### No batching

The selection document accepts a *list* of tasks so that one worker can host several — added for definitions whose import cost dominates, and for batches of very short tasks. Steward always writes exactly one entry, and the generality goes unused.

It is not merely unnecessary but unmotivated. Batching existed to solve **slot idle**: with a capped pool and a ten-minute tend interval, a worker finishing at t=0 leaves its slot empty until the next tend, which hurts most when tasks are short. Launching every task at once removes slots as a concept, so the problem batching solved does not arise. The constraint that came with it — *batch only tasks sharing a model, or the process's initial limit is wrong for some of them* — disappears at the same time.

What remains is the startup cost itself, which batching would have amortized: Flow's measured ~1.1s of pre-boundary work, and Hawk's far worse `uv pip install` per invocation. Those are real, and they are now squarely the frontends' problem rather than something Steward can paper over by running fewer processes — which is the honest place for them, and the reason the upstream ask in [execution.md](execution.md) open question 1 matters more than it did.

### No sharding

The mirror image of batching is splitting one large task's samples across processes, for which the selection schema reserved a per-task `samples` field. **Steward will not do this.** A task always runs all of its samples in one process, and controls its own memory through `max_samples` from the inside.

The cost is accepted rather than hidden: a single very large task is bounded by one core, and no amount of hardware helps it. What buying that back would cost is a second granularity mechanism running alongside the first — multiple logs per task, a completeness predicate that reassembles shards, and adjudication that has to know which shard a sample came from. One unit of work, one process, one log is worth more than the parallelism.

This closes [configuration.md](configuration.md) open question 6.

## The worker pool

### Total concurrency is one budget spent twice

Worker count and per-worker `max_samples` are not independent settings:

```
total concurrent samples  =  workers × max_samples
```

Steward owns both factors, which is what makes the fleet's load on a provider deterministic rather than emergent ([workflow.md](workflow.md), *What Steward actually has to solve*). The two spend differently, though, and the asymmetry decides how to split them:

```
local compute  ≈  (workers × process cost)  +  (concurrent samples × sample cost)
throughput     ≈  concurrent samples on that model        [per rate-limit bucket]
```

A process costs an interpreter and its imports, once. A concurrent sample costs a sandbox container, memory, and a slot, continuously. **Process count is the cheap axis; sample concurrency is the expensive one** — which is what makes launching many processes affordable in the first place.

### Launch everything, up to a ceiling

Every pending task gets a worker immediately, bounded only by a ceiling. There is no queue in the common case, because a typical sweep has fewer tasks than the ceiling allows.

The ceiling is **`min(cores, pending tasks)`**, overridable by the user. It follows directly from the process model: past core count, another process stops buying parallelism, because there is no core for it to run on. The queue exists for what is left over, and is frequently empty.

`eval_set()` carries the precedent for having a ceiling at all — `max_tasks` defaults to `max(len(models), 10)` — but its number does not transfer, because its tasks are coroutines sharing one interpreter and Steward's are processes:

| | `eval_set` `max_tasks` | Steward's ceiling |
|---|---|---|
| CPU | N tasks share one core | N tasks get N cores |
| memory | one address space | N interpreters |

Note what is deliberately *not* inherited. That default's second clause — at least one slot per model — gives `eval_set()` a stratification guarantee for free: no arm can starve while another runs. Plain `min(cores, pending)` does not, so if that property is wanted here it has to come from task ordering instead. Recorded as an open question below rather than smuggled into the ceiling.

### Cores means the cgroup's cores

`os.cpu_count()` reports the host's processors, not the container's quota. A Kubernetes pod limited to 2 CPUs on a 64-core node reports 64 — so a cores-derived ceiling over-spawns by 32× in precisely the deployment where over-spawning kills the pod ([hawk.md](hawk.md)). Steward must read the cgroup CPU quota where one exists and fall back to `os.cpu_count()` otherwise. Hawk's own `memory_monitor` already polls the cgroup, so the precedent and the mechanism are both to hand.

### Memory is assumed adequate

There is deliberately no memory term in the ceiling. Two reasons, and the second is the substantive one.

Nobody has measured per-worker memory, so any coefficient would be invented. More importantly, **the quantity it would describe is about to change shape.** Until Layer 2 pruning lands, every worker constructs every task in the eval set, datasets included — so per-worker memory scales with the whole manifest and grows each time someone adds a task. Once pruning lands it becomes roughly constant per worker. A memory formula written today would encode the un-pruned world and be wrong on the day the thing it was compensating for is fixed.

So: assume memory is fine, and treat **Layer 2 pruning as a prerequisite for launch-all at scale** rather than as the optimization it is currently filed as ([execution.md](execution.md), *Changes required*, item 5). Verified absent from `inspect_ai` main as of `0db4111e`. If large sweeps turn out to exhaust memory before it lands, the right response is a launch-time check against a measured figure — a guard, not a term in the default, so that it can be removed cleanly.

## Failure is adjudicated, not retried

### What can still fail a task

`fail_on_error=False` absorbs sample errors: a task carrying five hundred errored samples reaches the end of its dataset and finishes `status="success"`. So a task that *fails* has failed for a reason categorically outside the sample loop, and the list is short:

| observed | cause | recurs? |
|---|---|---|
| no log, clean non-zero exit | import error, task construction, bad model name | **always** |
| no log, killed by signal | OOM, host pressure | usually, unless the fleet changes |
| log, `status="error"` | dataset load, sandbox provisioning, a scorer or metric throwing | mixed — scorer bugs always, provisioning sometimes |
| log, process vanished | OOM mid-run, host loss | sometimes |

**Most of that is deterministic.** Automatically respawning a definition that will not import, or a scorer that throws after every sample has already run, spends money to arrive at exactly where it was — and does it at 3am with nobody watching. The rarity is the point: because sample errors no longer reach this level, a task failure is now an unusual and mostly-structural event, and structural events deserve a look rather than a counter.

### No automatic restart

**A task failure opens an anomaly. Steward never respawns a failed task on its own.**

The objection to this is obvious and turns out not to hold: a host reboots at 3am, forty workers die, and now forty approvals stand between the run and progress. But anomalies are *classed*, not itemized ([workflow.md](workflow.md), *Rule on classes, not instances*) — forty tasks killed by one reboot are **one anomaly with forty instances, and one ruling restarts all of them**. The cost of always asking is one question per cause, which is a rate a person can sustain.

So the attempt ceiling this document set out to choose is **zero**, and there is no number to tune. What bounds recurrence is not a counter but the record: every restart required a ruling, rulings accumulate as precedent, and a class that keeps returning is visible as such rather than hidden inside a retry count that resets. That is the mechanism the design already has for this, and it is strictly more informative than the one it replaces.

This also removes a mechanism rather than adding one. [execution.md](execution.md) treats whole-task retry as a fourth thing standing beside the three recovery tiers; it is really the third tier again — post-completion adjudication, with respawn-and-`resume` as the action a ruling authorizes.

### The failure signature classes the anomaly

The forensics still matter, but for proposing rather than deciding. Two bits are free — whether a log exists, and whether the process exited cleanly or died on a signal — and together they separate causes that want genuinely different responses:

| signature | the proposal it supports |
|---|---|
| no log, traceback | "this fails identically every time — here is the error; the definition needs fixing" |
| no log, `SIGKILL`, several tasks at once | "resource exhaustion; reduce the pool and resume" |
| `status="error"` after scoring | "every sample completed and the scorer threw; fix it and re-score rather than re-running" |

Getting this wrong in the other direction was a real risk. A plain *did it write a log?* test collapses the first two rows — and the case it mistakes is the one launch-all makes most likely, since until Layer 2 pruning lands the probable failure of a large sweep is OOM during startup: no log, and read as permanent. Steward would decline to retry precisely the failure a smaller pool would fix.

Note this is process forensics, not error classification. It reads how a worker died, never what its samples did, so it carries no dependency on the sample-error taxonomy ([execution.md](execution.md), open question 7).

### The cost, and where it is paid

A genuinely transient failure — a sandbox provisioning blip, a Docker daemon hiccup — now waits for a human instead of recovering itself. That is the wasted night this design cares most about avoiding, and it is a real cost rather than a theoretical one.

The mitigation is one the design already has: **`policy.md` pre-authorization.** *"Resume tasks killed by host loss or sandbox provisioning without asking"* is a standing rule granted once, and it converts a night's worth of potential 3am questions into standing authority — the *authorize at 10pm, do not interrogate at 3am* argument, applied to failure rather than to scaling. It also gives the launch-time exchange a concrete item it was missing.

The framing that matters: **approval is the default, and automatic restart is something a human grants for named classes** — rather than something Steward assumes and then bounds with a counter it hopes is large enough.

### Approved re-runs go first

Queue order between a fresh task and an adjudication re-run only bites when pending work exceeds the ceiling. When it does, **re-runs go first.**

Three reasons, and the first is now decisive:

- **Somebody asked for it.** A re-run exists only because a ruling authorized it, which is a stronger claim on a slot than any task that is merely next in the manifest.
- **They are cheap.** A respawn with `resume` reuses every completed, non-errored, non-invalidated sample, so re-running a five-hundred-sample task with forty-seven errors runs forty-seven samples. Re-runs and fresh tasks are not comparable units competing for a slot.
- **They front-load decidability.** A re-run that closes at 11pm produces a question someone can answer; the same work finishing at 6am produces one that stalls until morning. Prioritizing re-runs moves *the moment anomalies become resolvable* earlier in the night.

The starvation risk that would normally argue for capping this at a fraction of the pool does not arise, because nothing can loop without a ruling in between.

**Priority means queue order, never preemption.** A re-run does not displace a running worker, whatever its urgency.

## Open questions

1. **Ordering of the queued remainder.** Only bites when pending exceeds the ceiling. Manifest order, longest-first (minimizes makespan), shortest-first (lands results early), or stratified across arms (keeps an interrupted run comparable). The last is the one with a scientific rather than an operational argument behind it, and it is also what would recover the stratification property the ceiling declines to provide.

2. **Initial `max_samples` per worker.** The manifest carries model, sample count, and epochs, which is enough for a first allocation, and explicit `max_samples` yields a `ResizableLimiter` that Steward can retune live — so the initial value matters less than the ramp policy. How the per-provider throughput budget divides among the workers sharing it remains [workflow.md](workflow.md) open question 8.

3. **Scan drain cadence.** A scan pass is a detached child a tend spawns and reaps, which makes its cadence a scheduling decision: how much to let accumulate, how one-at-a-time is enforced, and how a crashed pass appears in the in-flight accounting. Covers the first and third bullets of [execution.md](execution.md) open question 9.

4. **Rebalancing on completion.** When a worker exits, its share of the throughput budget frees. Redistributing it to live workers means retuning through the control channel on every tend, and the ratchet is asymmetric — raising a limit takes effect at once, lowering one only as in-flight samples drain.
