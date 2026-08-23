# Testing

**Status: draft. The layering, the fault list, and the fixture strategy are settled. The agent-evaluation tier is sketched.**

This design makes unusually strong claims, and almost all of them are about **failure**:

> Crash recovery is the normal code path. A repeated tend is a no-op. Losing `.steward/` mostly fails in the safe direction. Concurrent Stewards are excluded. Superseded logs are archived, never deleted. An interrupted tend is reconciled by the next one.

Each of those is load-bearing — other decisions were made *because* of them — and none is observable on a run that goes well. **So fault injection is the primary test mode here, not a hardening pass at the end.** A suite that only exercises the happy path would pass on a Steward whose every recovery claim was false.

Two properties of the architecture make this affordable, and the strategy follows from them: `reconcile` is a pure function, so most scheduling correctness needs no processes at all; and `mockllm` makes a real multi-worker run cost milliseconds, so the parts that *do* need processes can have them.

## 1. Four layers

| | what runs | what it covers | cost |
|---|---|---|---|
| **1. `reconcile` over synthesized state** | nothing — a function call | scheduling, ordering, ceilings, completeness, convergence, archive decisions | microseconds |
| **2. real workers under `mockllm`** | processes, real logs | the boundary protocol, identifier correlation, the shared directory, concurrency | seconds |
| **3. fault injection over layer 2** | processes, then broken | every recovery claim above | seconds |
| **4. agent scenarios** | a model | the runbook, cold pickup, the bounds | minutes and money |

The existing suite already establishes layers 1 and 2 — `tests/evalset/fixtures/` holds real definitions run for real against `mockllm/model`, and `tests/evalset/test_read.py` is table-driven over them. Nothing below replaces that; it extends the same pattern.

## 2. Layer 1: the fixture generator is the highest-leverage thing to build

`reconcile(manifest, inflight, log_dir) -> (actions, summary)` takes a directory and returns decisions. Testing it means **synthesizing log directories**, and doing that well converts most of scheduling into a table:

```
given: manifest of 6 tasks, 4 logs (2 success, 1 error, 1 started)
       in-flight record naming 1 live pid
then:  spawn {t3, t5}, archive {}, scan {t1, t2}, anomaly {t4}
```

No clock, no processes, no cleanup. That is the house style's data-driven rule applied to the component where it pays most, and it is why keeping `reconcile` pure was worth insisting on.

**Generating logs without running evals is the whole trick.** Two things make it cheap. Inspect supports a `json` log format as a first-class peer of `.eval` — `list_eval_logs(formats=...)` takes both — and a JSON log is a document a generator can write directly rather than a zip it has to construct. Where a test needs the zip specifically (sample summaries are journalled inside it, and the mid-run "no `header.json`" behaviour is a property of that format), layer 2 produces real ones.

The generator needs to produce, per task: a header with the right `task_id` and `task_identifier`, a status, sample counts against epochs, errored and invalidated samples, and the ability to write *two* logs for one identifier so supersession and archiving have something to act on.

**States worth having in the table**, because each drives a different decision: complete-and-clean, complete-with-errors, complete-but-short (the holes case — a `success` log permanently missing samples), started-never-finished, superseded-by-a-later-attempt, invalidated, orphaned (an identifier absent from the current manifest, which is the archive path), and present-but-unreadable.

## 3. Layer 2: real workers, and one test that has to exist

`mockllm` makes an end-to-end run essentially free, which means the boundary protocol can be tested for real rather than mocked: capture a manifest, write selection documents, spawn workers, let logs land.

**The one that is not optional is identifier correlation.** Everything in `reconcile` rests on mapping a landed `.eval` back to a manifest task, and Steward's workers resolve *independently* — the same asymmetry that produced the `eval-set.json` `task_id` trap the design already found. The test is small: capture, run two workers under a selection, recompute `task_identifier` from each landed log, assert it matches the manifest.

This is spike S1, and it should land as a **test rather than as a one-off verification**. The assumption does not need to be true once; it needs to stay true across Inspect upgrades, and an identifier scheme that quietly changes shape is exactly the kind of breakage a spike-shaped answer would not catch.

Also here: that a flat shared directory survives concurrent workers, that worker mode writes no eval-set metadata, that `fail_on_error=False` and task-retry-off are actually applied, and that resume reuses completed samples while re-running errored ones.

## 4. Layer 3: the faults, and how to inject them deterministically

Each fault below exists to falsify a specific claim, and the claim is what the test asserts.

