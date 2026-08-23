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
- 🔧 — **test infrastructure** rather than a user-visible feature. Two steps are largely infrastructure, and both are placed earlier than they look like they belong.

Gates **M2**, **M3**, **M4** are [roadmap.md](roadmap.md) §3's milestones, located precisely. **Ship** is not pinned to one of them here; the only thing this document fixes about it is that it falls before §8.

Every step's design pass also has a **test budget** to respect — see §10, which is measured rather than guessed and is the one constraint that binds across all thirty-two steps at once.

## 2. Foundations — nothing spawns a process yet

Five steps that establish state, identity, and the decision function. None of them builds process machinery, which is why they are first: the component most likely to be subtly wrong is the cheapest one here to test exhaustively, and debugging it through a live fleet would be miserable.

### Step 1 — Identifier correlation ✅ **done**

**Delivered** the verified assumption everything downstream inherits: a landed `.eval` maps back to a manifest task. `tests/evalset/test_selection.py` — five cases offline plus one behind `network`, eleven process launches, 33s serial and under 10s with `-n auto` (§10).

The property holds. What sharpened during the work is *which half* needed verifying: a worker matches its selection by recomputing identifiers from its own resolved tasks, so a selection that runs at all already proves capture↔resolve agreement across processes. The unexercised half is resolve↔**log** — the half `reconcile` reads — and upstream touches it only when validating a resume target.

The fixtures are built so a field that silently dropped out of the hash would show up as a **collision** rather than a vacuous pass: fifteen tasks sharing one name and one args set, each differing in exactly one identity-relevant field (args, version, model, model args, model roles, solver chain, generate config, and every execution limit including the `output:<n>` token encoding). Fifteen shapes, fifteen distinct identifiers, fifteen logs, exact match. A second fixture covers the eval-set-level args, where the interesting case is a task whose own limits are *shadowed* by the eval set's — capture merges them into the hash while the log reads `eval.config`, and that merge round-trips.

Three findings went back into the design:

- **`identifier_version` was being dropped.** `EvalSetCapture` carries it; `read_eval_set()` discarded it. A manifest is committed as desired state and outlives an inspect upgrade, so a `TASK_IDENTIFIER_VERSION` bump would have made a finished sweep read as unstarted. Now carried (config §4); refusing on it is step 5.
- **A selection's `log_dir` does not reach pre-boundary writes** (exec §4). A flow worker given only the override drops `flow.yaml` into the definition's log directory. A worker needs both channels — step 6.
- **cwd does not break correlation**, contrary to the hazard upstream's error text warns about, because `definition_command` resolves the definition absolutely and flow anchors task files to the spec. Pinned as a test, so step 6 cannot quietly lose it.

### Step 2 — Workspace and `init` ✅ **done**

**Delivered** a real `steward init`, plus `Workspace` — the one place the layout is expressed, so no later step builds a path from a string. `tests/workspace/`, 19 cases, 4s, no subprocesses but `git init`.

Two decisions taken during the work:

- **The journal marks the workspace, and `init` opens it with a real `initialized` event.** Nothing else could mark it: `.steward/` is disposable, a definition can sit anywhere, and there is no `steward.yaml`. This pulls a minimal envelope and append path forward from step 3, which keeps the fold and the vocabulary where they belong, and it gives `Workspace.find()` something to walk up for (workflow §5.1).
- **The definition placeholder is empty**, and `--type` chooses only its filename — including `hawk.yaml`, since with nothing being authored there was no reason to exclude it. workflow §5's promise of a runnable scaffold was amended rather than left contradicting the code; what a good starting point contains is deferred.

A **skeletal `steward runbook`** ships too, so `AGENTS.md` can take its final shape now instead of naming a command that errors. Not a stub: agent.md §6's prohibitions and §5/§9's reading disciplines are settled, so it carries those for real and marks the operational sections *not yet written*.

One test defect found and fixed: the pre-existing `test_cli_init` invoked `init` with no directory, which wrote a workspace into the repository and appended to its `.gitignore`. `init` was right; the test was not.

