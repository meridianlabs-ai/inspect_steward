# Scheduling

**Status: settled. The process model, the worker pool, spawn order, all three concurrency knobs, scan cadence, and failure handling are decided; spend is declined outright.**

[execution.md](execution.md) establishes that the architecture is a pure function — `reconcile(manifest, inflight, log_dir) -> (actions, summary)` — and argues at length for its *properties*: testable, idempotent, driver-independent. It does not say what the function decides. This document is that half.

## 1. The process model

### 1.1 One task per process, by default

A worker runs one task unless the operator says otherwise. An earlier draft of this section said *always*, and the argument it gave for that is sound — it is just an argument for a default rather than for a prohibition.

**The binding argument is the GIL, and it is easy to wave away incorrectly.** Evals are usually described as I/O-bound, and they mostly are: a sample waiting on a model API costs nothing but a slot. But the work that is *not* waiting is all Python bytecode — transcript construction for long agentic runs, JSON serialization, `.eval` zip compression on write, non-model scorers, sandbox subprocess management — and that work is serialized within a process however high `max_samples` goes. **One process saturates one core.** Raising sample concurrency past that point buys queueing, not throughput.

Two further properties come free and are worth naming because each was argued for separately elsewhere:

- **Failure isolation.** One task per process means one crash costs one task. [hawk.md](hawk.md) makes this the headline benefit of Steward over a single-runner-pod model, where an OOM restarts the world.
- **Scheduling granularity.** The unit Steward spawns, reaps, retunes, and adjudicates is the unit the manifest enumerates. Nothing has to be mapped onto anything.

**What the prohibition got wrong is that all three of these are prices, and somebody else may be paying a larger one.** The GIL bounds a packed process's *burst* throughput; it costs nothing while its tasks are waiting on a model, which is most of the time. Failure isolation is real and is why the default is what it is. And granularity is preserved anyway — a packed worker still writes one log per task, so observation, adjudication, and resume are unchanged; only the process count moves. Set against that is a cost the prohibition could not address at all: a runtime charging an install and a secrets round trip *per process* (§1.2), where dividing the run is the expensive choice.

So the trade recorded in [workflow.md](workflow.md) — "fewer, larger workers pay less per-worker startup cost" against finer granularity — is real after all, and this section previously dismissed it by measuring the wrong quantities. Startup cost is seconds *per process*, which is minutes of a five-hundred-task sweep and unbounded when a frontend installs packages; the CPU ceiling is permanent but only binds on bursts. Neither dominates, which is why it is a knob.

### 1.2 Batching, opt-in

The selection document accepts a *list* of tasks so that one worker can host several. Steward writes one entry per task the pour gives that worker, which is one entry by default and the whole batch where the operator has asked for fewer processes than tasks.

**The motivation is startup cost, and it is the one thing the frontends cannot fix for us.** Flow's measured ~1.1s of pre-boundary work; Hawk's `uv pip install` and Secrets Manager round trip per invocation, unbounded on a cold environment or a config declaring `packages:`. Five hundred small tasks is around half an hour of pure startup, and no amount of upstream work removes it from a runtime whose per-process side effects are the point. Where that dominates, running the eval set whole and supervising the one process is the cheaper and the more honest arrangement.

**Batching does not solve slot idle, and is not offered as a fix for it.** That was the original motivation and it does not survive: the cost is `interval/2 ÷ mean task duration`, a fraction of a percent for the hours-long tasks Steward is for, and the two levers that address it directly — a shorter interval, a higher `max_tasks` — are knobs rather than mechanisms.

**One constraint the earlier draft named is now discharged rather than avoided.** *Batch only tasks sharing a model, or the process's initial limit is wrong for some of them* — this was true when `max_samples` was thought to be process-global. It is per **task** (§3.1), so a packed worker gives each of its tasks exactly the semaphore it would have had alone, and a batch may span models freely. The pour deals tasks round-robin across processes for an unrelated reason (§2.4).

**What packing costs is stated rather than discovered.** A process that dies takes every task it was running mid-flight, so width trades crash isolation for startup cost, monotonically. It is not as bad as it sounds — tasks the process already finished have landed their logs, and the rest resume from wherever they got to, so the loss is partial progress rather than work — but at one process the exposure is the whole run, which is precisely what Steward exists to remove. Hence the default of one, and hence this being an escape hatch rather than a tuning knob.

