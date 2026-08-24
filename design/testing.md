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

**The cost column is per *process*, not per test**, and that turns out to be the only number worth planning around. Measured in step 1: a capture costs ~2.5s and a worker ~3.2s regardless of what is inside them — fifteen tasks capture no slower than two, and four concurrent workers cost what one does, because roughly half of every launch is `import inspect_ai`. So a layer-2 test is priced by how many processes it starts, and layer 1 is free by comparison. [plan.md](plan.md) §10 turns that into a budget across the thirty-six steps; the design consequence here is that **layer 1 should absorb everything it can**, and layer 2 reserved for cases where the process boundary is itself the subject.

The existing suite already establishes layers 1 and 2 — `tests/evalset/fixtures/` holds real definitions run for real against `mockllm/model`, and `tests/evalset/test_read.py` is table-driven over them. Nothing below replaces that; it extends the same pattern.

## 2. Layer 1: the fixture generator is the highest-leverage thing to build

`reconcile(manifest, inflight, log_dir) -> (actions, summary)` takes a directory and returns decisions. Testing it means **synthesizing log directories**, and doing that well converts most of scheduling into a table:

```
given: manifest of 6 tasks, 4 logs (2 success, 1 error, 1 started)
       in-flight record naming 1 live pid
then:  spawn {t3, t5}, archive {}, scan {t1, t2}, anomaly {t4}
```

No clock, no processes, no cleanup. That is the house style's data-driven rule applied to the component where it pays most, and it is why keeping `reconcile` pure was worth insisting on.

**Generating logs without running evals is the whole trick.** Two things make it cheap. Inspect supports a `json` log format as a first-class peer of `.eval` — `list_eval_logs(formats=...)` takes both — and a JSON log is a document a generator can write directly rather than a zip it has to construct. And a whole synthetic log is under a kilobyte: `write_eval_log` takes an `EvalLog` built in memory, so the generator constructs models rather than hand-rolling JSON, and the format stays upstream's problem.

The generator needs to produce, per task: a header with the right `task_id` and `task_identifier`, a status, sample counts against epochs, errored and invalidated samples, and the ability to write *two* logs for one identifier so supersession and archiving have something to act on.

**One thing makes the identifiers trustworthy rather than merely present.** `task_identifier`'s `EvalLog` branch reads a handful of `EvalSpec` fields and nothing else, so one function builds the `EvalSpec` that both the manifest row and every log for a task derive from — and the two agree *by construction* rather than by a literal repeated in two places. What that cannot prove is capture↔log correlation, since both sides run the same branch; that claim is layer 2's and step 1 made it.

**The mid-run `.eval` case turned out to be layer 1 too.** An `.eval` being written has no `header.json` and the reader falls back to `_journal/start.json` — genuinely a property of the zip, and reproducible as a zip with exactly one member holding `{version, eval, plan}`. Ten lines, no eval. That leaves sample summaries as the only case that needs a real one, and see §7 q3 for why nothing is likely to reach for it.

**States worth having in the table**, because each drives a different decision: complete-and-clean, complete-with-errors, complete-but-short (the holes case — a `success` log permanently missing samples), started-never-finished, superseded-by-a-later-attempt, invalidated, orphaned (an identifier absent from the current manifest, which is the archive path), and present-but-unreadable.

## 3. Layer 2: real workers, and one test that has to exist

`mockllm` makes an end-to-end run essentially free, which means the boundary protocol can be tested for real rather than mocked: capture a manifest, write selection documents, spawn workers, let logs land.

**The one that is not optional is identifier correlation.** Everything in `reconcile` rests on mapping a landed `.eval` back to a manifest task, and Steward's workers resolve *independently* — the same asymmetry that produced the `eval-set.json` `task_id` trap the design already found. The test is small: capture, run two workers under a selection, recompute `task_identifier` from each landed log, assert it matches the manifest.

This is spike S1, and it should land as a **test rather than as a one-off verification**. The assumption does not need to be true once; it needs to stay true across Inspect upgrades, and an identifier scheme that quietly changes shape is exactly the kind of breakage a spike-shaped answer would not catch.