### Step 3 — The journal

**Delivers** append-only durable state, and the fold that reads it.

- **Scope.** The JSONL envelope (`ts`, `type`, payload); the event vocabulary as it stands today, extensible later; the fold; tolerance of a corrupt or truncated last line; UTC ISO-8601 everywhere.
- **Refs.** workflow §5.5, §5.6, §5.2, §6.2; exec §10.
- **Done when** a fold over a synthesized journal — including a torn tail — produces the expected state without raising.

The event *vocabulary* grows in nearly every later step. The envelope, the fold and the corruption behaviour do not, and those are what this step fixes.

### Step 4 — Observed state, and the fixtures that prove it 🔧

**Delivers** the read half of convergence — a log directory becomes a structured observation — together with the ability to synthesize such a directory without running an eval.

- **Scope, the generator.** Write `json`-format logs directly with a chosen `task_id` / `task_identifier` / status / sample counts / errors / invalidations. Two logs for one identifier, so supersession has something to act on. Which tests must use a real `.eval` zip instead, and whether that boundary can be enforced rather than remembered.
- **Scope, the reader.** Per identifier, the current log and its predecessors; completeness against epochs; the holes case (a `success` log permanently missing samples); errored and invalidated sample counts; identifiers present in the directory but absent from the manifest.
- **Refs.** testing §2, §1, §7 q3; workflow §2.1, §2.2; exec §5.8, §5.1; config §3, §4.
- **Done when** eight states are producible and read correctly, table-driven: complete-clean, complete-with-errors, complete-but-short, started-never-finished, superseded, invalidated, orphaned, unreadable — plus the mid-run `.eval` case where there is no `header.json`.

**Generator and reader are one step because they are mutually defining.** A log-directory generator cannot be tested except by reading its output, and the reader cannot be tested except against generated input; "eight states are producible" is not a verifiable claim on its own. The generator is nonetheless the higher-leverage half, and it is what makes step 5 a table rather than a fixture suite.

### Step 5 — `reconcile`

**Delivers** the decision function. Pure, no clock, no processes.

- **Scope.** `(manifest, inflight, observed) -> (actions, summary)`. The spawn set; the pool ceiling; the task-major transposition of spawn order; the initial per-worker `max_samples` allocation; the action vocabulary every later step consumes. Refusing a manifest whose `identifier_version` does not match the running inspect, rather than reading its unmatchable identifiers as work not yet started (step 1).
- **Refs.** exec §8.3, §8.1; sched §1.1, §2.1–2.5, §3.1–3.3.
- **Done when** the table covers convergence, idempotence and every state from step 4 — and passes with no process ever started.

Sandbox division is *not* here; it is step 22, blocked upstream. `reconcile` divides one budget at this step and grows a second later.

## 3. Execution — processes, and the machinery that survives them

### Step 6 — Worker spawn

**Delivers** one detached worker running one task, landing one log.

- **Scope.** Write the selection document; spawn detached with a clean environment; apply the static operational overrides (`log_dir`, `max_samples`) **through both channels** — the selection reaches the boundary, the frontend's own `--log-dir` reaches everything written before it (step 1); resolve the definition path absolutely, which is what makes the identifier cwd-independent (step 1); confirm the log lands with the identifier step 1 predicted; confirm worker mode writes no eval-set metadata.
- **Refs.** exec §3, §4, §4.1; config §6.1, §6.2.
- **Done when** N workers under `mockllm` land N correct logs in one shared directory.

### Step 7 — Once-per-run pre-boundary work ⚠ frontend-side

**Delivers** Steward owning a definition's once-per-run setup, so workers do not each repeat it.

