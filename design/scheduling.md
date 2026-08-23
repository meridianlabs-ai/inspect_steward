# Scheduling

**Status: settled. The process model, the worker pool, spawn order, all three concurrency knobs, scan cadence, and failure handling are decided; spend is declined outright.**

[execution.md](execution.md) establishes that the architecture is a pure function — `reconcile(manifest, inflight, log_dir) -> (actions, summary)` — and argues at length for its *properties*: testable, idempotent, driver-independent. It does not say what the function decides. This document is that half.

## 1. The process model

### 1.1 One task, one process

A worker runs exactly one task. Not "one by default" — always.

**The binding argument is the GIL, and it is easy to wave away incorrectly.** Evals are usually described as I/O-bound, and they mostly are: a sample waiting on a model API costs nothing but a slot. But the work that is *not* waiting is all Python bytecode — transcript construction for long agentic runs, JSON serialization, `.eval` zip compression on write, non-model scorers, sandbox subprocess management — and that work is serialized within a process however high `max_samples` goes. **One process saturates one core.** Raising sample concurrency past that point buys queueing, not throughput.

Two further properties come free and are worth naming because each was argued for separately elsewhere:

- **Failure isolation.** One task per process means one crash costs one task. [hawk.md](hawk.md) makes this the headline benefit of Steward over a single-runner-pod model, where an OOM restarts the world.
- **Scheduling granularity.** The unit Steward spawns, reaps, retunes, and adjudicates is the unit the manifest enumerates. Nothing has to be mapped onto anything.

This overturns a trade-off recorded in [workflow.md](workflow.md), which weighed "fewer, larger workers pay less per-worker startup cost" against finer granularity. That comparison was measuring the wrong quantities: startup cost is seconds, paid once, while the CPU ceiling is permanent. The trade was never as close as it looked.

### 1.2 No batching

The selection document accepts a *list* of tasks so that one worker can host several — added for definitions whose import cost dominates, and for batches of very short tasks. Steward always writes exactly one entry, and the generality goes unused.

It is not merely unnecessary but unmotivated. Batching existed to solve **slot idle**: with a capped pool and a ten-minute tend interval, a worker finishing at t=0 leaves its slot empty until the next tend, which hurts most when tasks are short. Launching every task at once removes slots as a concept, so the problem batching solved does not arise. The constraint that came with it — *batch only tasks sharing a model, or the process's initial limit is wrong for some of them* — disappears at the same time.

What remains is the startup cost itself, which batching would have amortized: Flow's measured ~1.1s of pre-boundary work, and Hawk's far worse `uv pip install` per invocation. Those are real, and they are now squarely the frontends' problem rather than something Steward can paper over by running fewer processes — which is the honest place for them, and the reason the upstream ask in [execution.md](execution.md) open question 1 matters more than it did.

### 1.3 No sharding

The mirror image of batching is splitting one large task's samples across processes, for which the selection schema reserved a per-task `samples` field. **Steward will not do this.** A task always runs all of its samples in one process, and controls its own memory through `max_samples` from the inside.

The cost is accepted rather than hidden: a single very large task is bounded by one core, and no amount of hardware helps it. What buying that back would cost is a second granularity mechanism running alongside the first — multiple logs per task, a completeness predicate that reassembles shards, and adjudication that has to know which shard a sample came from. One unit of work, one process, one log is worth more than the parallelism.

This closes [configuration.md](configuration.md) open question 6.

## 2. The worker pool

### 2.1 Total concurrency is one budget spent twice

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

### 2.2 Launch everything, up to a ceiling

Every pending task gets a worker immediately, bounded only by a ceiling. There is no queue in the common case, because a typical sweep has fewer tasks than the ceiling allows.

The ceiling is **`min(cores, pending tasks)`**, overridable by the user. It follows directly from the process model: past core count, another process stops buying parallelism, because there is no core for it to run on. The queue exists for what is left over, and is frequently empty.

`eval_set()` carries the precedent for having a ceiling at all — `max_tasks` defaults to `max(len(models), 10)` — but its number does not transfer, because its tasks are coroutines sharing one interpreter and Steward's are processes:

