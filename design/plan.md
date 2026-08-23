# Implementation plan

**Status: the sequence and the gates are settled. Each step's internals are not — that is what per-step design produces.**

[roadmap.md](roadmap.md) draws the scope line and names four milestones. This document is the next resolution down: **thirty-two steps, each one independently designable, buildable and testable**, in an order where every step's dependencies are behind it.

A step is not a task list. It is a *unit of design*: small enough that its open questions can be closed in one sitting, self-contained enough that it can be tested without the steps after it, and shaped so that finishing it leaves the system in a working state rather than a half-migrated one. The details are deliberately absent — each step gets its own design pass when it comes up, and the **Refs** line says which numbered sections that pass starts from.

## 1. How to read this

Each step carries:

- **Delivers** — the capability, in a sentence.
- **Scope** — the two to five things the design pass has to settle. Not exhaustive.
- **Refs** — numbered sections in the design docs. `exec §7.3` means [execution.md](execution.md) section 7.3.
- **Done when** — the observable condition that lets the next step start. Almost always a test, because almost everything here is testable without a human.

Two markers appear:

- ⚠ **upstream N** — blocked or degraded pending item N of [execution.md](execution.md) §12. The step says whether a workaround exists. A few steps depend on a change in a *frontend* rather than in Inspect; those say so.
- 🔧 — **test infrastructure**, not a user-visible feature. Three steps are pure infrastructure and they are placed where they are on purpose.

Gates **M2**, **M3**, **M4** are [roadmap.md](roadmap.md) §3's milestones, located precisely. **Ship** is not pinned to one of them here; the only thing this document fixes about it is that it falls before §8.

## 2. Foundations — nothing spawns a process yet

Six steps that establish state, identity, and the decision function. None of them starts a worker, which is why they are first: the component most likely to be subtly wrong is the cheapest one here to test exhaustively, and debugging it through a live fleet would be miserable.

### Step 1 — Identifier correlation

**Delivers** the verified assumption everything downstream inherits: a landed `.eval` maps back to a manifest task.

- **Scope.** Capture a manifest, hand-write two selection documents, run both workers, recompute `task_identifier` from each landed log, assert equality with the manifest. Cover the resume path and at least two definition types.
- **Refs.** config §3, §4; testing §3; roadmap §2.
- **Done when** it is a test in the suite, not a note in a file. The property has to keep holding across Inspect upgrades, and an identifier scheme that quietly changes shape is exactly what a one-off verification would miss.

This is the only step that could invalidate the architecture rather than merely complicate it, so it goes first and it is an afternoon.

### Step 2 — Workspace and `init`

**Delivers** a real `steward init`: the directory that everything else writes into.

- **Scope.** The three-plus-one categories and which are committed; `.gitignore`; git detection and what happens without git; the scaffolded definition; refusing to overwrite. The one file Steward must never write.
- **Refs.** workflow §5, §5.1, §5.4, §5.8, §5.9.
- **Done when** `init` produces a workspace that a later step can open, and re-running it is safe.

### Step 3 — The journal

**Delivers** append-only durable state, and the fold that reads it.

- **Scope.** The JSONL envelope (`ts`, `type`, payload); the event vocabulary as it stands today, extensible later; the fold; tolerance of a corrupt or truncated last line; UTC ISO-8601 everywhere.
- **Refs.** workflow §5.5, §5.6, §5.2, §6.2; exec §10.
- **Done when** a fold over a synthesized journal — including a torn tail — produces the expected state without raising.

The event *vocabulary* grows in nearly every later step. The envelope, the fold and the corruption behaviour do not, and those are what this step fixes.

### Step 4 — Log-directory fixture generator 🔧

**Delivers** the ability to synthesize a log directory in any state, without running an eval.

- **Scope.** Write `json`-format logs directly with a chosen `task_id` / `task_identifier` / status / sample counts / errors / invalidations. Two logs for one identifier, so supersession has something to act on. The eight states worth having: complete-clean, complete-with-errors, complete-but-short, started-never-finished, superseded, invalidated, orphaned, unreadable.
- **Refs.** testing §2, §1.
- **Done when** every one of those eight states can be produced in one call.

