# Roadmap

**Status: the cut and the sequence are settled. Dates are not attempted.**

Every other document works a topic to closure regardless of when it ships. This one draws the line once, at the end, so that no earlier decision was distorted by scope pressure.

## 1. Where things actually stand

**Enumeration is built and works on all three definition types.** `read_eval_set()` executes a definition under `INSPECT_EVAL_SET_CAPTURE` and returns a `Manifest` of resolved tasks; `steward tasks` renders it. Detection handles a script, a Flow spec, and a Hawk config, and Hawk's own lowering is captured through Hawk's own entrypoint rather than re-derived.

**The upstream protocol has landed.** Capture mode, selection mode with its resume path, the error-handling overrides that make worker mode mean what it says, and the schema-v2 operational overrides (`log_dir`, `max_samples`).

**The foundations are built, decision included.** `steward init` creates a real workspace; `journal.jsonl` is an append-only record that survives a torn write and an event type from a later version; a log directory reads back as structured state against a manifest, with a fixture generator that produces one without running anything; and `reconcile` turns that state into the set of workers to spawn, in the order to spawn them ([plan.md](plan.md) steps 1–5).

**Steward spawns workers.** `Fleet.spawn` writes a selection document and launches a detached process for one task, and the log it lands recomputes to the identifier that asked for it — so the cycle capture → reconcile → spawn → land → reconcile closes, and the second reconcile has nothing left to do (step 6). A fan-out over a Flow definition keeps that frontend's once-per-run work in each worker's own scratch directory rather than contending over the shared one; Hawk's equivalent cannot be redirected and remains an ask, but it is waste rather than corruption (step 7).

**Steward can now see and steer a fleet, and mutual exclusion is in place.** `resolve_inflight` answers what is running by sweeping the process table for each worker's selection marker, with a record supplying what a process cannot say (step 8); `inspect ctl` reads a live worker's task and retunes its `max_samples` (step 9); and the run claim keeps two Stewards from writing at once, releasing itself when a holder dies and killing one that wedges (step 10). Every recovery claim those rest on is now falsifiable on demand rather than only stated (step 11).

**A workspace can state its own standing rules.** `_steward.md` replaces `policy.md` — YAML front matter Steward acts on unattended above prose an agent interprets — and the rule that keeps it safe is enforced rather than documented: a key the definition could express is refused by name, and anything unrecognised is rejected outright (step 12).

**A run is now supervised.** `steward tend` is the turn — observe, decide, act, record — and `steward status` is the same function with its actions discarded, so the preview cannot describe something other than what happens next. A repeated tend does nothing and an interrupted one is reconciled by the following one, which is what makes the loop safe to put on a timer. It converges: given a committed manifest it spawns the fleet, reaps what died, archives what the definition no longer asks for, stops respawning a task that has stopped getting anywhere, and then leaves the run alone (step 13).

**And it says how the run is going, cheaply enough to ask constantly.** `steward status` renders one line per task — samples done, what is running and queued, how hard the model pool is working, how close the leading sample is to whichever limit will stop it, and what the task is scoring — filled from the log for the denominators and from each running worker's own socket for everything that moves. The header reads a turn used to repeat every ten minutes are now cached against the listing's own `mtime` and `size`, so the cost of a turn tracks what *changed* rather than how large the campaign has grown: 0.862s down to 0.058s at eight hundred settled logs (step 13a).

**And it says what needs a person, once each.** Everything a turn has to report beyond a number is an **item** — a task that stopped getting anywhere, a definition edited since capture, a file that would not parse — carrying a computed owner, a level, and an id chosen so that what re-arms it is what changes it. One list, filtered rather than duplicated, so the terminal and `status.md` cannot drift apart in what they say. Over it sits a verdict: ✅ nothing needs you, ⚠️ something does and work continues, 🛑 nothing is progressing, ⏸ paused. And `steward ack` disposes of the case nothing will fix by itself — a deliberate edit, accepted with a reason, leaves every surface and keeps its record in the journal, which is what stops an attention list from becoming something nobody reads (step 14).