| | `eval_set` `max_tasks` | Steward's ceiling |
|---|---|---|
| CPU | N tasks share one core | N tasks get N cores |
| memory | one address space | N interpreters |

Note what is deliberately *not* inherited. That default's second clause — at least one slot per model — gives `eval_set()` a stratification guarantee for free: no arm can starve while another runs. Plain `min(cores, pending)` does not. Rather than smuggle the property into the ceiling, it comes from spawn order instead (see below), which delivers the same guarantee without a second knob.

### 2.3 Cores means the cgroup's cores

`os.cpu_count()` reports the host's processors, not the container's quota. A Kubernetes pod limited to 2 CPUs on a 64-core node reports 64 — so a cores-derived ceiling over-spawns by 32× in precisely the deployment where over-spawning kills the pod ([hawk.md](hawk.md)). Steward must read the cgroup CPU quota where one exists and fall back to `os.cpu_count()` otherwise. Hawk's own `memory_monitor` already polls the cgroup, so the precedent and the mechanism are both to hand.

### 2.4 Memory is assumed adequate

There is deliberately no memory term in the ceiling. Two reasons, and the second is the substantive one.

Nobody has measured per-worker memory, so any coefficient would be invented. More importantly, **the quantity it would describe is about to change shape.** Until Layer 2 pruning lands, every worker constructs every task in the eval set, datasets included — so per-worker memory scales with the whole manifest and grows each time someone adds a task. Once pruning lands it becomes roughly constant per worker. A memory formula written today would encode the un-pruned world and be wrong on the day the thing it was compensating for is fixed.

So: assume memory is fine, and treat **Layer 2 pruning as a prerequisite for launch-all at scale** rather than as the optimization it is currently filed as ([execution.md](execution.md), *Changes required*, item 5). Verified absent from `inspect_ai` main as of `0db4111e`. If large sweeps turn out to exhaust memory before it lands, the right response is a launch-time check against a measured figure — a guard, not a term in the default, so that it can be removed cleanly.

### 2.5 Spawn order transposes the crossing

Order only matters when pending work exceeds the ceiling — below it everything runs at once and there is no queue to sequence. When it does matter, the default is actively bad rather than merely arbitrary.

**The manifest arrives model-major.** `eval_resolve_tasks` loops models on the outside and tasks within, so a three-task, two-model sweep enumerates as every task on the first model, then every task on the second (verified against `sweep_evalset.py`). Spawning in that order is the worst available choice, for two independent reasons:

- **An interrupted run is uncomparable.** Stop at the halfway point and one model is complete and the other untouched — the arms cannot be set against each other at all. And interruption is not exotic here: a deadline, `steward stop`, and a projection that says forty hours instead of four are all in this design already.
- **One rate-limit bucket is saturated while the others idle.** Sixteen concurrent tasks on one model hammer a single provider's quota; sixteen spread across five models use five quotas. Model-major ordering is throughput-pessimal for the same reason it is science-pessimal.

The fix is not a reordering, which is what makes it easy to accept: **it is a transposition.** The user wrote a task order and a model order; the model-major nesting is `eval_resolve_tasks`'s choice, not theirs. Steward spawns task-major — preserving both sequences exactly as written, and flipping only the axis nobody expressed an intent about:

```
enumerated   mbpp@gpt5  gsm8k@gpt5  swe@gpt5  mbpp@opus  gsm8k@opus  swe@opus
spawned      mbpp@gpt5  mbpp@opus   gsm8k@gpt5  gsm8k@opus  swe@gpt5  swe@opus
             └── complete on both models ──┘
```

**No duration-based ordering.** Sorting the longest task definitions first would improve makespan — the long pole has to run regardless, and starting it first lets short tasks fill in around it rather than trailing after it — but it requires an estimate Steward does not have. `samples × epochs` is available and is the wrong granularity for precisely this comparison: a fifty-sample agentic task can outlast a five-thousand-sample multiple-choice task, so sorting by sample count across task definitions would be confidently backwards. Smoke deliberately does not time tasks, and prior-log durations would work but only on a repeat run. Better to leave the order the author wrote than to reorder it against a bad proxy.