- **Scope.** Sort a frontend's pre-boundary work into *correct per worker* (process state), *wasteful* (repeated directory scans, cross-product builds), and *actively unsafe* (concurrent dependency installation into one environment, N× remote secrets calls). The mechanism by which a worker is told to skip — the general form is a way for an external runner to declare it owns the once-per-run half. How Steward learns the resolved environment to hand on; `DefinitionCommand.env` already exists to carry it.
- **Refs.** hawk §6, §10, §12 q6; exec §13 q1; config §2.2.
- **Workaround.** Serialize worker startup behind a lock. Removes the race, keeps the N× cost and the environment mutation. Interim, not an answer.
- **Done when** a fan-out over a frontend definition installs once and resolves secrets once.

**This is general, not a Hawk step, and treating it as Hawk-specific is how it gets built twice.** Flow has the same shape — every worker rescans the log directory and rewrites `flow.yaml`; the cost merely grows with the run instead of corrupting an environment. Hawk is where it turns unsafe: `install_into_current` runs unconditionally on every invocation, so N workers starting together run N concurrent `uv pip install` against one shared environment, and against a PyPI-versioned inspect-ai that can downgrade the interpreter Steward itself is running in.

Nothing here blocks a raw-script or Flow fleet, so **M2 does not wait for it** — but *Hawk* fan-out does, from step 6 onward, which is why it sits at the front of execution rather than in §8's Hawk group.

### Step 8 — In-flight record and liveness

**Delivers** knowing what is running, including during the window where a worker is invisible.

- **Scope.** `intent` written *before* spawn; `launched`; `exited`. Liveness against control discovery. Self-identifying workers — the process table scanned for the selection-document path — which is what covers the pre-boundary window. Reaping.
- **Refs.** exec §7, §7.1, §7.2, §7.3.
- **Done when** a worker killed before its log lands is not respawned, and one killed after is not either.

Step 7 makes this window shorter for the frontends where it is longest, but not shorter than zero, so the two are independent.

### Step 9 — The control channel client

**Delivers** talking to a live worker: read its config, change it, pause it, act on its samples.

- **Scope.** Discovery; read the config view; set `max_samples`; `pause` / `resume`; `invalidate_samples`. Above all: what happens when the worker exits mid-call, which is the normal case rather than the exceptional one.
- **Refs.** exec §8.2, §8.5; workflow §3.1, §10.5; sched §3.2.
- **Done when** every call has a defined behaviour against a worker that has already gone.

This is the mechanism half of concurrency tuning, and it is also what `pause` and adjudication (step 20) need. It lands here, well before the policy that steers it, because it is a wire protocol rather than a judgement and it is testable on its own against a single live worker.

### Step 10 — The run claim

**Delivers** mutual exclusion between Stewards.

- **Scope.** Acquire, hold for the turn, release. Staleness and reaping. What the claim keys on. Behaviour when the clock moves backwards.
- **Refs.** exec §5.7, §10.
- **Done when** a second tend refuses against a held claim and reaps a stale one.

### Step 11 — Fault-injection harness 🔧

**Delivers** the ability to break a run deterministically.

- **Scope.** Definitions that hang, crash or exit at each of the three interesting points (before import completes, before the boundary, after the log lands). Killing a worker at a named state. Breaking the filesystem underneath a run. Waiting on conditions, never on `sleep`. Whether it ships as a tool or stays a fixture.
- **Refs.** testing §4, §7 q1.
- **Done when** the fifteen faults in testing §4 are expressible, and the ones whose subjects exist (steps 6–10) pass.

It lands here rather than at the end because this is the first point where every recovery claim has machinery behind it, and because the harness then grows with each later step instead of being retrofitted onto all of them at once.

### Step 12 — `steward tend` and `steward status`

**Delivers** the turn: observe, decide, act, record.

- **Scope.** The two dispositions of one function; what `status` may not do; the journal events a turn writes; rewriting `status.md` and its two ages; an interrupted turn reconciled by the next.
- **Refs.** exec §8.4, §8.1, §8.3; workflow §8, §3, §5.7.
- **Done when** a repeated tend is a no-op and a tend interrupted at any point is recovered by the following one — both under step 11's harness.

`status.md` is here rather than with the sync because writing it is part of what a turn *is* (workflow §3), and because M2 wants a human-readable snapshot even though nothing syncs yet.