Test infrastructure ahead of the thing it tests, which reads backwards and is the point: it is the reason steps 5 and 6 can be tables rather than fixtures.

### Step 5 — Observed state

**Delivers** the read half of convergence: a log directory becomes a structured observation.

- **Scope.** Per identifier, the current log and its predecessors; completeness against epochs; the holes case (a `success` log permanently missing samples); errored and invalidated sample counts; identifiers present in the directory but absent from the manifest.
- **Refs.** workflow §2.1, §2.2; exec §5.8, §5.1; config §3, §4.
- **Done when** it is table-driven over step 4's fixtures, including the mid-run `.eval` case where there is no `header.json`.

### Step 6 — `reconcile`

**Delivers** the decision function. Pure, no clock, no processes.

- **Scope.** `(manifest, inflight, observed) -> (actions, summary)`. The spawn set; the pool ceiling; the task-major transposition of spawn order; the initial per-worker `max_samples` allocation; the action vocabulary every later step consumes.
- **Refs.** exec §8.3, §8.1; sched §1.1, §2.1–2.5, §3.1–3.3.
- **Done when** the table covers convergence, idempotence and every state from step 4 — and passes with no process ever started.

Sandbox division is *not* here; it is step 24, blocked upstream. `reconcile` computes a division of one budget at this step and grows a second later.

## 3. Execution — processes, and the machinery that survives them

### Step 7 — Worker spawn

**Delivers** one detached worker running one task, landing one log.

- **Scope.** Write the selection document; spawn detached with a clean environment; apply the static operational overrides (`log_dir`, `max_samples`); confirm the log lands with the identifier step 1 predicted; confirm worker mode writes no eval-set metadata.
- **Refs.** exec §3, §4, §4.1; config §6.1, §6.2.
- **Done when** N workers under `mockllm` land N correct logs in one shared directory.

### Step 8 — Once-per-run pre-boundary work ⚠ frontend-side

**Delivers** Steward owning a definition's once-per-run setup, so workers do not each repeat it.

- **Scope.** Sort a frontend's pre-boundary work into *correct per worker* (process state), *wasteful* (repeated directory scans, cross-product builds), and *actively unsafe* (concurrent dependency installation into one environment, N× remote secrets calls). The mechanism by which a worker is told to skip — the general form is a way for an external runner to declare it owns the once-per-run half. How Steward learns the resolved environment to hand on; `DefinitionCommand.env` already exists to carry it.
- **Refs.** hawk §6, §10, §12 q6; exec §13 q1; config §2.2.
- **Workaround.** Serialize worker startup behind a lock. Removes the race, keeps the N× cost and the environment mutation. Interim, not an answer.
- **Done when** a fan-out over a frontend definition installs once and resolves secrets once.

**This is general, not a Hawk step, and treating it as Hawk-specific is how it gets built twice.** Flow has the same shape — every worker rescans the log directory and rewrites `flow.yaml`; the cost merely grows with the run instead of corrupting an environment. Hawk is where it turns unsafe: `install_into_current` runs unconditionally on every invocation, so N workers starting together run N concurrent `uv pip install` against one shared environment, and against a PyPI-versioned inspect-ai that can downgrade the interpreter Steward itself is running in.

Nothing here blocks a raw-script or Flow fleet, so **M2 does not wait for it** — but *Hawk* fan-out does, from step 7 onward, which is why it sits at the front of execution rather than in §8's Hawk group.

### Step 9 — In-flight record and liveness

**Delivers** knowing what is running, including during the window where a worker is invisible.

- **Scope.** `intent` written *before* spawn; `launched`; `exited`. Liveness against control discovery. Self-identifying workers — the process table scanned for the selection-document path — which is what covers the pre-boundary window. Reaping.
- **Refs.** exec §7, §7.1, §7.2, §7.3.
- **Done when** a worker killed before its log lands is not respawned, and one killed after is not either.

Step 8 makes this window shorter for the frontends where it is longest, but not shorter than zero, so the two are independent.

### Step 10 — The control channel client

**Delivers** talking to a live worker: read its config, change it, pause it, act on its samples.