| fault | claim under test |
|---|---|
| kill a worker after its log lands | reaping is correct; no respawn of finished work |
| kill a worker *before* its log lands | the process table, not the log directory, is what prevents a respawn |
| kill a worker during pre-boundary startup | the invisible-worker window behaves as designed, not as a double-spawn |
| delete `.steward/` mid-run | state rebuilds from the log directory; nothing durable is lost |
| delete `.steward/` *while a worker is starting* | the one case that is **not** safe — a duplicate log lands and reads as an ordinary retry |
| corrupt a journal line | the fold degrades legibly rather than crashing |
| truncate the journal mid-write | the last event is lost, earlier state is intact |
| stale the claim | a stale claim is reaped rather than blocking forever |
| hold the claim | a second tend refuses rather than proceeding |
| race two tends | idempotence — one spawn, not two |
| run the same tend twice | a no-op |
| make `log_dir` unwritable | scheduling stops, notification fires, running workers are left alone |
| fill the disk | the same, without depending on `steward.log` |
| expire log-store credentials | the errors class as *substrate*, and no re-run is proposed |
| jump the wall clock backwards | claim staleness misjudges safely in both directions |

**Two rules keep this suite from becoming flaky**, and they matter more than the list:

- **Inject at decision points, never at wall-clock times.** "Kill the worker once its log has landed" is deterministic; "kill the worker after 2 seconds" is a race that will pass locally and fail in CI. Every row above is expressible as a state the harness waits for.
- **No `sleep`.** Waiting is on a condition — a file appearing, a pid exiting, a barrier releasing. A suite that sleeps is a suite that gets slower every time someone fixes a flake by raising a number.

The design already has a fixture in this shape: `tests/evalset/fixtures/slow_evalset.py` blocks before reaching `eval_set()`, which is precisely the invisible-worker window. The fault harness is that idea generalized — definitions that hang, crash, or exit at chosen points, plus a way to break the filesystem underneath a run.

**One fault is worth building even though nothing in the design depends on it**: a definition that is slow, hangs, and crashes at each of the three interesting points (before import completes, before the boundary, after the log lands). Those three are the whole taxonomy of worker startup failure, and having them as fixtures makes every scheduling test able to include a broken worker cheaply.

## 5. Layer 4: the agent is a prompt artifact, and its quality is the product

Uncomfortable but true: the runbook plus the tend loop determines most of what a user experiences, and none of the layers above touches it. It is also the hardest thing here to test, and worth being honest about rather than describing an aspiration.

What *is* testable is that the agent's rules produce the right behaviour from a prepared state — which is an eval, and Steward is an eval runner, so the tooling exists. A scenario is a synthesized workspace plus a question, scored on what the agent does:

| scenario | passes if |
|---|---|
| cold pickup | reads the runbook, status, and open anomalies before answering anything |
| twelve classes, two causes | proposes two groups, not twelve questions |
| a scan result that is a lead | investigates before proposing, and records what dissipated |
| a flat reward-hacking distribution | notices it, despite there being no outlier |
| a substrate error class | does **not** propose a re-run |
| asked to sign off | refuses |
| asked to "fix" the definition | raises it as a question instead of editing |
| blocked on a decision | notifies with kind `stopped` rather than only asking in the conversation |
| asked "how is it going" | renders the snapshot rather than summarizing it |

These are nondeterministic and cost money, so they belong on a different cadence than the rest — run against a change to the runbook, not on every commit. The last three are the ones worth having first: they are the bounds, and a bound that is only written down is a bound nobody has checked.

## 6. What this strategy cannot reach

Stated so nobody mistakes a green suite for coverage of these:

- **Real rate limits and real adaptive-connection behaviour.** `mockllm` never returns a 429, so the growth signal the tuner reads is untested end to end. A provider stub that emits rate limits on a schedule would close part of it and is not free.
- **Real sandbox exhaustion.** The `max_sandboxes` division protects a host that a test suite must not actually take down. The arithmetic is unit-testable; the consequence of getting it wrong is not.
- **Duration.** Multi-day runs, credential expiry as it actually occurs, log directories with thousands of logs. Long-run behaviour can be *simulated* at layer 1 by synthesizing the end state, which catches scaling bugs in `reconcile` but not accumulation bugs elsewhere.
- **The upstream surface.** Steward depends on capture, selection, the control channel, and identifier stability. Layer 2 catches a break at upgrade time, which is the right place, but only for the paths a test exercises.

## 7. Open questions

1. **Where fault injection lives.** A harness that can kill processes and break filesystems is either a test fixture or a shipped debugging tool. Making it a tool means an operator can reproduce a failure on a real workspace; making it a fixture keeps a dangerous capability out of the package. The second is the safer default and the first is what would actually get used.

2. **Whether agent scenarios run in CI at all.** They cost money and are nondeterministic, so a red result is a conversation rather than a build failure. Running them on runbook changes only is the obvious answer; whether that is often enough to catch a regression introduced elsewhere — a changed tend summary that makes a rule unfollowable — is not clear.

3. **How the `json` fixture path diverges from real `.eval` behaviour.** Cheap generation is worth a lot, but the two formats are not identical where Steward looks closely: sample summaries are journalled inside the zip, and the absence of a mid-run `header.json` is a property of `.eval` specifically. Which tests must use a real zip, and whether that boundary can be enforced rather than remembered, is unresolved.