### Step 13 — The timer

**Delivers** the guarantee that the mechanical tend happens whether or not anyone is watching.

- **Scope.** Detect a system scheduler and install; fall back to the ticker; refuse if neither. Disarming. What a missed interval looks like afterwards.
- **Refs.** agent §2, §2.1; exec §8; workflow §7.
- **Done when** a run tends on schedule with no agent attached at all.

### Step 14 — `steward launch`

**Delivers** the entry point: a definition becomes a run.

- **Scope.** Capture; commit the manifest as desired state; report the delta against what is already there; apply the initial concurrency allocation; refusing to launch without an armed timer.
- **Refs.** workflow §7, §2.3; config §4; sched §2.2, §3.1–3.3.
- **Done when** a launch on a fresh workspace and a re-launch over a partial log directory both do the right thing.

**Last in the group, because launch is a composition rather than a component.** In the convergence model it is *capture, commit desired state, arm the timer, tend* — so it needs both of the two steps before it, and building it earlier would mean stubbing the thing it is mostly made of. Tend is testable ahead of it against a hand-committed manifest and step 4's fixtures.

> ### ▸ Gate M2 — run a sweep
>
> A manifest runs as one process per task, with crash isolation and real CPU parallelism. Logs land, nothing is lost, a human reads `status`. It notices nothing: errors are counts, not anomalies. ([roadmap.md](roadmap.md) §3.1)

## 4. Observability and tuning

### Step 15 — The tend summary and the queue

**Delivers** the most-executed interface in the system.

- **Scope.** The summary schema; the queue semantics — at-least-once, acknowledgment as a position rather than per item; what an arriving agent reads as its delta; context cost per tend.
- **Refs.** agent §4, §2.2, §2.3, §8, §5; workflow §5.6.
- **Done when** an agent that missed six tends reads exactly what happened across them, once.

### Step 16 — The tuning loop

**Delivers** concurrency that adapts over the night rather than being fixed at launch.

- **Scope.** The growth signal — rate limits, not saturation — carried in the summary; the envelope as policy; the asymmetric ratchet; retuning through step 9; recording tuning precedent.
- **Refs.** sched §3.2, §3.4, §3.5; workflow §10.5–10.7, §10.10, §10.11, §10.13.
- **Done when** an envelope and a synthesized signal produce the right retune, and the ratchet's asymmetry is a test rather than a comment.

Note the honest limit up front: `mockllm` never returns a 429, so the *end-to-end* growth path stays untested until something emits rate limits on a schedule (testing §6).

### Step 17 — `steward.log` and the sync

**Delivers** durability of the workspace outward, and the record of whether Steward itself worked.

- **Scope.** `steward.log` as the machinery record, separate from the journal's record of decisions, and the rule that says which goes where; the exclusionary sync policy; what leaves and what must not; the rule that the sync never raises.
- **Refs.** workflow §5.7, §9, §9.1–9.4; exec §9.
- **Done when** an unwritable destination degrades a run instead of stopping it.

## 5. Judgement

### Step 18 — Anomalies, proposals, and precedent

**Delivers** errors as structured state with a lifecycle, rather than counts.

- **Scope.** The three levels — instance, class computed from exception type plus raising frame, and proposal grouped by the agent across classes. The state machine and the fold. The window closing on a ruling rather than on a clock. Per-class ruling records, so a bad grouping is recoverable. Precedent lookup and how it travels. Ruling versus policy.
- **Refs.** workflow §12, §12.1, §12.2, §12.8, §6.1; sched §5.1, §5.3; exec §6.7.
- **Done when** synthesized error populations produce stable classes across tends, a ruling on a proposal produces correct per-class records and closes exactly the right window, and precedent surfaces on a recurrence.

**The three levels are one data model, so they are one step.** The seam between them is real — instance and class are computed, proposal is the agent's judgement — but it runs *through* the model rather than between two of them, and building the halves separately would mean designing the same thing twice and getting the second attempt subtly out of line with the first.