- **Scope.** Discovery; read the config view; set `max_samples`; `pause` / `resume`; `invalidate_samples`. Above all: what happens when the worker exits mid-call, which is the normal case rather than the exceptional one.
- **Refs.** exec §8.2, §8.5; workflow §3.1, §10.5; sched §3.2.
- **Done when** every call has a defined behaviour against a worker that has already gone.

This is the mechanism half of concurrency tuning, and it is also what `pause` and adjudication (step 21) need. It lands here, well before the policy that steers it, because it is a wire protocol rather than a judgement and it is testable on its own against a single live worker.

### Step 11 — The run claim

**Delivers** mutual exclusion between Stewards.

- **Scope.** Acquire, hold for the turn, release. Staleness and reaping. What the claim keys on. Behaviour when the clock moves backwards.
- **Refs.** exec §5.7, §10.
- **Done when** a second tend refuses against a held claim and reaps a stale one.

### Step 12 — Fault-injection harness 🔧

**Delivers** the ability to break a run deterministically.

- **Scope.** Definitions that hang, crash or exit at each of the three interesting points (before import completes, before the boundary, after the log lands). Killing a worker at a named state. Breaking the filesystem underneath a run. Waiting on conditions, never on `sleep`. Whether it ships as a tool or stays a fixture.
- **Refs.** testing §4, §7 q1.
- **Done when** the fifteen faults in testing §4 are expressible, and the ones whose subjects exist (steps 7–11) pass.

It lands here rather than at the end because this is the first point where every recovery claim has machinery behind it, and because the harness then grows with each later step instead of being retrofitted onto all of them at once.

### Step 13 — `steward launch`

**Delivers** the entry point: a definition becomes a run.

- **Scope.** Capture; commit the manifest as desired state; report the delta against what is already there; apply the initial concurrency allocation; the first spawn; refusing to launch without an armed timer.
- **Refs.** workflow §7, §2.3; config §4; sched §2.2, §3.1–3.3.
- **Done when** a launch on a fresh workspace and a re-launch over a partial log directory both do the right thing.

### Step 14 — `steward tend` and `steward status`

**Delivers** the turn: observe, decide, act, record.

- **Scope.** The two dispositions of one function; what `status` may not do; the journal events a turn writes; an interrupted turn reconciled by the next.
- **Refs.** exec §8.4, §8.1, §8.3; workflow §8.
- **Done when** a repeated tend is a no-op and a tend interrupted at any point is recovered by the following one — both under step 12's harness.

### Step 15 — The timer

**Delivers** the guarantee that the mechanical tend happens whether or not anyone is watching.

- **Scope.** Detect a system scheduler and install; fall back to the ticker; refuse if neither. Disarming. What a missed interval looks like afterwards.
- **Refs.** agent §2, §2.1; exec §8; workflow §7.
- **Done when** a run tends on schedule with no agent attached at all.

> ### ▸ Gate M2 — run a sweep
>
> A manifest runs as one process per task, with crash isolation and real CPU parallelism. Logs land, nothing is lost, a human reads `status`. It notices nothing: errors are counts, not anomalies. ([roadmap.md](roadmap.md) §3.1)

## 4. Observability and tuning

### Step 16 — The tend summary and the queue

**Delivers** the most-executed interface in the system.

- **Scope.** The summary schema; the queue semantics — at-least-once, acknowledgment as a position rather than per item; what an arriving agent reads as its delta; context cost per tend.
- **Refs.** agent §4, §2.2, §2.3, §8; workflow §5.6.
- **Done when** an agent that missed six tends reads exactly what happened across them, once.

### Step 17 — The tuning loop

**Delivers** concurrency that adapts over the night rather than being fixed at launch.

- **Scope.** The growth signal — rate limits, not saturation — carried in the summary; the envelope as policy; the asymmetric ratchet; retuning through step 10; recording tuning precedent.
- **Refs.** sched §3.2, §3.4, §3.5; workflow §10.5–10.7, §10.10, §10.11, §10.13.
- **Done when** an envelope and a synthesized signal produce the right retune, and the ratchet's asymmetry is a test rather than a comment.

Note the honest limit up front: `mockllm` never returns a 429, so the *end-to-end* growth path stays untested until something emits rate limits on a schedule (testing §6).

### Step 18 — `status.md`, `steward.log`, and the sync