**This closes the debt the ceiling left open.** Declining `eval_set()`'s model floor meant nothing guaranteed a slot per model — but spawn order applies to the initial launch, not only to the queue, so a task-major spawn puts at least one task per model in flight whenever the ceiling is at least the model count. Steward gets from ordering what `eval_set()` gets from its floor, with no second mechanism.

**One limit, stated rather than guessed around.** Steward cannot know the user's comparison axis. A sweep may vary models, task args, or solvers, and the manifest looks much the same in each case. Spreading by *model* is justified independently by rate-limit buckets, so it holds regardless; that it also stratifies the most common comparison is a bonus rather than the premise. For an args sweep on a single model, the transposition is a no-op and the author's order simply stands.

## 3. Setting the concurrency knobs

### 3.1 The three knobs have different scopes, and the difference decides everything

This was the most consequential thing to get right, because the docs treated all three as one "per-process budget that must be divided across workers", and they are not the same kind of thing at all:

| lever | scope | multiplies across workers? |
|---|---|---|
| `max_samples` | **per task** — `ResizableLimiter` per task in `_task_sample_semaphores` | **no** |
| `max_connections` | process-global pool, adaptive | yes, but self-correcting |
| `max_sandboxes` | process-global — *"sandbox concurrency is shared across the process's evals, not per-eval"* | **yes, with no feedback** |

The first row is the surprise. Because `max_samples` is per *task*, a worker running one task gets precisely the semaphore that task would have had inside a single-process `eval_set()` run — so there is nothing to divide, and passing a definition's value through unchanged preserves its meaning exactly. What differs between Steward and `eval_set()` is *task* parallelism, which is the ceiling decision, made deliberately above.

### 3.2 `max_samples` — set explicitly, so it can be steered

Steward always writes an explicit `max_samples` into the selection, **defaulting to 40** and yielding to whatever the definition set. Explicit is the point: it is the difference between a `ResizableLimiter` the control channel can retune mid-eval and a `DynamicSampleLimiter` that tracks the model's connection controller and cannot be adjusted at all ([workflow.md](workflow.md), *Setting `max_samples` explicitly is what makes it a knob*).

The cost is real and worth stating: leaving it unset would let sample concurrency ride the adaptive controller, which is genuinely better at finding the right level *within one process*. Steward gives that up because per-process adaptation cannot see the fleet, and coordination across workers is the thing only Steward can do.

40 is a starting point, not an answer. It is deliberately modest, because the ratchet is asymmetric — raising a limit takes effect immediately, lowering one only stops new acquires and waits for in-flight samples to drain — so climbing from a low setpoint is cheap and descending from a high one is not. Users who know their eval sets it in the definition and Steward honours that.

### 3.3 `max_connections` — left adaptive, because AIMD coordinates itself

Steward does **not** divide `max_connections` across workers, and the reason is structural rather than a judgement call. The adaptive controller is AIMD: additive increase of 5% per clean round, multiplicative decrease of ×0.8 per rate-limit episode, with a 15-second cooldown that the server's `Retry-After` extends. Independent AIMD controllers sharing one bottleneck converge on a fair share of it — which is how TCP has shared links for decades.

So the provider's own rate limiting *is* the coordination mechanism. N workers each running their own controller against one quota will transiently overshoot together and then settle, and no allocation Steward could compute in advance would do better, because the quantity being discovered — what this account can actually sustain on this model at this hour — is not knowable in advance.

The defaults stay: `min 10, start 20, max 100` per process. The ceiling is a bound on *discovery*, not a promise of load. And it yields a precise signal for when it is the wrong bound: **a fleet pinned at max with no scale-downs is limited by the ceiling, not by the provider**, and that is when raising it is right.

### 3.4 Growing `max_samples` — rate limits are the signal, not saturation

Something has to raise 40, and the obvious signal is the wrong one. **A saturated sample limiter tells you nothing.** Sitting at 40/40 may simply mean eighty samples remain queued; it says nothing about whether sixty would run better. Saturation measures demand, not headroom.