### Step 19 — Notification ⚠ upstream 7

**Delivers** the channel that reaches an absent human.

- **Scope.** The four kinds and which two are Steward's alone; Apprise wiring; what a failed notification does.
- **Refs.** workflow §11, §11.1–11.4; agent §7.
- **Workaround.** Steward carries Apprise itself, duplicating `build_apprise` / `init_apprise`. Real, and small.
- **Done when** each kind fires from a synthesized condition.

Before adjudication rather than after, because anomalies need somewhere to escalate and the channel's shape constrains the lifecycle. Building the lifecycle first risks discovering that late.

### Step 20 — Adjudication actions

**Delivers** doing something about a ruling.

- **Scope.** `invalidate_samples` plus respawn with `resume`; approved re-runs scheduled ahead of pending fresh tasks; the task-level attempt ceiling; the conversation's rules.
- **Refs.** exec §6.5, §6.6; sched §5.5; workflow §15; agent §6.
- **Done when** an invalidate-and-resume cycle reuses completed samples and re-runs only the invalidated ones.

### Step 21 — Signoff ⚠ upstream 6

**Delivers** the attestation, and the end of the run.

- **Scope.** The completion criterion and the gate latch; `anomalies.md`; approval terminations; curation into `logs-archive/`; what a stopped run leaves behind.
- **Refs.** workflow §13, §13.1, §14, §14.1, §2.2–2.4.
- **Workaround.** Reimplement the supersession predicate (`latest_completed_task_eval_logs` is private and exported nowhere), accepting that it can drift from `eval_set()`'s definition.
- **Done when** signoff refuses while anomalies are open, and curation moves rather than deletes.

> ### ▸ Gate M3 — walk away
>
> An overnight run tends itself, notices what matters, escalates it, and ends in an attestation. This is the product. ([roadmap.md](roadmap.md) §3.2)
>
> One caveat stated plainly: only the runbook's *bounds* exist at this gate — its operational half is step 29 — so a human is in the loop each session. That is deliberate; see §7.

## 6. Completeness and trust

### Step 22 — Sandbox division ⚠ upstream 9 + 10

**Delivers** a fleet that does not ask a Docker host for `workers × 2 × cores` containers.

- **Scope.** Sandbox type from the manifest; elastic versus host-bound; the division and its floor; redistribution when a worker exits.
- **Refs.** sched §3.6, §3.7; exec §12 items 9, 10.
- **No workaround.** The override does not exist and patching after spawn is too late — the containers are already open.
- **Done when** the arithmetic is unit-tested and the override is exercised against a real Docker sweep.

Positioned by an external dependency rather than by design. It is an M2-on-Docker concern: the arithmetic belongs in step 5 and the override in step 6. **k8s and unsandboxed evals are unaffected** — `k8s_sandbox` does not override `default_concurrency`, so the base `None` applies and its sandboxes are elastic.

### Step 23 — The scan boundary mode ⚠ upstream

**Delivers** a third mode at the `eval_set()` boundary, with Steward as the single writer of scan results.

- **Scope.** How the mode is signalled and what it hands back; taking the scan over with the definition's own `scanner` in hand; what enforces single-writer against a directory that other processes are still landing logs into.
- **Refs.** exec §4.2, §4.3, §5.7.
- **Done when** a scan runs over a synthesized log directory and writes exactly one result set.

Protocol work, and the reason the scanning trio is split: this step is a boundary contract, step 24 is scheduling, step 25 is reporting. They have different dependencies and different failure modes, and one step covering all three would be the largest in the plan by a wide margin.

### Step 24 — Scan passes as scheduled work

**Delivers** scans as detached children a tend spawns and reaps.

- **Scope.** Spawned immediately rather than queued behind a core, because a scan is not competing for one; one log at a time, and where that has to bend; eager drain; a crashed pass as an anomaly rather than a retry; how a scan appears in the in-flight accounting.
- **Refs.** sched §4, §4.1–4.3; exec §4.4.
- **Done when** a sweep's logs drain through scanning across successive tends, and a killed pass surfaces as an anomaly under step 11's harness.