### 1.3 No sharding

The mirror image of batching is splitting one large task's samples across processes, for which the selection schema reserved a per-task `samples` field. **Steward will not do this.** A task always runs all of its samples in one process, and controls its own memory through `max_samples` from the inside.

The cost is accepted rather than hidden: a single very large task is bounded by one core, and no amount of hardware helps it. What buying that back would cost is a second granularity mechanism running alongside the first — multiple logs per task, a completeness predicate that reassembles shards, and adjudication that has to know which shard a sample came from. One unit of work, one process, one log is worth more than the parallelism.

This closes [configuration.md](configuration.md) open question 6.

## 2. The worker pool

### 2.1 Two knobs, and they are about different things

A run has a shape, and it takes two numbers to say:

| key | means | default |
|---|---|---|
| `max_tasks` | how many tasks may be in flight at once, fleet-wide | unbounded — all of them |
| `max_workers` | how many worker **processes** those are divided into | unbounded — a process per task |

**`max_tasks` is the load; `max_workers` is only the packing.** Total concurrent samples is `tasks in flight × max_samples` and nothing else — `max_samples` is per task (§3.1), so a process running four tasks is four times the load of one running one, whether that is four processes or one. Steward owns both factors of the product, which is what makes the fleet's load on a provider deterministic rather than emergent ([workflow.md](workflow.md), *What Steward actually has to solve*). `max_workers` moves neither factor. It buys back per-process startup and costs crash isolation, and that is the whole of what it does.

An earlier draft wrote this identity as `workers × max_samples`, which was correct only while a worker was always one task. It is worth restating in the form that survives packing:

```
total concurrent samples  ≈  tasks in flight × max_samples
local compute             ≈  (processes × process cost) + (concurrent samples × sample cost)
throughput                ≈  concurrent samples on that model        [per rate-limit bucket]
```

A process costs an interpreter and its imports, once — or, for a frontend that installs packages, considerably more (§1.2). A concurrent sample costs a sandbox container, memory, and a slot, continuously. **Process count is usually the cheap axis; sample concurrency is the expensive one** — which is what makes a process per task the right default, and `max_workers` the knob for the runtimes where the first clause is false.

### 2.2 Launch everything, and let the operator bound it

Every pending task starts immediately, in a process of its own. Both knobs are unbounded by default, so an unshaped run has no queue at all and no two tasks sharing an interpreter.

**Neither default is derived from the hardware, and an earlier draft got that wrong twice.** Its first reasoning — *past core count another process buys no parallelism, because there is no core for it to run on* — misreads the workload. A worker is on the CPU in **bursts**: transcript construction, JSON serialization, `.eval` compression, non-model scoring. Between the bursts it is waiting on a model API and using no core at all. So ten workers on four cores is entirely ordinary; they rarely contend, and when they do it costs latency on a burst rather than throughput on the run.

Its second answer was a flat ceiling of ten, borrowed from `eval_set()`'s own `max_tasks` default. That is a defensible number and it is still the one to reach for; what was wrong was shipping it as the default, because **a default limit is a limit nobody chose**. A ten-task sweep is not helped by it, a five-hundred-task sweep is throttled by it, and in neither case did the operator say anything. Unbounded is the honest starting point: Steward runs what the definition asked for, and the operator narrows it when the machine or the provider gives them a reason to.

**The pour is four lines, and each knob is counted the same way — against what is already there.**

```
placeable = pending             if max_tasks   is None else max(0, max_tasks   - tasks in flight)
batch     = pending[:placeable]
processes = len(batch)          if max_workers is None else max(0, max_workers - processes alive)
            (dealt round-robin, and never more processes than there are tasks to put in them)
```

Reading either as "start up to N" on a later tend double-counts: eight tasks already in flight and five pending under `max_tasks: 10` places two, not five.

**They compose to four useful shapes**, and the two extremes are the ones worth naming:

