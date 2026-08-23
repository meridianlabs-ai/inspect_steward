# Roadmap

**Status: the cut and the sequence are settled. Dates are not attempted.**

Every other document works a topic to closure regardless of when it ships. This one draws the line once, at the end, so that no earlier decision was distorted by scope pressure.

## 1. Where things actually stand

**Enumeration is built and works on all three definition types.** `read_eval_set()` executes a definition under `INSPECT_EVAL_SET_CAPTURE` and returns a `Manifest` of resolved tasks; `steward tasks` renders it. Detection handles a script, a Flow spec, and a Hawk config, and Hawk's own lowering is captured through Hawk's own entrypoint rather than re-derived.

**The upstream protocol has landed.** Capture mode, selection mode with its resume path, the error-handling overrides that make worker mode mean what it says, and the schema-v2 operational overrides (`log_dir`, `max_samples`).

**Everything else is unbuilt.** There is no `launch`, no `tend`, no workspace, no journal, no anomalies, no signoff — `steward init` is a stub that prints a sentence. The runner is the project.

## 2. Verify one thing before building anything

Every scheduling decision rests on mapping a landed `.eval` back to a manifest task, and Steward's workers resolve **independently** — the same asymmetry that produced the `eval-set.json` `task_id` trap the design already found. Capture, run two workers under a selection, recompute `task_identifier` from each landed log, assert it matches the manifest.

It is an afternoon, it is the assumption everything downstream inherits, and it lands as a **test** rather than a verification ([testing.md](testing.md)) — the property has to keep holding across Inspect upgrades, not merely hold today.

## 3. Four milestones, each a capability rather than a percentage

| | a user can | ships |
|---|---|---|
| **M1 — enumerate** | see what a definition resolves to, before running it | **done** |
| **M2 — run a sweep** | run a manifest as one process per task, with crash isolation and real CPU parallelism | `launch`, `tend`, `status`, the workspace |
| **M3 — walk away** | leave an overnight run and be told only what matters | the timer, journal, anomalies, notification, signoff |
| **M4 — close the loop** | trust the result: scanned, smoke-gated, reusable | scanning, smoke, store publication |

**M3 is the product.** M2 is worth shipping on its own — one task per process buys crash isolation and CPU parallelism that `eval_set()` cannot, and it is the milestone that de-risks everything by proving the protocol at scale — but nobody walks away from it. M4 is what makes a result *trustworthy* rather than merely produced.

### 3.1 M2 — run a sweep

The sequence matters more than the list, and one ordering choice is worth stating: **`reconcile` is built and exhaustively tested before anything spawns a process.** It is a pure function over a synthesized log directory, it is the component most likely to be subtly wrong, and it is the cheapest thing here to test. Building the process machinery first would mean debugging scheduling logic through a fleet.

1. **The workspace.** `init` for real — the directory, `.gitignore`, git detection, the scaffolded definition. Everything else writes here.
2. **The log-directory fixture generator**, which is test infrastructure and belongs before the thing it tests ([testing.md](testing.md)).
3. **`reconcile`**, table-driven: spawn set, ceilings, spawn order, completeness, convergence.
4. **Spawn and reap.** Selection documents, detached workers, the in-flight record with `intent` before spawn, liveness against control discovery, the quarantine rule for the invisible-worker window.
5. **The run claim**, short-lived, with staleness reaping.
6. **`status` and `tend`** as one function with two dispositions.
7. **Fault injection** over the above — the recovery claims are load-bearing and unobservable on a good run.

What M2 deliberately lacks: it does not notice anything. Tasks run, logs land, a human reads `status`. Errors are visible as counts, not as anomalies with a lifecycle.

### 3.2 M3 — walk away

1. **The journal** and the fold, including per-tend `observation` events.
2. **The timer**, and `launch` arming it or refusing.
3. **Anomalies** — classes from exception type plus raising frame, the state machine, proposals spanning classes, precedent.
4. **Notification**, with the four kinds. Depends on **upstream item 7** or on Steward carrying Apprise itself.
5. **Adjudication actions** — `invalidate_samples` plus respawn-with-`resume`.
6. **`signoff`**, `anomalies.md`, the gate latch, and curation into `logs-archive/`.
7. **The agent surface** — the tend summary, collection, `steward runbook`.
8. **The sync**, `steward.log`, and the two ages.

### 3.3 M4 — close the loop

Scanning is the largest piece and the most valuable: a third boundary mode, Steward as single writer, the distribution reporting that makes results triageable, and `scanning.md` / `analysis.md` mirrored into the log directory. The smoke gate and store publication are small by comparison. Note archiving is **not** here — it moved into M3 with signoff, since curation is part of the attestation rather than a later tidy-up.

## 4. What is deferred, and why