### Step 25 — Scan results as leads

**Delivers** scan output the agent can act on.

- **Scope.** Reporting distributions rather than verdicts; a scan result as a measurement only the agent can read; findings as anomalies that arrive last; collection versus investigation.
- **Refs.** workflow §12.6, §12.3, §12.5; agent §1.
- **Done when** a flat distribution and an outlier distribution over the same scanner produce visibly different leads in the tend summary.

One frontend caveat covering all three: **Hawk rejects `scan:` locally**, so none of this has a Hawk path until §8.

### Step 26 — `scanning.md` and `analysis.md`

**Delivers** what investigation produces, per task, mirrored where the data lives.

- **Scope.** Skeleton rendering; the unprobed count; adjudicating as you go; mirroring into `log_dir` including on S3.
- **Refs.** workflow §12.7, §12.4.
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

### Step 29 — Filling the runbook, and cold pickup

**Delivers** the prompt artifact that determines most of what a user experiences.

The command and `AGENTS.md` already exist (step 2), carrying the bounds that were settled in advance. What is left is the half that had to be learned: the sections the skeleton marks *not yet written*.

- **Scope.** Cadence and how it is armed; cold pickup as a specified, testable procedure; tuning inside the envelope; when to notify; the hard stops. The launch-time pre-authorization exchange. The agent scenarios of testing §5.
- **Refs.** agent §3, §5, §6, §9; testing §5, §7 q2; workflow §10.7.
- **Done when** the three bound scenarios pass — refusing signoff, raising a definition change as a question, notifying with kind `stopped` rather than only speaking into the conversation.

**Deliberately last before ship, and after the M3 gate it appears to belong to.** A runbook is a set of rules for operating machinery, and rules written against machinery nobody has operated are guesses. Steps 15 through 21 each surface rules as a side effect of being built — what the summary makes obvious, what the anomaly lifecycle actually asks of a reader, which escalations turn out to matter — and those accumulate as notes rather than as a document. This step is where they become one.

The split step 2 made keeps the cost of that honest. The **bounds** did not need discovering — they follow from decisions taken in other documents, so they ship from the start and an agent is never unbounded. What waits is the **operational** half, and until it lands, running overnight means a human in the session each time. That is a slower path to the same place, and it is also how the rules get discovered rather than invented.

## 8. Hawk in the pod — after ship

[hawk.md](hawk.md) §11 stages Hawk in three, and only the third is here.

**Stage 0** — read and run a Hawk config — is done. **Stage 1** — Hawk on an ordinary machine, with the full workspace, tend loop, anomalies and signoff — is not separate work: a Hawk config is a definition type, so it falls out of steps 1–29. Its one Hawk-specific obligation is **step 7**, pulled to the front of execution for exactly that reason, plus two local caveats with no design content: `isolation: strict` hard-fails without `HAWK_RUNNER_PATCH_SANDBOX`, which only the Helm template sets, and `scan:` is rejected locally, so steps 23–25 have no Hawk path until this group.

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

**`reconcile` before any process exists (5 before 6).** It is the component most likely to be subtly wrong and the cheapest to test exhaustively. Building the fleet first would mean debugging scheduling logic by watching it.

**Test infrastructure ahead of its subject (4 before 5, 11 before the recovery work).** The fixture generator is what makes `reconcile` a table instead of a fixture suite, and the fault harness lands at the first point where all the recovery claims have machinery behind them — so it grows with each later step rather than being retrofitted onto all of them at once. Both read backwards. Both are the reason the steps after them are cheap.

**Once-per-run ownership as a general step, not a Hawk one (7).** Flow and Hawk hit the same wall from opposite sides — one wastefully, one unsafely — and the ask is one mechanism. Filing it under Hawk is how it ends up implemented twice, so it sits in execution beside the spawn it constrains.

**Launch last in its group (14, after tend and the timer).** It is a composition of both, not a peer of either.