The signal is **the absence of rate limits** — no backpressure, or only light backpressure, means the provider has room and `max_samples` should climb. That is observable rather than inferred: the config view reports each adaptive controller's recent scale changes, so a rate-limit episode leaves a visible multiplicative cut.

[workflow.md](workflow.md) warns, correctly, that *rate limits are the wrong signal for the local ceiling* — a run can climb to 200 concurrent samples with no rate limits at all and still take the box down. That caution stands, and the structure below is what answers it: **with `max_sandboxes` divided properly, raising `max_samples` cannot allocate more containers.** Extra samples queue on the process-global sandbox limiter instead, holding a coroutine and its sample state rather than a container. The local ceiling is enforced by the sandbox limiter, which frees the sample limiter to follow the provider signal alone.

So the two decisions interlock: **bounding sandboxes correctly is what makes growing samples safe.**

### 3.5 The signal is mechanical; the decision is not

Because the signal reduces to "were there scale-downs this interval", it is tempting to encode the whole rule in `tend` and let the mechanical layer ramp on its own. **It stays with the agent.** `tend` reports the signal; the agent decides whether to act on it.

The reason is that *no rate limits* establishes only that the provider has room, and having room is not the same as wanting more. The judgement that follows draws on things `tend` has no way to weigh — whether an arm is already producing anomalies that make going faster a way to break more samples, whether a deadline makes the risk worth taking, whether the box is also hosting something else, whether this provider rate-limited an hour ago and has simply gone quiet since. That is run history and project policy, which is exactly the material the agent exists to hold and a threshold rule cannot represent.

It also keeps a line the design draws everywhere else intact: [workflow.md](workflow.md)'s *the envelope is policy, the tuning is the agent's job*. An autonomous ramp in `tend` would be the first policy the mechanical layer executes on its own, and the first place where a bad rule ships in the binary rather than being arguable in a runbook.

**The cost is accepted, and it is real: nothing ramps while no agent is in session.** A run that starts at 40 and is left alone stays at 40. That is the correct failure — the unattended behaviour is *slower than optimal*, which is recoverable, rather than *further into a provider or a host than anyone chose*, which is not. It also puts a floor under how bad the starting value may be, since 40 has to be survivable on its own, and it makes the initial value worth tuning in the definition where the author knows the workload.

What `tend` owes the agent, then, is not a recommendation but the evidence: per-model rate-limit episodes since the last tend, current `max_samples` per worker, and how long each has held. The shape of that report belongs with the rest of the tend summary ([agent.md](agent.md)).

### 3.6 `max_sandboxes` — divided, but only where the host is the ceiling

Sandbox concurrency is process-global, so N workers means N independently-computed limiters — and unlike connections there is **no backpressure signal whatsoever**. A provider says 429; a Docker host says nothing until it thrashes or the OOM killer arrives. Over-allocation is discovered by the box falling over.

Whether that matters depends entirely on where sandboxes run, and Inspect already draws the distinction as a first-class API rather than something Steward must infer from a provider name:

```python
# SandboxEnvironment base — every provider that does not override it
def default_concurrency(cls) -> int | None:
    """Default max_sandboxes for this provider (`None` means no maximum)"""
    return None

# DockerSandboxEnvironment
def default_concurrency(cls) -> int | None:
    return 2 * (os.cpu_count() or 1)
```

**`None` means elastic; an integer means host-bound.** k8s and every non-overriding provider are correctly elastic by default, and Docker declares a host budget. That is exactly the split [workflow.md](workflow.md) draws by hand in its sandbox table, available programmatically.

**The hazard is quadratic, and the shape of the number says why.** Docker's `2 × cores` is plainly a statement about what one *host* supports — but it is applied per process, and Steward runs `cores` workers:

| box | containers requested |
|---|---|
| 16 cores | 16 workers × 32 = **512** |
| 64 cores | 64 workers × 128 = **8,192** |

So the rule follows from what the number already means:

> **Effective host budget** is the definition's `max_sandboxes` if it set one, otherwise the sandbox type's `default_concurrency()`. If that is `None`, the provider is elastic and Steward divides nothing. If it is an integer, it is a host budget, and each worker gets `budget ÷ workers` (floor 1).