| `max_workers` | `max_tasks` | 500 pending | |
|---|---|---|---|
| — | — | 500 processes × 1 task | the default |
| — | 10 | 10 processes × 1 task, 490 queued | the old ceiling, spelled properly |
| 10 | — | 10 processes × 50 tasks | startup cost cut fifty-fold |
| 1 | — | 1 process × 500 tasks | run it whole |

**`max_workers` alone queues nothing from a standing start**, which is the property that keeps the two knobs from being confused: given everything at once it changes how the work is packaged, not how much of it starts.

**It does queue on a later tend, and an early draft of this section said otherwise.** A selection document is written once, at spawn, so a live process cannot be handed more work — and a task that becomes pending *after* its siblings were placed has nowhere to go while every allowed process is still alive. A task that errors mid-run while its process carries on with the rest is exactly that shape. So both knobs can hold a queue back, which is why **the pour reports which one did** rather than leaving a reader to infer it from whichever key is set: with `max_workers: 1` and `max_tasks: 10` and one process alive, nine task slots are free and raising `max_tasks` buys nothing. `pour` returns `blocked`, and every renderer names the bound that is actually binding.

**A parked worker holds its slot.** A worker waiting on a human approval is alive and making no progress ([execution.md](execution.md), *The parked worker*), and its tasks count against `max_tasks` like any others. Excluding them would silently overrun the budget that number exists to bound — the sandbox is still up, the process is still resident, the model connections are still open — so the pour counts them as in flight, and `reconcile` must neither reap the worker nor schedule a replacement for its tasks. Packed, this bites harder: a parked process holds every task it was given, not one.

The consequence is intended rather than tolerated: enough parked workers slow a bounded fleet, and a budget's worth stops it. Nothing should proceed while decisions pile up unanswered, and a run that has stopped for want of a human is a clearer thing to be told about than one that quietly kept starting work nobody could approve. What makes it survivable is being *told* — a park is a notification, not a scheduling problem.

**Slot idle exists only where somebody asked for it.** With `max_tasks` set, a task finishing at t=0 leaves its slot empty until the next tend, costing roughly `interval/2 ÷ mean task duration`. For the overnight sweeps Steward is for, where tasks run for hours against a ten-minute interval, that is a fraction of a percent. The levers if it ever matters: **shorten the tend interval, or raise `max_tasks`** — not batching, which addresses a different cost entirely (§1.2). Packing makes one case slightly worse and it is worth knowing: a process frees capacity only when *all* of its tasks finish, so refill is coarser than it is at the default width.

### 2.3 Memory is assumed adequate, and the defaults now assume more of it

There is deliberately no memory term in either knob. Two reasons, and the second is the substantive one.

Nobody had measured per-worker memory, so any coefficient would have been invented. More importantly, **the quantity it would describe has since changed shape.** Every worker used to construct every task in the eval set, datasets included — so per-worker memory scaled with the whole manifest and grew each time someone added a task. A memory formula written then would have encoded the un-pruned world and been wrong on the day the thing it compensated for was fixed.

**Layer 2 pruning has landed, and that removed the scaling term rather than reducing it.** A worker now constructs only the tasks it was given ([configuration.md](configuration.md) §6.2). Measured on a 51-task set with one large dataset: un-pruned peak RSS went 0.33 → 0.39 → 0.52 GiB as heavy tasks went 1 → 2 → 4, while pruned it stayed flat at ~0.27 GiB. Per-worker cost is now proportional to the worker's own task, which is what makes a large manifest safe at the default shape.

**What is left is a constant per worker, and it is reported rather than assumed.** A worker still imports inspect_ai and loads its own task's dataset, and unbounded defaults still multiply that by the fleet width: a five-hundred-task sweep is five hundred copies of whatever that constant is, and asks the provider for `500 × max_samples` concurrent samples besides. Both are the operator's to narrow, and narrowing either is one key: `max_workers` cuts the copies, `max_tasks` cuts the provider load.

So the guard this section called for exists, in the form it called for — "a launch-time check against a measured figure, a guard rather than a term in the defaults". **Capture is the measurement**: it executes the definition and constructs every task, which is the most startup work anything does, so its peak RSS bounds a worker's and was taken while already being paid for. `launch` records it on the manifest and prints it against the width the run will reach; `status` repeats it. Nothing refuses and nothing defaults — the operator who chose unbounded keeps that choice and is told what it can cost.