**And now none of it needs anyone to type anything.** `steward timer arm` installs a scheduled tend — a launchd agent, a systemd user timer, or a marked block in the crontab, and a machine with none of the three is told so rather than handed a substitute — so the loop runs on its interval with no agent in session and no terminal open, which is the guarantee the whole arrangement was built around. Arming refuses when a scheduled tend would not inherit the credentials the arming shell holds, because the alternative is a fleet failing to authenticate every ten minutes all night while `status.md` reports it dutifully trying. With a timer comes the brake: `steward pause` stops scheduling and leaves work in flight to finish, recorded in the journal rather than in the disposable cache so that clearing one cannot silently resume an expensive run. And because a scheduler cannot report its own absence, arming writes down what it installed, and any turn that finds no tend for two intervals says the run is **unsupervised** — which catches a crontab somebody rewrote as readily as one Steward removed (step 15).

**What is still missing is the way in and the way it keeps happening.** There is no `launch` to commit desired state — a manifest is written by hand today — and no timer, so a tend happens only when someone or something types it. Pause has no persistence yet, and nothing reaches an absent human: no anomalies with a lifecycle, no channel, no signoff.

## 2. The one thing to verify first — verified

Every scheduling decision rests on mapping a landed `.eval` back to a manifest task, and Steward's workers resolve **independently** — the same asymmetry that produced the `eval-set.json` `task_id` trap the design already found.

**It holds**, across all three definition types, across concurrent workers sharing one flat directory, across a resume, and across a working-directory change. It is a **test** rather than a recorded verification ([testing.md](testing.md)) — the property has to keep holding across Inspect upgrades, not merely hold today — and the fixtures are shaped so that a field dropping out of the identifier surfaces as a collision rather than as a passing round trip ([plan.md](plan.md) step 1).

One thing the work changed: the manifest now records `identifier_version`. It was being dropped, and a manifest is committed as desired state — so an inspect upgrade mid-run would have made every identifier unmatchable and a finished sweep would have read as one that never started.

## 3. Four milestones, each a capability rather than a percentage

The milestones name *capabilities*; [plan.md](plan.md) decomposes them into thirty-six buildable steps and locates the gates precisely within that sequence. Where the two differ on ordering, plan.md is the finer instrument and says why.

| | a user can | ships |
|---|---|---|
| **M1 — enumerate** | see what a definition resolves to, before running it | **done** |
| **M2 — run a sweep** | run a manifest as one process per task, with crash isolation and real CPU parallelism | `launch`, `tend`, `status`, the workspace |
| **M3 — walk away** | leave an overnight run and be told only what matters | the timer, journal, anomalies, notification, signoff |
| **M4 — close the loop** | trust the result: scanned, smoke-gated, reusable | scanning, smoke, store publication |

**M3 is the product.** M2 is worth shipping on its own — one task per process buys crash isolation and CPU parallelism that `eval_set()` cannot, and it is the milestone that de-risks everything by proving the protocol at scale — but nobody walks away from it. M4 is what makes a result *trustworthy* rather than merely produced.

### 3.1 M2 — run a sweep

The sequence matters more than the list, and one ordering choice is worth stating: **`reconcile` is built and exhaustively tested before anything spawns a process.** It is a pure function over a synthesized log directory, it is the component most likely to be subtly wrong, and it is the cheapest thing here to test. Building the process machinery first would mean debugging scheduling logic through a fleet.