**Delivers** the human-readable surface, and the record of whether Steward itself worked.

- **Scope.** `status.md` and its two ages; `steward.log` as the machinery record, separate from the journal's record of decisions; the exclusionary sync policy; what leaves and what must not; the rule that the sync never raises.
- **Refs.** workflow §5.7, §9, §9.1–9.4; exec §9.
- **Done when** an unwritable destination degrades a run instead of stopping it.

## 5. Judgement

### Step 19 — Anomalies

**Delivers** errors as structured state with a lifecycle, rather than counts.

- **Scope.** Classing from exception type plus raising frame; instance / class / proposal; the state machine; the window closing on a ruling rather than on a clock; the fold.
- **Refs.** workflow §12, §12.1, §12.2; sched §5.1, §5.3; exec §6.7.
- **Done when** synthesized error populations produce stable classes across tends, and a ruling closes exactly the right window.

### Step 20 — Notification ⚠ upstream 7

**Delivers** the channel that reaches an absent human.

- **Scope.** The four kinds and which two are Steward's alone; Apprise wiring; what a failed notification does.
- **Refs.** workflow §11, §11.1–11.4; agent §7.
- **Workaround.** Steward carries Apprise itself, duplicating `build_apprise` / `init_apprise`. Real, and small.
- **Done when** each kind fires from a synthesized condition.

Before adjudication rather than after, because anomalies need somewhere to escalate and the channel's shape constrains the lifecycle. Building the lifecycle first risks discovering that late.

### Step 21 — Adjudication actions

**Delivers** doing something about a ruling.

- **Scope.** `invalidate_samples` plus respawn with `resume`; approved re-runs scheduled ahead of fresh tasks; the task-level attempt ceiling; the conversation's rules.
- **Refs.** exec §6.5, §6.6; sched §5.5; workflow §15; agent §6.
- **Done when** an invalidate-and-resume cycle reuses completed samples and re-runs only the invalidated ones.

### Step 22 — Proposals and precedent

**Delivers** the grouping that makes adjudication one question instead of thirty.

- **Scope.** Grouping classes into proposals; per-class ruling records so a bad grouping is recoverable; precedent lookup and how it travels; ruling versus policy.
- **Refs.** workflow §12.1, §12.8, §6.1; sched §5.3.
- **Done when** a ruling on a proposal produces correct per-class records, and precedent surfaces on a recurrence.

### Step 23 — Signoff ⚠ upstream 6

**Delivers** the attestation, and the end of the run.

- **Scope.** The completion criterion and the gate latch; `anomalies.md`; approval terminations; curation into `logs-archive/`; what a stopped run leaves behind.
- **Refs.** workflow §13, §13.1, §14, §14.1, §2.2–2.4.
- **Workaround.** Reimplement the supersession predicate (`latest_completed_task_eval_logs` is private and exported nowhere), accepting that it can drift from `eval_set()`'s definition.
- **Done when** signoff refuses while anomalies are open, and curation moves rather than deletes.

> ### ▸ Gate M3 — walk away
>
> An overnight run tends itself, notices what matters, escalates it, and ends in an attestation. This is the product. ([roadmap.md](roadmap.md) §3.2)
>
> One caveat stated plainly: the runbook is not written yet (step 29), so at this gate a human is in the loop each session. That is deliberate — see §7.

## 6. Completeness and trust

### Step 24 — Sandbox division ⚠ upstream 9 + 10

**Delivers** a fleet that does not ask a Docker host for `workers × 2 × cores` containers.

- **Scope.** Sandbox type from the manifest; elastic versus host-bound; the division and its floor; redistribution when a worker exits.
- **Refs.** sched §3.6, §3.7; exec §12 items 9, 10.
- **No workaround.** The override does not exist and patching after spawn is too late — the containers are already open.
- **Done when** the arithmetic is unit-tested and the override is exercised against a real Docker sweep.

Positioned by an external dependency rather than by design. It is an M2-on-Docker concern and would otherwise sit near step 13. **k8s and unsandboxed evals are unaffected** — `k8s_sandbox` does not override `default_concurrency`, so the base `None` applies and its sandboxes are elastic.

### Step 25 — Scanning ⚠ upstream

**Delivers** the third boundary mode, and Steward as single writer.