Also here: that a flat shared directory survives concurrent workers, that worker mode writes no eval-set metadata, that `fail_on_error=False` and task-retry-off are actually applied, and that resume reuses completed samples while re-running errored ones.

**Real workers touch real user state, so this layer redirects the home directory.** A frontend Steward launches may write outside the tree it was pointed at — Flow records a global `last_log_dir` under `platformdirs.user_data_dir`, which a test run would rewrite to a pytest temp path and leave there. The isolation is autouse rather than opt-in, because the tests that do this are the ones nobody thinks of as touching a home directory, and it exempts the network tests, which need the credentials the real home holds. It also pins `UV_CACHE_DIR` to the real cache: both frontends shell out to uv before reaching the boundary, and a moved home takes uv's cache with it, which triples the flow tests for no isolation anyone wanted. That a *production* run has the same side effect is a separate question, answered separately ([configuration.md](configuration.md), *Inspect Flow specs*): a worker cannot give up its home directory, and a test has no business having one.

**The substitute home has to be short.** Inspect binds its control socket at `<data dir>/inspect_ai/control/<pid>.sock`, and a pytest temp path (`/private/var/folders/../pytest-of-user/pytest-93/popen-gw6/home0`) pushes that past the 104-byte `sun_path` limit. Inspect's response is a warning and an eval that runs without a control surface, so the failure surfaces as a control-channel test that finds nothing, with no mention of a path anywhere. The fake home is therefore made directly under `/tmp` rather than by `tmp_path_factory` — the one place a test fixture in this suite trades tidiness for a platform limit.

## 4. Layer 3: the faults, and how to inject them deterministically

Each fault below exists to falsify a specific claim, and the claim is what the test asserts.

| fault | claim under test | falsified by |
|---|---|---|
| kill a worker after its log lands | reaping is correct; no respawn of finished work | `worker/test_faults.py` |
| kill a worker *before* its log lands | the process table, not the log directory, is what prevents a respawn | `worker/test_inflight.py` |
| kill a worker during pre-boundary startup | the invisible-worker window behaves as designed, not as a double-spawn | `worker/test_inflight.py`, `worker/test_spawn.py` |
| delete `.steward/` mid-run | a worker carries its identity in its own environment, so it is still seen running and nothing is scheduled over it | `worker/test_faults.py` |
| delete `.steward/` *while a worker is starting* | it dies at the boundary, having not yet read the document — and a dead worker is respawned exactly once | `worker/test_faults.py` |
| corrupt a journal line | the fold degrades legibly rather than crashing | `workspace/test_journal.py` |
| truncate the journal mid-write | the last event is lost, earlier state is intact | `workspace/test_journal.py` |
| kill a tend holding the claim | the lock goes with the process; the next tend takes it with nothing to reap | `workspace/test_claim.py` |
| wedge a tend holding the claim | it is killed and the claim taken, rather than blocking the run until morning | `workspace/test_claim.py` |
| hold the claim | a second tend refuses rather than proceeding | `workspace/test_claim.py` |
| race two tends | idempotence — one spawn, not two | *step 13* |
| run the same tend twice | a no-op | *step 13* |
| make `log_dir` unwritable | scheduling stops, notification fires, running workers are left alone | *steps 13, 24* |
| fill the disk | the same, without depending on `steward.log` | *steps 13, 24* |
| expire log-store credentials | the errors class as *substrate*, and no re-run is proposed | *step 23* |
| jump the wall clock backwards | a holder looks young, so the tend refuses rather than killing it | `workspace/test_claim.py` |

**The third column is the point of the table.** A fault list with no test beside it is an intention, and the whole argument of this document is that intentions about failure are the ones that quietly stop being true. A row naming a step rather than a module is work not yet possible; a row naming neither would be a hole.