1. **The workspace and the journal.** *Done.* `init` for real — the directory, `.gitignore`, git detection, the placeholder definition — plus the append-only record and the fold that everything else reads state from.
2. **The log-directory fixture generator and the observed-state reader.** *Done*, and built together because neither is testable without the other ([testing.md](testing.md)).
3. **`reconcile`**, table-driven: spawn set, ceilings, spawn order, completeness, convergence. *Done.*
4. **Spawn and reap.** *Done*, including the in-flight record with `intent` before spawn and self-identifying workers for the invisible-worker window. Liveness turned out to be the **process table** rather than control discovery: sweeping it for the selection marker costs ~60ms, which made it the primary source rather than a recovery route.
5. **The control channel client.** *Done*, and it is `inspect ctl` rather than an in-process client — one invocation reads the whole fleet, and the CLI already carries the error vocabulary and the provenance that a hand-rolled client would have had to reproduce.
6. **The run claim**, short-lived. *Done.* A kernel lock that a dead holder releases by itself, and a wedged one is killed to release.
7. **Fault injection** over the above. *Done*, and smaller than expected: ten of the sixteen faults were already falsified by tests written for the steps that built their subjects. What it cost was one definition that fails wherever it is told to — and what it bought was a bug, since deleting `.steward/` mid-run had been quietly able to respawn a task over a live worker.
8. **`_steward.md`** — the workspace's standing rules, front matter above prose. *Done*, and ahead of the tend loop rather than after `launch`, because three settings the loop needs are standing properties of a workspace and stubbing a home for each would mean building it twice. It ships one key, `max_workers`, and grows one per step: the rule is that a key the definition could express is refused by name, which took `max_samples` and `headline_metric` out on the way.
9. **`status` and `tend`** as one function with two dispositions, writing `status.md` — then what a turn *says*: the attention list a human reads, the work list an agent drains, and the verdict glyph over both.
10. **The timer**, and then **`launch`** — in that order, because launch is *capture, commit, arm, tend* and therefore a composition of the two items before it rather than a peer of either.
11. **Worker startup at scale** — the identity facets that let inspect prune unselected tasks before constructing them, and the measured memory projection a wide run is judged by. Landed with upstream item 5.

**Gate passed** — a fifteen-task sweep runs a process per task, survives a `kill -9` of one worker, preserves the crashed worker's partial log, escalates a task that stops making progress, and converges. Each worker constructed its own task and not the other fourteen, which is early pruning demonstrated through Steward rather than upstream ([plan.md](plan.md), *Gate M2*).

What M2 deliberately lacks: it does not notice anything. Tasks run, logs land, a human reads `status`. Errors are visible as counts, not as anomalies with a lifecycle.

### 3.2 M3 — walk away

1. **The tend summary and its queue**, which is the agent's whole surface at this milestone. **Built** ([plan.md](plan.md) step 19): three sections led by what needs a decision, `collect` with a cursor over history alone, and `raise` as the state between open and closed.
2. **Human interaction in a detached worker** — a worker parked on a tool approval or an `ask_user`, surfaced as blocked work with a command that attaches to it. **Built** ([plan.md](plan.md) step 20), with **upstream items 12 and 13** landing together as it says they must. Placed second because the summary is what it reports through, and because it is the one condition where walking away does not work: a parked worker holds its slot until a person answers.
3. **The tuning policy** — the growth signal, the envelope, the asymmetric ratchet. Only the judgement is here; the mechanism landed in M2 because `pause` and adjudication needed it anyway.
4. **`steward.log` and the sync**, with the two ages.
5. **Anomalies** — the three levels (instance, class computed from exception type plus raising frame, proposal grouped across classes), the state machine, per-class ruling records, precedent. One data model, so it is built at once.
6. **Notification**, with the four kinds. Depends on **upstream item 7** or on Steward carrying Apprise itself.
7. **Adjudication actions, and the stuck sample** — `invalidate_samples` plus respawn-with-`resume`, and the live counterpart: a sample that stopped moving while nothing failed, surfaced with the directive that would free it. Both are Steward acting on the run under a ruling or a pre-authorization, which is why they are one step.
8. **`signoff`**, `anomalies.md`, the gate latch, and curation into `logs-archive/`.