**A ceiling rather than an estimate, which is a consequence of the paragraph above and is stated in the line.** Pruning means a worker builds its own tasks and capture builds all of them, so the two figures diverge exactly as the eval set grows — the case where they converge is the case where pruning does not fire, which is when an operator most needs the number. Reporting a bound and calling it one is the honest form; a figure labelled *projected* would be read as *expected* and be wrong by the width of the saving.

### 2.4 Spawn order transposes the crossing

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

**This closes the debt the ceiling left open.** Declining `eval_set()`'s model floor meant nothing guaranteed a slot per model — but spawn order applies to the initial launch, not only to the queue, so a task-major spawn puts at least one task per model in flight whenever `max_tasks` is at least the model count. Steward gets from ordering what `eval_set()` gets from its floor, with no second mechanism.

**Packing deals rather than slices, and this is why.** Once several tasks share a process, the pour has to decide which ones — and taking a contiguous run of the transposed order would hand one process every model of the same task. That is the first of the two failures above, reintroduced at process granularity: lose the process and the task is gone on every arm at once, which is exactly the uncomparable interruption the transposition exists to prevent. Dealing round-robin spreads each task's models across processes instead, at no cost, so the transposition survives packing rather than being undone by it. (Rate-limit spreading needs no help here — a packed process runs its tasks concurrently, so it is on all of its models at once whichever way they were cut.)

**One limit, stated rather than guessed around.** Steward cannot know the user's comparison axis. A sweep may vary models, task args, or solvers, and the manifest looks much the same in each case. Spreading by *model* is justified independently by rate-limit buckets, so it holds regardless; that it also stratifies the most common comparison is a bonus rather than the premise. For an args sweep on a single model, the transposition is a no-op and the author's order simply stands.

## 3. Setting the concurrency knobs

### 3.1 The three knobs have different scopes, and the difference decides everything

This was the most consequential thing to get right, because the docs treated all three as one "per-process budget that must be divided across workers", and they are not the same kind of thing at all:

| lever | scope | multiplies across workers? |
|---|---|---|
| `max_samples` | **per task** — `ResizableLimiter` per task in `_task_sample_semaphores` | **no** |
| `max_connections` | process-global pool, adaptive | yes, but self-correcting |
| `max_sandboxes` | process-global — *"sandbox concurrency is shared across the process's evals, not per-eval"* | **yes, with no feedback** |

The first row is the surprise, and it is what makes packing cheap. Because `max_samples` is per *task*, a worker gets precisely the semaphore each of its tasks would have had inside a single-process `eval_set()` run — so there is nothing to divide, whether it holds one task or fifty, and passing a definition's value through unchanged preserves its meaning exactly. It also discharges the constraint §1.2 used to carry: a batch may span models freely, because no task's limit depends on what it is sharing a process with.

What differs between Steward and `eval_set()` is *task* parallelism, and there Steward now writes the number rather than inheriting one. A worker's selection carries `max_tasks` equal to the size of its batch, unconditionally. That is not a preference: in selection mode `eval_set()` never reaches its own defaulting, so an unset value falls through to `eval()`'s rule — one task at a time for a single model, the model count for several — and a packed worker would run its batch sequentially with nobody having chosen that. The fleet-wide bound is `Pool.max_tasks` (§2.2); this is only that process's share of it.

### 3.2 `max_samples` — set explicitly, so it can be steered

Steward always writes an explicit `max_samples` into the selection. Explicit is the point: it is the difference between a `ResizableLimiter` the control channel can retune mid-eval and a `DynamicSampleLimiter` that tracks the model's connection controller and cannot be adjusted at all ([workflow.md](workflow.md), *Setting `max_samples` explicitly is what makes it a knob*).

**Retuning it is task-scoped, so a read comes first.** The knob is on the task's config, not the process's, and its selector is a `task_id` Steward does not know until it asks — so the tend's fleet listing is a precondition for moving the setpoint rather than an independent call. Two things make that free: the listing spans every process in one call, and at one task per worker the row that comes back is unambiguous without matching. The listing also reports `in_use` alongside the limit, which is what makes a *lowering* decision informed rather than hopeful — see §10.5 on why that direction is the slow one.