Per sandbox *type*, since tasks in one eval set may use different ones — the Docker tasks share a host budget and the k8s tasks do not compete for it.

**Where the budget is smaller than the pool, the pool is what gives.** Dividing floors at 1, so ten containers across sixteen workers would hand out sixteen — overshooting the very budget being divided. The limiter cannot fix this, because each worker's is process-local and one is the smallest it goes. The fix belongs a layer up: *Launch everything, up to a ceiling* gains a second ceiling, and **the number of workers concurrently running tasks of a host-bound sandbox type is capped at that type's budget.** Tasks with no sandbox, and tasks on an elastic provider, are unaffected — they are not competing for the host, and starving them to respect a Docker budget would be the wrong trade. This is possible only because the manifest knows each task's sandbox type, which is the same thing the first upstream ask below is for.

**Two things are missing before this can be implemented**, and both are narrow:

- **The manifest cannot see the inputs.** Capture records no per-task sandbox type, and its `options` carries `log_dir`, `retry_attempts`, `limit`, `epochs`, `tags`, `metadata`, the error-handling trio, and `scanners` — but not `max_sandboxes`. Steward needs the sandbox *type name* per task (it can call `default_concurrency()` itself, since the provider package is installed in the same environment the workers run in) and `max_sandboxes` in options. A capture-schema addition.
- **The selection document cannot carry the result.** It takes `log_dir` and `max_samples`, so a worker's sandbox limit cannot be set at spawn — only patched afterwards, by which point containers are open and the ratchet says lowering the limit will not reclaim them. A fourth operational override, on precisely the grounds the others were argued: operational, absent from `task_identifier`, and unreachable by environment variable.

One wrinkle worth flagging rather than fixing here: Docker's `default_concurrency()` calls `os.cpu_count()`, which reports host processors rather than a container's quota — the same lie discussed under *Cores means the cgroup's cores*, this time inside Inspect. In a Hawk pod it is moot, because the infra config sets `max_sandboxes` explicitly and the default is never consulted. Elsewhere it is Inspect's to fix.

### 3.7 When a worker exits, only one thing needs redistributing

As a run drains, capacity frees. The question of what to do with it mostly dissolves, and it is worth saying why, because "rebalance on completion" sounds like a scheduler obligation and is only one here.

**Redistribution presupposes an allocation, and two of the three knobs are not allocations.** `max_samples` is a per-worker constant that grows on a signal, not a share of a fleet total — and the drain phase is already handled by the mechanism above, because fewer live workers means less rate-limit pressure, which is the signal that says raise. Adding a rebalancing rule would be the growth rule under a second name, with a second set of thresholds to disagree with the first. `max_connections` is per-process and adaptive; each controller finds its own level and a shared 429 is what coordinates them. Neither has anything to hand back.

**Slot reuse is real but only applies when there is a queue at all**, which is the uncommon case — the pool ceiling binds only when pending tasks exceed cores, and a typical sweep has fewer. When it does bind, a freed slot takes the next task in spawn order. That is backfill, not rebalancing, and it needs no policy beyond the order already chosen.

**`max_sandboxes` is the one genuine allocation, and its denominator is chosen so the question does not arise.** Divide by the number of *outstanding* tasks of that sandbox type, capped at the pool ceiling — not by the number of workers running right now:

```
per-worker share  =  host budget  ÷  min(pool ceiling, tasks of this type not yet complete)
```

That denominator only ever decreases, so each worker's share only ever increases, so **every patch is a raise** — and raises take effect immediately while lowering waits for in-flight samples to drain. The ratchet's asymmetry stops being a hazard and becomes the reason this works. Using live workers as the denominator would break it: two survivors would be widened to half the host each, and the next spawn off the queue could not be given room back.

## 4. Scanning is scheduled work

[execution.md](execution.md) establishes what a scan pass *is* — a detached child running the definition in a third mode over a list of log locations, with exactly one alive at a time so the scan directory keeps a single writer. What remains is when to spawn one.

### 4.1 Immediately, because a scan is not competing for a core