- **Scope.** The scan mode itself; a scan as a detached child spawned and reaped by a tend; one log at a time and where that bends; eager drain; a crashed pass as an anomaly rather than a retry; reporting distributions rather than verdicts.
- **Refs.** exec §4.2–4.4; sched §4, §4.1–4.3; workflow §12.3, §12.5, §12.6.
- **Done when** a sweep's logs drain through scanning with the results surfacing as leads in the tend summary.

The largest piece in the plan and the most valuable. One frontend caveat: **Hawk rejects `scan:` locally**, so this has no Hawk path until §8.

### Step 26 — `scanning.md` and `analysis.md`

**Delivers** what investigation produces, per task, mirrored where the data lives.

- **Scope.** Skeleton rendering; the unprobed count; adjudicating as you go; mirroring into `log_dir` including on S3.
- **Refs.** workflow §12.7, §12.4, §12.6.
- **Done when** both files exist per task and reach the log directory.

### Step 27 — Smoke gate ⚠ upstream 8

**Delivers** the rehearsal before the sweep.

- **Scope.** The dataset `limit` override; the Steward-side wall-clock cap (*not* a passed-through `time_limit`, which is in the identifier); what a smoke failure blocks.
- **Refs.** workflow §7.1; exec §12 item 8.
- **No workaround.** Without `limit`, a rehearsal runs the whole dataset.
- **Done when** a smoke run truncates, caps, and gates the real launch.

### Step 28 — Store read and publish

**Delivers** reuse across runs, and publication as an act of signoff.

- **Scope.** The read half (a cache); publication gated on the attestation, not on landing; configuration.
- **Refs.** exec §5.3–5.6; workflow §13.2.
- **Done when** publication happens at signoff and never before.

> ### ▸ Gate M4 — close the loop
>
> The result is trustworthy: scanned, smoke-gated, reusable. ([roadmap.md](roadmap.md) §3.3)

## 7. The agent surface

### Step 29 — `steward runbook`, `AGENTS.md`, and cold pickup

**Delivers** the prompt artifact that determines most of what a user experiences.

- **Scope.** What the runbook says — cadence, the never-sign-off rule, smoke-first, what to escalate, how to tune inside the envelope, render-don't-replace. Cold pickup as a specified, testable procedure. The launch-time pre-authorization exchange. The agent scenarios of testing §5.
- **Refs.** agent §3, §5, §6, §9; testing §5, §7 q2; workflow §10.7.
- **Done when** the three bound scenarios pass — refusing signoff, raising a definition change as a question, notifying with kind `stopped` rather than only speaking into the conversation.

**Deliberately last before ship, and after the M3 gate it appears to belong to.** A runbook is a set of rules for operating machinery, and rules written against machinery nobody has operated are guesses. Steps 16 through 23 each surface rules as a side effect of being built — what the summary makes obvious, what the anomaly lifecycle actually asks of a reader, which escalations turn out to matter — and those accumulate as notes rather than as a document. This step is where they become one.

The cost is honest and worth naming: between the M3 gate and this step, running overnight means a human in the session each time, working from the design docs rather than from a runbook. That is a slower path to the same place, and it is also how the rules get discovered rather than invented.

## 8. Hawk in the pod — after ship

[hawk.md](hawk.md) §11 stages Hawk in three, and only the third is here.

**Stage 0** — read and run a Hawk config — is done. **Stage 1** — Hawk on an ordinary machine, with the full workspace, tend loop, anomalies and signoff — is not separate work: a Hawk config is a definition type, so it falls out of steps 1–29. Its one Hawk-specific obligation is **step 8**, pulled to the front of execution for exactly that reason, plus two local caveats with no design content: `isolation: strict` hard-fails without `HAWK_RUNNER_PATCH_SANDBOX`, which only the Helm template sets, and `scan:` is rejected locally, so step 25 has no Hawk path until this group.

**Stage 2 — Steward inside the pod — lands after ship.** It is architecture rather than configuration, it is the one stage needing a change on someone else's roadmap, and the three stages before it de-risk it. Nothing above waits on it.

### Step 30 — Blocking launch and exit codes

**Delivers** `steward launch --wait-signoff`: a process that holds the pod open for the whole lifecycle Steward defines.