The cost is real and worth stating: leaving it unset would let sample concurrency ride the adaptive controller, which is genuinely better at finding the right level *within one process*. Steward gives that up because per-process adaptation cannot see the fleet, and coordination across workers is the thing only Steward can do.

**Three parties can have an opinion about the number, and they rank by how specifically they asked:**

| | |
|---|---|
| the **operator**, on the launch that is happening now | wins outright — somebody typed it |
| the **definition** | next: how many samples a task should run at once is a property of the eval, and its author knows the workload |
| **the ramp's floor (40)** | nobody expressed a preference |

The middle row is the one that needed something from upstream, and it now has it: `eval_set()`'s capture records `max_samples` in the manifest's `options`, so a definition asking for 60 gets 60 instead of being silently replaced by Steward's 40. Before that field existed there was no read-side workaround either — a landed log records only the *effective* value, which is whatever Steward imposed.

The middle and bottom rows have to stay distinguishable, which is why Steward's default is not simply the initial value of the operator's setting. Collapse them and one of two things goes silently wrong: Steward's own fallback outranks a definition, or an explicit `--max-samples` loses to one. Neither is visible from the number that comes out.

Which row wins also decides the *regime*, and the difference matters more than the number. An explicit value — the operator's or the definition's — is **pinned**: somebody expressed a setpoint, and nothing moves it. The fallback is not a setpoint at all but the floor of the default ramp (`DEFAULT_SAMPLES_RAMP`, 40–200), and §3.5's loop climbs from it. 40 is deliberately modest because the ratchet is asymmetric — raising a limit takes effect immediately, lowering one only stops new acquires and waits for in-flight samples to drain — so climbing from a low setpoint is cheap and descending from a high one is not. A worker the ramp has climbed respawns at its climbed level rather than the floor: the level was earned against measured headroom, and a crash does not unmeasure it — clamped, though, into the range in force *now*, because the journal says what was authorized when the climb happened while `_steward.md` says what is authorized today, and a spawn answers to the second. Live workers are clamped the same way rather than only respawned ones: narrowing the range under a running fleet steps the tasks above it back inside, and that move is not gated like a growth step, because a level outside the envelope is not capacity being declined but authority already exceeded.

**The operator's row outlives the turn that typed it, alone among the flags.** `--max-workers` and `--max-tasks` are recomputed from scratch each turn and leave no residue, so one that is not repeated simply stops applying; `--max-samples` decides the *regime* and the regime persists in the workers it spawned, so a pin that lapsed would leave the next tend climbing a level nobody was ramping and spawning the queue at the ramp's floor — the operator's number overridden twice, by a default they never chose, while nobody was watching. The pin is therefore recorded in the observation like everything else a turn ran under, and released by writing a `samples_ramp` *range* in `_steward.md`: the file that holds standing wishes, and the one instruction that cannot mean anything but *ramp this run*.

### 3.3 `max_connections` — left adaptive, because AIMD coordinates itself

Steward does **not** divide `max_connections` across workers, and the reason is structural rather than a judgement call. The adaptive controller is AIMD: additive increase of 5% per clean round, multiplicative decrease of ×0.8 per rate-limit episode, with a 15-second cooldown that the server's `Retry-After` extends. Independent AIMD controllers sharing one bottleneck converge on a fair share of it — which is how TCP has shared links for decades.

So the provider's own rate limiting *is* the coordination mechanism. N workers each running their own controller against one quota will transiently overshoot together and then settle, and no allocation Steward could compute in advance would do better, because the quantity being discovered — what this account can actually sustain on this model at this hour — is not knowable in advance.

The defaults stay: `min 10, start 20, max 100` per process. The ceiling is a bound on *discovery*, not a promise of load. And it yields a precise signal for when it is the wrong bound: **a fleet pinned at max with no scale-downs is limited by the ceiling, not by the provider**, and that is when raising it is right.

### 3.4 Growing `max_samples` — rate limits are the signal, not saturation