A scan pass costs a process, which under a `min(cores, pending)` ceiling looks like it should cost a task its core. It does not, and the pass should not be scheduled as though it did: a scan's work is reading transcripts and, for model-graded scanners, waiting on model calls. Transcript decompression and JSON parsing are real CPU but brief and bounded per log; the expensive part is I/O-bound. **A scan pass does not consume a worker slot**, and the pool ceiling counts task workers only.

So there is no reason to defer. A pass spawns as soon as there is something unscanned and no pass is running, rather than waiting for a threshold of logs to accumulate. Latency is the whole argument: [workflow.md](workflow.md) shows scan findings arriving last and re-opening runs that read as resolved, so anything that delays them pushes discovery closer to signoff, where a finding that needs a re-run costs the most. Deferring to a single terminal pass is the worst version of this and is rejected outright — it makes signoff block on a long serial job whose output may invalidate it.

### 4.2 One log at a time, and where that has to bend

Each pass covers one log. A crashed pass then costs one log's scan rather than a batch's, progress is exactly countable, the in-flight record names a single log, and a transcript that reliably kills a scanner is isolated to itself instead of blocking everything queued behind it.

**One case forces an exception, and it should be recorded rather than discovered later.** A scan pass has to *be* the definition executed, so every pass pays the definition's startup cost — seconds for a script, ~1.1s for Flow, and for Hawk a `uv pip install` and secrets resolution measured in minutes. Per log, over a large eval set, that is not a tax but a wall. So the rule is one log per pass **when scanning keeps up**, and a pass otherwise takes whatever is unscanned when it starts:

> A pass takes the unscanned logs available at spawn. Under immediate spawning that is one log; if scans run slower than logs land, the backlog batches itself.

This is self-regulating in the direction that matters — the batching only appears where per-pass startup is being paid too often, which is the same condition that makes batching worth it. It preserves one-at-a-time serialization either way, and idempotency means a re-run after a crash skips what already has rows.

### 4.3 A crashed pass is an anomaly, not a retry

A scan pass that dies is reaped like any other detached child, and the design's failure rule applies unchanged: it is recorded and surfaced, not automatically respawned. [workflow.md](workflow.md) already lists a failed scan pass as an anomaly. With one-log passes the evidence is unusually good, because the record names the log that was being scanned when it died — which is the difference between "scanning is broken" and "this transcript breaks the scanner", and only the second is actionable without a human.

The queue is unaffected: an unscanned log stays unscanned and stays counted, so `status` reports coverage honestly rather than reporting a drain that silently skipped something.

## 5. Failure is adjudicated, not retried

### 5.1 What can still fail a task

`fail_on_error=False` absorbs sample errors: a task carrying five hundred errored samples reaches the end of its dataset and finishes `status="success"`. So a task that *fails* has failed for a reason categorically outside the sample loop, and the list is short:

| observed | cause | recurs? |
|---|---|---|
| no log, clean non-zero exit | import error, task construction, bad model name | **always** |
| no log, killed by signal | OOM, host pressure | usually, unless the fleet changes |
| log, `status="error"` | dataset load, sandbox provisioning, a scorer or metric throwing | mixed — scorer bugs always, provisioning sometimes |
| log, process vanished | OOM mid-run, host loss | sometimes |

**Most of that is deterministic.** Automatically respawning a definition that will not import, or a scorer that throws after every sample has already run, spends money to arrive at exactly where it was — and does it at 3am with nobody watching. The rarity is the point: because sample errors no longer reach this level, a task failure is now an unusual and mostly-structural event, and structural events deserve a look rather than a counter.

### 5.2 No automatic restart

**A task failure opens an anomaly. Steward never respawns a failed task on its own.**

The objection to this is obvious and turns out not to hold: a host reboots at 3am, forty workers die, and now forty approvals stand between the run and progress. But anomalies are *classed*, not itemized ([workflow.md](workflow.md), *Rule on classes, not instances*) — forty tasks killed by one reboot are **one anomaly with forty instances, and one ruling restarts all of them**. The cost of always asking is one question per cause, which is a rate a person can sustain.