- **Scope.** Workers as ordinary children rather than detached, since the runner is PID 1 and detaching buys nothing; the in-pod timer as a second driver of the same `reconcile`, sharing the run claim with an external tend; the exit-code mapping.
- **Refs.** hawk §7, §7.1, §8; exec §11.3.
- **Done when** the mapping is exercised end to end, including the non-obvious row: **terminal without signoff exits 0**, because a non-zero exit trips `backoffLimit` and the restarted runner resurrects the eval.
- **Also settles** hawk §12 q2 — how long a parked run waits before its deadline fires, and what the timeout writes.

### Step 31 — The relay surface

**Delivers** driving a pod-resident Steward from outside.

- **Scope.** A loopback TCP server inside the pod — Inspect's control channel cannot be borrowed, since the bind is hardcoded `AF_UNIX` with a PID-derived path and a `SO_PEERCRED` check, and `acp_server`'s `int → TCP 127.0.0.1:<port>` path is the precedent. A `steward --remote` client shaped by the relay's limits: **one connection per command**, because a five-session-per-principal cap and a 900-second idle timeout that keepalives do not reset both punish a pooled client. Recording a relay signoff as claimed-but-unverified, and declining to add a shared-token scheme below a real gate.
- **Refs.** hawk §9, §9.1, §9.2.
- **Done when** a full tend cycle runs over `hawk attach` without approaching either limit.

### Step 32 — The Hawk call site ⚠ Hawk-side

**Delivers** Hawk invoking Steward instead of `eval_set_from_config`.

- **Scope.** A runner type or flag at the one call site in `run_eval_set.py`. Not Steward's code.
- **Refs.** hawk §11; config §8.3.
- **Also settles** hawk §12 q4 — whether a resumed Hawk job is the same Steward run, which decides whether the journal survives a pod restart at all.

## 9. Why this order

Six ordering choices are load-bearing. The rest of the sequence is just dependencies.

**`reconcile` before any process exists (6 before 7).** It is the component most likely to be subtly wrong and the cheapest to test exhaustively. Building the fleet first would mean debugging scheduling logic by watching it.

**Test infrastructure ahead of its subject (4 before 5–6, 12 before the recovery work).** The fixture generator is what makes observed state and `reconcile` tables instead of fixtures, and the fault harness lands at the first point where all the recovery claims have machinery behind them — so it grows with each later step rather than being retrofitted onto all of them at once. Both read backwards. Both are the reason the steps after them are cheap.

**Once-per-run ownership as a general step, not a Hawk one (8).** Flow and Hawk hit the same wall from opposite sides — one wastefully, one unsafely — and the ask is one mechanism. Filing it under Hawk is how it ends up implemented twice, so it sits in execution beside the spawn it constrains.

**Concurrency tuning split across the M2 gate (10 and 17).** The mechanism — the control channel client — is a wire protocol testable against one live worker, and `pause` and adjudication need it anyway, so it lands early in execution. The policy — signal, envelope, ratchet — needs the tend summary to carry the signal, so it lands immediately after step 16. Nothing breaks without the policy; the run is only slower, which is why the M2 gate does not wait for it.

**Notification before adjudication (20 before 21).** Anomalies need somewhere to escalate, notification is independently testable, and the channel's shape constrains the lifecycle.

**The runbook last before ship (29).** Argued in §7 above. It is the one step placed by an argument about *how design happens* rather than by a dependency.

Two steps are placed by external dependency rather than by design, and both are called out where they appear: **sandbox division (24)** would sit near step 13 if upstream items 9 and 10 existed, and **smoke (27)** would sit near step 13 if item 8 did.

## 10. What this plan does not decide

- **The internals of any step.** Each gets a design pass, starting from its **Refs** line.
- **Sizing.** [roadmap.md](roadmap.md) §3 declines to attempt dates and this document does too.
- **Where ship falls** between the M3 gate and step 29. Only that it falls before §8.
- **The open questions.** Twenty-odd remain across the docs, distributed over the steps that own them. None blocks step 1.
- **The one real gap.** Nothing surfaces an agent's mistake ([roadmap.md](roadmap.md) §7). It belongs to step 22 and is not yet solved there.