Something has to raise 40, and the obvious signal is the wrong one. **A saturated sample limiter tells you nothing about headroom.** Sitting at 40/40 may simply mean eighty samples remain queued; it says nothing about whether sixty would run better. Saturation measures demand — which makes it a *precondition* rather than the signal: raising an unsaturated limiter reports as discovered capacity what was never asked for, so a step is only ever considered at the limit.

The signal is **the absence of rate limits** — no backpressure, or only light backpressure, given saturation, means the provider has room and `max_samples` should climb. That is observable rather than inferred: the config view reports each adaptive controller's recent scale changes, so a rate-limit episode leaves a visible multiplicative cut, timestamped, and "any cuts since my last turn" is a comparison.

[workflow.md](workflow.md) warns, correctly, that *rate limits are the wrong signal for the local ceiling* — a run can climb to 200 concurrent samples with no rate limits at all and still take the box down. That caution stands, and two local gates answer it. Sandboxes are bounded by construction: a step that would take the fleet-wide sum of setpoints past the machine's sandbox budget is never offered (§3.6), so following the provider signal cannot ask the host for containers it does not have. CPU is bounded by measurement: a worker whose window shows the process near a full core does not step, because the GIL means more samples buy queueing rather than throughput there — and a Python process at saturation is the one local resource the sandbox budget cannot see.

So the decisions interlock: **bounding the local resources is what frees the sample limiter to follow the provider signal alone.**

### 3.5 The signal is mechanical, and inside the envelope so is the decision

An earlier version of this section ruled the opposite way — `tend` reports, the agent decides — and accepted the cost in writing: *nothing ramps while no agent is in session*. Step 21 withdraws that acceptance deliberately, and it is worth recording both the original objection and what dissolved it.

The objection was never that the arithmetic needed judgement; it was that an autonomous ramp would be **a policy shipped in the binary that nobody chose**. That is answered by making the envelope the choice: the ramp acts only inside a range someone can see and set (`samples_ramp` in `_steward.md`, defaulted to 40–200 and disabled by any explicit `max_samples` anywhere), which is workflow.md's *authorize at 10pm, do not interrogate at 3am* made standing rather than conversational. And the accepted cost stopped being acceptable on its own terms: undershoot compounds (workflow.md §10.6), the hours Steward exists for are exactly the unattended ones, and a run left at a third of its viable concurrency all night is not a conservative outcome — it is the expensive one.