**Concurrency tuning split across the M2 gate (9 and 16).** The mechanism — the control channel client — is a wire protocol testable against one live worker, and `pause` and adjudication need it anyway, so it lands early in execution. The policy — signal, envelope, ratchet — needs the tend summary to carry the signal, so it lands immediately after step 15. Nothing breaks without the policy; the run is only slower, which is why the M2 gate does not wait for it.

**Notification before adjudication (19 before 20).** Anomalies need somewhere to escalate, notification is independently testable, and the channel's shape constrains the lifecycle.

**The runbook last before ship (29).** Argued in §7 above. It is the one step placed by an argument about *how design happens* rather than by a dependency.

Two steps are placed by external dependency rather than by design, and both are called out where they appear: **sandbox division (22)** would sit across steps 5 and 6 if upstream items 9 and 10 existed, and **smoke (27)** would sit beside launch if item 8 did.

## 10. The test budget

Thirty-two steps each adding "just a few end-to-end tests" is how a suite reaches twenty minutes, and by then no one runs it before pushing. Step 1 measured what the cost actually is, so the rest of the plan can be held to a number instead of an intention.

**The unit of cost is the process launch, not the task and not the sample.** Measured on this machine:

| | |
|---|---|
| bare interpreter | 0.03s |
| `import inspect_ai` | 1.3s |
| capture — 2 tasks / 15 tasks | 2.50s / **2.31s** |
| one worker — 2 tasks / 15 tasks | 3.18s / **3.43s** |
| four workers, concurrently | 3.32s |

Fifteen tasks capture *faster* than two; thirteen extra tasks in a worker cost a quarter-second; four concurrent workers cost what one does. **Roughly 3s per launch, and the eval inside it is free.** Half the 3s is importing inspect_ai, which no amount of fixture care will avoid.

Three rules follow, and they are the opposite of the instinct:

1. **Count launches, not tests.** Step 1 runs five non-network cases in 33s because it launches eleven processes. Cutting a *test* saves nothing if it does not cut a launch; folding two assertions into one run saves 3s every time.
2. **Put more into each definition, not more definitions.** Step 1's dimensional fixture covers fifteen identity fields in one worker. Splitting it into fifteen focused tests would have been the same coverage for 45× the cost, and the shared-name construction that makes a dropped field collide is only possible *because* they share a run.
3. **Reach for a subprocess only when the process boundary is the subject.** [testing.md](testing.md) §1's layer 1 — `reconcile` over a synthesized log directory — is microseconds, and it is where most of this plan's correctness lives.

**Which steps genuinely need real workers**: 1 (done), 6–9, 11, and parts of 12, 14, 20, and 23–25 — call it ten. Steps 2–5, 10, 13, 15–19, 21–22, and 26–28 are layer 1 or near it: synthesized state, pure functions, no eval runs at all. **Budget ~12 launches for a layer-2 step**, which is roughly 35s serial and under 10s with `-n auto`. Ten such steps lands the whole suite near five minutes serial and one to two minutes on CI. That is the line; a step that wants more should say why in its design pass.

**Levers held in reserve**, in the order they become worth their complexity: cache captures across tests in a session (deterministic by the contract in configuration.md §4, so it is safe — but xdist gives each worker its own cache, so it pays off only once a single xdist worker runs many tests); share one worker run across several assertions; and, last, split the suite so layer 2 runs on a different cadence than layer 1.

## 11. What this plan does not decide

- **The internals of any step.** Each gets a design pass, starting from its **Refs** line.
- **Sizing.** [roadmap.md](roadmap.md) §3 declines to attempt dates and this document does too.
- **Where ship falls** between the M3 gate and step 29. Only that it falls before §8.
- **The open questions.** Twenty-odd remain across the docs, distributed over the steps that own them. None blocks step 1.
- **The one real gap.** Nothing surfaces an agent's mistake ([roadmap.md](roadmap.md) §7). It belongs to step 18 and is not yet solved there.