| deferred | reason | reconsider when |
|---|---|---|
| **In-flight requeue (tier 2)** | now adjudicated rather than automatic, so tier 3's respawn-with-`resume` reaches the same samples. It saves a respawn, not a decision. | someone measures the respawn cost and minds it |
| **The flow store read half** | a cache. It makes a re-launch cheaper and changes no result. | reuse across projects becomes common |
| **Log cleanup *during* a run** | Steward never deletes, so `cleanup_older_eval_logs` is never called: superseded logs simply accumulate while the run is live, and `latest_completed_task_eval_logs` semantics pick the right one at read time. Curation happens **at signoff** instead ([workflow.md](workflow.md), *Curation is part of the attestation too*), which is the only moment "superseded" is unambiguous. | — |
| **A TUI** | `status` on a repeat, over the same surface. Pleasant, not load-bearing. | someone watches runs often enough to want it |
| **Cross-host runs** | needs worker discovery and a real lease with a fencing token — a different architecture, not a feature | a sweep outgrows one machine |
| **A stateful supervisor** | the timer covers the mechanical floor without any of the wedging problems | measured slot idle a timer cannot absorb |
| **Spend management** | declined outright, not deferred ([workflow.md](workflow.md), *Spend is not Steward's to manage*) | — |
| **Sharding one task across processes** | declined outright ([scheduling.md](scheduling.md), *No sharding*) | — |
| **Batching tasks into one worker** | declined outright — the slot idle it existed to absorb went away with capped pools | — |

## 5. Upstream, ordered by when it bites

[execution.md](execution.md)'s *Changes required in inspect_ai* is the authoritative list. Ordered against the milestones:

| item | needed by | if it does not land |
|---|---|---|
| 7 — notification outside an eval | **M3** | Steward carries Apprise itself, duplicating `build_apprise` / `init_apprise`. Works, and is the one item with a genuine workaround |
| 8 — dataset `limit` override | M4 (smoke) | no smoke gate; a rehearsal would run the whole dataset |
| 5 — early pruning | M2 **at scale** | small sweeps are fine; per-worker memory scales with the whole manifest, so a large one is not. A precondition for launching wide, not for launching |
| 9, 10 — `max_sandboxes` override and sandbox type in the manifest | M2 **on Docker** | k8s and unsandboxed evals are unaffected. On a Docker host the fleet asks for `workers × 2 × cores` containers, which is the one failure mode with no backpressure signal |
| 6 — public directory operations (incl. `archive_dir`) | **M3** | signoff curates into `logs-archive/`, and the predicate it needs — `latest_completed_task_eval_logs` — is private and exported nowhere. Steward reimplements it, which is small but free to drift against the definition of "superseded" that `eval_set()` uses |

**Two items touch M3, and both have workarounds** — Steward can carry Apprise itself, and it can compute supersession itself. Nothing blocks M2 except at scale or on Docker, which is worth knowing: the runner can be built and proven before any of this lands.

## 6. How Hawk meshes

[hawk.md](hawk.md) already stages itself, and the stages line up rather than competing:

- **Hawk Stage 0** — read and run a Hawk config. Done, and it is part of M1.
- **Hawk Stage 1** — run a Hawk config outside the pod. This is **not separate work**: it falls out of M2 and M3, because a Hawk config is a definition type. Its one Hawk-specific obligation is the pre-boundary work that must not be per-worker, which bites the moment Steward spawns a second worker itself.
- **Hawk Stage 2** — Steward inside the pod. The only stage needing a change on Hawk's side (one call site), and the only one that adds architecture — the in-pod timer and the relay RPC surface. It sits after M3, since it is a deployment of the loop rather than part of building it.

## 7. What "done" means for the design

The exit condition for the design work is that an implementation plan can be written from these documents without further discovery. Against that:

- **scheduling.md** — no open questions.
- **workflow.md** — two, neither blocking: how a human replies with no agent in session, and who commits the journal.
- **execution.md** — seven, of which the substantive ones are Flow's pre-boundary cost, cross-host runs, torn manifest reads, and the scanning mode's internals. All are M4-or-later or accepted-as-is.
- **agent.md, testing.md** — the tend summary's encoding, what surfaces an agent's mistake, and how the harness is packaged. Real, and none blocks starting.
- **hawk.md** — six, all Stage 1 or Stage 2.
- **configuration.md** — three, all narrow.

The one genuinely unresolved thing worth naming rather than burying in a list: **nothing surfaces an agent's mistake.** The agent groups error classes into proposals a human agrees to in one word, and per-class ruling records make a bad grouping *recoverable* — deliberately — but nothing brings it to anyone's attention. That is a gap in the design, not in the plan, and it should be closed before M3 ships rather than after.