So `tend` acts, and every gate is mechanical. A step up is bought with a whole **clean window**: the limiter saturated (§3.4's precondition), zero scale-downs since the last turn, HTTP retries and sample errors flat, worker CPU under 75% of a core over the window, twenty minutes since the task's last step, the fleet-wide sandbox budget holding (§3.6), and no hold in force. A cut is bought with nothing: on *sustained* pushback — episodes in two consecutive windows, since a single episode is the adaptive controllers doing their own job — the connection ceiling is clamped to where the controllers already fell and the sample level steps back down, holds notwithstanding, because the cut exists precisely for when nobody is watching. Two loops, one discipline: **inspect's controllers move within bounds; tend moves the bounds.**

What the judgement list bought is not discarded — it is exactly what the **hold** is for. Whether an arm's anomalies make going faster a way to break more samples, whether pushback is really someone else's workload on a shared key, whether a deadline changes the risk appetite: `tend` cannot weigh those, and it does not have to, because the agent that can reads every retune in its next collection (each is a journal `action`) and freezes the climb with `steward ramp hold` — leaving the safety cut armed. The human keeps the two numbers only they may move: a pinned setpoint and the envelope itself, and capacity against either surfaces as a `tuning_proposal` rather than a move.

What `tend` owes the agent is therefore the narration and the evidence, and it delivers both: the tuning block carries each task's level and whatever gates its next step; the history carries every move with its reason; and the same reason lands in the eval log through the control channel's provenance, which is what makes an unattended retune reviewable afterwards.

### 3.6 `max_sandboxes` — a machine budget, enforced as a cap on the fleet's sample setpoints

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

**The hazard is the multiplication, and the shape of the number says why.** Docker's `2 × cores` is plainly a statement about what one *host* supports — but it is applied per process, and Steward runs a poolful:

| box | containers requested |
|---|---|
| 16 cores | 10 workers × 32 = **320** |
| 64 cores | 10 workers × 128 = **1,280** |

A flat ceiling makes this linear in the worker count rather than quadratic in cores, which is a real improvement over the cores-derived ceiling an earlier draft assumed — 8,192 on a 64-core box became 1,280. It does not make it safe: 1,280 is still ten times what the provider says the host supports.

**Earlier drafts answered the multiplication with division** — `budget ÷ workers` handed out per process at spawn — and immediately owed a corner-case ledger: a floor-at-1 rule that over-allocates whenever the budget is smaller than the pool, a per-sandbox-type split so Docker tasks and k8s tasks do not share a budget only one of them draws on, and two capture-schema additions before any of it could see its inputs. The tuning loop ([§3.5](#35-the-signal-is-mechanical-and-inside-the-envelope-so-is-the-decision)) dissolved the division by touching the same quantity from the other side: a task's containers never exceed its running samples, so **bounding the fleet-wide sum of `max_samples` setpoints bounds total containers** — and the ramp already owns those setpoints. The rule as built:

> **Effective host budget** is the definition's `max_sandboxes` if it set one, otherwise the largest per-process limit the workers themselves report over the live config read — which is the provider's `default_concurrency()`, and per the shape argument above a statement about the host. `None` is elastic and caps nothing. A ramp step is admitted only if the sum of setpoints across every task reporting a sandbox limiter, plus the step, stays within the budget.

Enforced at every decision rather than divided once at spawn, which is what retires the corners. Nothing is handed out, so there is no floor-at-1 to over-allocate through — when the ramp floors alone already exceed the budget, the ramp never steps and the tuning block names the budget as the binding constraint, an honest report where the division had a silent overshoot. Per-type bookkeeping is unneeded because each task's own live read says whether it is host-bound: an elastic task reports no limiter and simply never enters the sum. And drain-phase redistribution follows by arithmetic rather than policy ([§3.7](#37-when-a-worker-exits-nothing-needs-redistributing)).

**What the sum-cap deliberately does not do is lower a pinned setpoint.** The budget gates the *ramp*, so a definition that pins `max_samples=200` on ten Docker tasks can still ask the host for more than the provider says it supports — but those are ten numbers a person chose against a budget the same person owns, which is the class of contradiction Steward reports rather than resolves.

Of the two capture-schema gaps the division was blocked on, one landed and one dissolved: `options` now carries `max_sandboxes`, and the per-task sandbox *type* is unneeded because the live config read reports each task's effective limiters directly — Steward reads what the provider decided rather than re-deriving it from a type name. The selection-document override went the same way: nothing needs setting at spawn because nothing is divided at spawn.

One wrinkle worth flagging rather than fixing here: Docker's `default_concurrency()` calls `os.cpu_count()`, which reports the host's processors rather than a container's cgroup quota. A pod limited to 2 CPUs on a 64-core node reports 64, so the budget Steward reads back off the workers is 32× the truth in precisely the deployment where over-allocating kills the pod ([hawk.md](hawk.md)). Reading the real number is not hard — the cgroup CPU quota is one file, `cpu.max` on v2 and a `cfs_quota_us`/`cfs_period_us` pair on v1, and Hawk's own `memory_monitor` already polls the cgroup — but the inflation happens inside the provider, so it is Inspect's to fix. In a Hawk pod it is moot regardless, because the infra config sets `max_sandboxes` explicitly and the default is never consulted.

### 3.7 When a worker exits, nothing needs redistributing

As a run drains, capacity frees. The question of what to do with it dissolves, and it is worth saying why, because "rebalance on completion" sounds like a scheduler obligation and is not one here.

**Redistribution presupposes an allocation, and none of the three knobs is one.** `max_samples` is a per-task level the ramp grows on a signal, not a share of a fleet total — and the drain phase is already handled by the mechanism above, because fewer live tasks means less rate-limit pressure, which is exactly what a clean window measures. Adding a rebalancing rule would be the growth rule under a second name, with a second set of thresholds to disagree with the first. `max_connections` is per-process and adaptive; each controller finds its own level and a shared 429 is what coordinates them. Neither has anything to hand back.

**Slot reuse is real but only applies when there is a queue at all** — the pool ceiling binds only when pending tasks exceed it, which a small sweep does not and a large one does from the first tend. When it does bind, a freed slot takes the next task in spawn order. That is backfill, not rebalancing, and it needs no policy beyond the order already chosen.

**`max_sandboxes` was the one genuine allocation in earlier drafts, and [§3.6](#36-max_sandboxes--a-machine-budget-enforced-as-a-cap-on-the-fleets-sample-setpoints) dissolved it.** The budget is enforced as a cap on the fleet-wide sum of sample setpoints rather than divided into per-worker shares, so completion frees capacity by arithmetic: the finished task's setpoint leaves the sum, and the next clean window lets another task's ramp spend the headroom. There is no share to hand back, no denominator to choose, and no patch to send — the ramp *is* the redistribution. (An earlier version of this section chose an outstanding-tasks denominator precisely so that every repatch was a raise; the asymmetry it was protecting — raise at once, lower only as in-flight work drains — now lives in the ramp's own cut-fast, restore-through-the-gates discipline.)

## 4. Scanning is scheduled work

[execution.md](execution.md) establishes what a scan pass *is* — a detached child running the definition in a third mode over a list of log locations, with exactly one alive at a time so the scan directory keeps a single writer. What remains is when to spawn one.

### 4.1 Immediately, because a scan is not competing for a core

A scan pass costs a process, which under a worker ceiling looks like it should cost a task its slot. It does not, and the pass should not be scheduled as though it did: a scan's work is reading transcripts and, for model-graded scanners, waiting on model calls. Transcript decompression and JSON parsing are real CPU but brief and bounded per log; the expensive part is I/O-bound. **A scan pass does not consume a worker slot**, and the pool ceiling counts task workers only.

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

So the attempt ceiling this document set out to choose is **zero**, and there is no number to tune. The same line runs one level down: a sample gets the `retry_on_error` attempts its definition asked for, and past those every further attempt is adjudicated too ([execution.md](execution.md), *Two tiers, not three*). Standing authorization is the pressure valve at both levels — `_steward.md` may admit a class of restart in advance, which is a ruling made earlier rather than an exception to the rule. What bounds recurrence is not a counter but the record: every restart required a ruling, rulings accumulate as precedent, and a class that keeps returning is visible as such rather than hidden inside a retry count that resets. That is the mechanism the design already has for this, and it is strictly more informative than the one it replaces.

This also removes a mechanism rather than adding one. [execution.md](execution.md) treats whole-task retry as a fourth thing standing beside the three recovery tiers; it is really the third tier again — post-completion adjudication, with respawn-and-`resume` as the action a ruling authorizes.

### 5.3 The failure signature classes the anomaly

The forensics still matter, but for proposing rather than deciding. Two bits are free — whether a log exists, and whether the process exited cleanly or died on a signal — and together they separate causes that want genuinely different responses:

| signature | the proposal it supports |
|---|---|
| no log, traceback | "this fails identically every time — here is the error; the definition needs fixing" |
| no log, `SIGKILL`, several tasks at once | "resource exhaustion; reduce the pool and resume" |
| `status="error"` after scoring | "every sample completed and the scorer threw; fix it and re-score rather than re-running" |

Getting this wrong in the other direction was a real risk. A plain *did it write a log?* test collapses the first two rows — and the case it mistakes was, before Layer 2 pruning landed, the likeliest one: OOM during startup leaves no log and reads as permanent. Steward would decline to retry precisely the failure a smaller pool would fix.

Note this is process forensics, not error classification. It reads how a worker died, never what its samples did, so it carries no dependency on the sample-error taxonomy ([execution.md](execution.md), open question 7).

### 5.4 The cost, and where it is paid

A genuinely transient failure — a sandbox provisioning blip, a Docker daemon hiccup — now waits for a human instead of recovering itself. That is the wasted night this design cares most about avoiding, and it is a real cost rather than a theoretical one.

The mitigation is one the design already has: **`_steward.md` pre-authorization.** *"Resume tasks killed by host loss or sandbox provisioning without asking"* is a standing rule granted once, and it converts a night's worth of potential 3am questions into standing authority — the *authorize at 10pm, do not interrogate at 3am* argument, applied to failure rather than to scaling. It also gives the launch-time exchange a concrete item it was missing.

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