The **runbook is deliberately not written here** — it is a set of rules for operating machinery, and the rules are discovered by building the items above rather than guessed ahead of them ([plan.md](plan.md) §7).

### 3.3 M4 — close the loop

Scanning is the largest piece and the most valuable — three steps in [plan.md](plan.md), because the boundary mode, the scheduling of passes, and the distribution reporting have different dependencies and different failure modes: a third boundary mode with Steward as single writer, scan passes as detached children a tend spawns and reaps, the distribution reporting that makes results triageable, and `scanning.md` / `analysis.md` mirrored into the log directory. The smoke gate and store publication are small by comparison. Note archiving is **not** here — it moved into M3 with signoff, since curation is part of the attestation rather than a later tidy-up.

## 4. What is deferred, and why

| deferred | reason | reconsider when |
|---|---|---|
| **In-flight requeue** | one of the adjudicated tier's two mechanisms, and the other reaches the same samples. It saves a respawn, not a decision. | someone measures the respawn cost and minds it |
| **The flow store read half** | a cache. It makes a re-launch cheaper and changes no result. | reuse across projects becomes common |
| **Log cleanup *during* a run** | Steward never deletes, so `cleanup_older_eval_logs` is never called: superseded logs simply accumulate while the run is live, and `latest_completed_task_eval_logs` semantics pick the right one at read time. Curation happens **at signoff** instead ([workflow.md](workflow.md), *Curation is part of the attestation too*), which is the only moment "superseded" is unambiguous. | — |
| **Steward inside a Hawk pod** (Hawk Stage 2) | **deferred past ship.** Hawk *local* is not separate work — a Hawk config is a definition type, so Stage 1 falls out of the milestones above. Stage 2 is architecture rather than configuration (a blocking launch, an in-pod driver, a relay RPC surface) and it is the one piece needing a change on someone else's roadmap. | the pod is where campaigns actually run |
| **A TUI** | `status` on a repeat, over the same surface. Pleasant, not load-bearing. | someone watches runs often enough to want it |
| **Cross-host runs** | needs worker discovery and a real lease with a fencing token — a different architecture, not a feature | a sweep outgrows one machine |
| **A stateful supervisor** | the timer covers the mechanical floor without any of the wedging problems | measured slot idle a timer cannot absorb |
| **Spend management** | declined outright, not deferred ([workflow.md](workflow.md), *Spend is not Steward's to manage*) | — |
| **Sharding one task across processes** | declined outright ([scheduling.md](scheduling.md), *No sharding*) | — |
| **Batching tasks into one worker** | declined outright — the slot idle it existed to absorb went away with capped pools | — |
| **Windows** | declined outright ([execution.md](execution.md), *Detachment and the in-flight record*). Detachment, the control socket, and process-table identification are all POSIX mechanisms; a Windows port is a second execution model rather than a flag, and the failure without one is silent | — |

## 5. Upstream, ordered by when it bites

[execution.md](execution.md)'s *Changes required in inspect_ai* is the authoritative list. Ordered against the milestones:

| item | needed by | if it does not land |
|---|---|---|
| 7 — notification outside an eval | **M3** | smaller than it looked: `build_apprise` / `init_apprise` are importable from `inspect_ai.util._notify`, the same reach-around Steward already does for `_eval.evalset` and `_cli.main`, so nothing is duplicated. The ask is *make them public*, not *unblock us* |
| 8 — dataset `limit` override | ~~M4 (smoke)~~ **landed** | schema version 3; the smoke's remaining half is Steward-side |
| 5 — early pruning | ~~M2 **at scale**~~ **landed** | schema version 5, matching on `(registry_name, args_hash)`. Per-worker memory no longer scales with the manifest — measured flat at ~0.27 GiB across a set whose un-pruned worker grew 0.33 → 0.52 GiB. Steward's half (emitting the facets, and the measured projection that replaced the interim guard) is [plan.md](plan.md) step 18 |
| 9, 10 — `max_sandboxes` override and sandbox type in the manifest | M2 **on Docker** — 9 landed, 10 half | the override and `options["max_sandboxes"]` are in at schema version 3; the per-task sandbox type is not, and it is what decides which tasks share a budget. Until then k8s and unsandboxed evals are unaffected, and on a Docker host the fleet still asks for `workers × 2 × cores` containers — the one failure mode with no backpressure signal |
| 6 — public directory operations (incl. `archive_dir`) | **M3** | signoff curates into `logs-archive/`, and the predicate it needs — `latest_completed_task_eval_logs` — is private and exported nowhere. Steward reimplements it, which is small but free to drift against the definition of "superseded" that `eval_set()` uses. Its second half — a batched header read that degrades instead of raising — Steward has already reimplemented, and would keep doing so |
| 11 — epochs reducer in the manifest | never blocking | a reducer-only change reads as complete where `eval_set()` would re-score. Silent rather than loud, which is why it is worth one field |
| 14 — the headline metric in the log header | ~~never blocking~~ **landed** | `Task(headline_metric=…)` declares it, `EvalResults.headline` resolves it, and the fallback is the first metric of the first score. Steward's half is done — `LogAttempt.headline` reads it, and the interim convention step 13a shipped is retired |
| 12, 13 — ACP on in worker mode, and the parked state on the control channel | ~~**M3**~~ **landed** | a detached worker has no path to a human: `approver: human` and `ask_user` land as errored samples in successful logs. Only definitions that ask for human interaction are affected, and they are affected completely. The two must land together — 12 without 13 replaces a loud errored sample with an invisible stall ([plan.md](plan.md) step 20) |

**Two items touch M3, and both have workarounds** — Steward can reach Apprise through a private import, and it can compute supersession itself. Nothing blocks M2 except at scale or on Docker, which is worth knowing: the runner can be built and proven before any of this lands.

## 6. How Hawk meshes

[hawk.md](hawk.md) already stages itself, and the stages line up rather than competing:

- **Hawk Stage 0** — read and run a Hawk config. Done, and it is part of M1.
- **Hawk Stage 1** — run a Hawk config outside the pod. This is **not separate work**: it falls out of M2 and M3, because a Hawk config is a definition type. Its one obligation was the pre-boundary work that must not be per-worker, and step 7 settled what that actually costs: Flow's is redirected into each worker's own scratch directory, and Hawk's is N× startup remote calls and N× redundant installs — waste, not corruption, since uv serializes installs and capture has already run one. Fan-out is not blocked; what remains is an ask on Hawk ([plan.md](plan.md) step 7).
- **Hawk Stage 2** — Steward inside the pod. The only stage needing a change on Hawk's side (one call site), and the only one that adds architecture — the blocking launch, the in-pod driver, and the relay RPC surface. **It lands after ship**, not merely after M3: nothing above waits on it, and the three stages before it de-risk it ([plan.md](plan.md) §8).

## 7. What "done" means for the design

The exit condition for the design work is that an implementation plan can be written from these documents without further discovery. Against that:

- **scheduling.md** — no open questions.
- **workflow.md** — two, neither blocking: how a human replies with no agent in session, and who commits the journal.
- **execution.md** — seven, of which the substantive ones are Flow's pre-boundary cost, cross-host runs, torn manifest reads, and the scanning mode's internals. All are M4-or-later or accepted-as-is.
- **agent.md, testing.md** — the tend summary's encoding, what surfaces an agent's mistake, and how the harness is packaged. Real, and none blocks starting.
- **hawk.md** — six, all Stage 1 or Stage 2.
- **configuration.md** — three, all narrow.

The one genuinely unresolved thing worth naming rather than burying in a list: **nothing surfaces an agent's mistake.** The agent groups error classes into proposals a human agrees to in one word, and per-class ruling records make a bad grouping *recoverable* — deliberately — but nothing brings it to anyone's attention. That is a gap in the design, not in the plan, and it should be closed before M3 ships rather than after.