Worth noting what the column shows: **ten of these were falsified before any of them was written as fault injection**, because each arrived as the natural test of the step that built its subject. Building the harness as a step of its own was still right — it found the `.steward/` rows above, which had been wrong in this table since it was written — but the shape it took was three pieces of shared machinery, not a suite.

**Two rules keep this suite from becoming flaky**, and they matter more than the list:

- **Inject at decision points, never at wall-clock times.** "Kill the worker once its log has landed" is deterministic; "kill the worker after 2 seconds" is a race that will pass locally and fail in CI. Every row above is expressible as a state the harness waits for.
- **No `sleep`.** Waiting is on a condition — a file appearing, a pid exiting, a barrier releasing. A suite that sleeps is a suite that gets slower every time someone fixes a flake by raising a number.

**Both rules are a mechanism rather than a discipline**, which is what keeps them from decaying. `tests/evalset/fixtures/faulty_evalset.py` fails wherever it is told to — `pre` (the module body, before `eval_set()`), `run` (inside a sample), or `post` (after `eval_set()` returns and before the process exits) — and each point is a two-file handshake: the definition writes `<point>.reached` on arrival, and `hang` waits for the test to write `<point>.go`. So *inject at a decision point* is the only thing the fixture can do, and a test that wants a worker held somewhere waits for a marker instead of estimating how long it takes to get there.

Those three points are the whole taxonomy of a worker's lifetime, and `post` is the only way to observe a landed log whose process is still alive at all.

**There is deliberately no `slow`**, despite it appearing in the taxonomy above for a while. A delay is a race and a gate is a state: a test that wants *still running* holds the gate, and one that wants *eventually finishes* releases it. Removing it also took the last wall-clock waits out of the worker suite, which had been paying a fixed sleep per test to keep an eval alive long enough to ask it something.

**A held worker is a leak waiting to happen, and it needs answering twice.** Workers are detached so that a run survives the tend that started it, and that guarantee does not know it is in a test: nothing kills them when pytest exits, and one waiting on a marker a finished run will never write spins until somebody finds it in `ps`. So an autouse fixture sweeps the test's own workspace at teardown — the production scan, scoped exactly as a tend scopes it, because a leak is by definition what no `finally` caught and catching it must not depend on remembering. And a hold watches for its spawner disappearing and exits, which covers the case teardown cannot: pytest killed, or Ctrl-C. That second check is a *state* rather than a timeout — the ppid captured at startup, compared against the current one, so a subreaper adopting the orphan reads the same as init doing it.

**The fixture is a fixture, not a shipped tool** (§7 q1, closed). The safety argument — keeping a process-killing, filesystem-breaking capability out of the package — is real but secondary. The decisive one is that nothing in `src/` would import any of it: the two pieces that looked most like a tool turned out to be an eval definition and a `chmod`.

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

1. ~~**Where fault injection lives.**~~ *Closed: a fixture.* See §4 — the safety argument was the weaker one, and what settled it is that nothing in the package would import any of it.

2. **Whether agent scenarios run in CI at all.** They cost money and are nondeterministic, so a red result is a conversation rather than a build failure. Running them on runbook changes only is the obvious answer; whether that is often enough to catch a regression introduced elsewhere — a changed tend summary that makes a rule unfollowable — is not clear.

3. **How the `json` fixture path diverges from real `.eval` behaviour.** *Mostly resolved, and enforced structurally rather than remembered.* Two of the three divergences stopped mattering. The mid-run missing `header.json` is synthesizable as a one-member zip, so it needs no real eval (§2). And sample summaries are out of reach by construction: **the observation layer reads headers only** — never a sample, never a summary — so nothing built on it can drift into the part of the format the fixtures do not model. A test that wants summaries has to go around `observe_logs` to get them, which is visible in review rather than silent.

    The third divergence is new and is about speed rather than correctness: a `json` header read is fully synchronous inside its `async def`, where an `.eval` header read genuinely awaits on I/O. So the fixture path can prove the reader *right* and can never say anything about how fast it is — a local benchmark over generated logs would measure zero benefit from concurrent reads and be entirely misleading about the case that matters, which is `.eval` on S3.