So the attempt ceiling this document set out to choose is **zero**, and there is no number to tune. The same line runs one level down: a sample gets the `retry_on_error` attempts its definition asked for, and past those every further attempt is adjudicated too ([execution.md](execution.md), *The authority line is not where the tiers divide*). Standing authorization is the pressure valve at both levels — `policy.md` may admit a class of restart in advance, which is a ruling made earlier rather than an exception to the rule. What bounds recurrence is not a counter but the record: every restart required a ruling, rulings accumulate as precedent, and a class that keeps returning is visible as such rather than hidden inside a retry count that resets. That is the mechanism the design already has for this, and it is strictly more informative than the one it replaces.

This also removes a mechanism rather than adding one. [execution.md](execution.md) treats whole-task retry as a fourth thing standing beside the three recovery tiers; it is really the third tier again — post-completion adjudication, with respawn-and-`resume` as the action a ruling authorizes.

### 5.3 The failure signature classes the anomaly

The forensics still matter, but for proposing rather than deciding. Two bits are free — whether a log exists, and whether the process exited cleanly or died on a signal — and together they separate causes that want genuinely different responses:

| signature | the proposal it supports |
|---|---|
| no log, traceback | "this fails identically every time — here is the error; the definition needs fixing" |
| no log, `SIGKILL`, several tasks at once | "resource exhaustion; reduce the pool and resume" |
| `status="error"` after scoring | "every sample completed and the scorer threw; fix it and re-score rather than re-running" |

Getting this wrong in the other direction was a real risk. A plain *did it write a log?* test collapses the first two rows — and the case it mistakes is the one launch-all makes most likely, since until Layer 2 pruning lands the probable failure of a large sweep is OOM during startup: no log, and read as permanent. Steward would decline to retry precisely the failure a smaller pool would fix.

Note this is process forensics, not error classification. It reads how a worker died, never what its samples did, so it carries no dependency on the sample-error taxonomy ([execution.md](execution.md), open question 7).

### 5.4 The cost, and where it is paid

A genuinely transient failure — a sandbox provisioning blip, a Docker daemon hiccup — now waits for a human instead of recovering itself. That is the wasted night this design cares most about avoiding, and it is a real cost rather than a theoretical one.

The mitigation is one the design already has: **`policy.md` pre-authorization.** *"Resume tasks killed by host loss or sandbox provisioning without asking"* is a standing rule granted once, and it converts a night's worth of potential 3am questions into standing authority — the *authorize at 10pm, do not interrogate at 3am* argument, applied to failure rather than to scaling. It also gives the launch-time exchange a concrete item it was missing.

The framing that matters: **approval is the default, and automatic restart is something a human grants for named classes** — rather than something Steward assumes and then bounds with a counter it hopes is large enough.

### 5.5 Approved re-runs go first

Queue order between a fresh task and an adjudication re-run only bites when pending work exceeds the ceiling. When it does, **re-runs go first.**

Three reasons, and the first is now decisive:

- **Somebody asked for it.** A re-run exists only because a ruling authorized it, which is a stronger claim on a slot than any task that is merely next in the manifest.
- **They are cheap.** A respawn with `resume` reuses every completed, non-errored, non-invalidated sample, so re-running a five-hundred-sample task with forty-seven errors runs forty-seven samples. Re-runs and fresh tasks are not comparable units competing for a slot.
- **They front-load decidability.** A re-run that closes at 11pm produces a question someone can answer; the same work finishing at 6am produces one that stalls until morning. Prioritizing re-runs moves *the moment anomalies become resolvable* earlier in the night.

The starvation risk that would normally argue for capping this at a fraction of the pool does not arise, because nothing can loop without a ruling in between.

**Priority means queue order, never preemption.** A re-run does not displace a running worker, whatever its urgency.

## 6. Open questions

None outstanding. Spend was the last one, and it is resolved as a won't-do: Steward does not track, project, cap, or act on spend. The reasoning is in [workflow.md](workflow.md), *Spend is not Steward's to manage* — briefly, Inspect ships no prices so Steward would have to own a price table; a cap observed at the tend interval is soft by construction and cannot stop what motivates wanting it; firing one would add a trigger rather than a capability, since `stop`, `pause`, and archiving an arm already exist; and the real control is the manifest, which enumerates everything before a dollar is spent.
