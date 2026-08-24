# Implementation plan

**Status: the sequence and the gates are settled. Each step's internals are not — that is what per-step design produces.**

[roadmap.md](roadmap.md) draws the scope line and names four milestones. This document is the next resolution down: **thirty-six steps, each one independently designable, buildable and testable**, in an order where every step's dependencies are behind it.

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

Every step's design pass also has a **test budget** to respect — see §10, which is measured rather than guessed and is the one constraint that binds across all thirty-six steps at once.

## 2. Foundations — nothing spawns a process yet

Five steps that establish state, identity, and the decision function. None of them builds process machinery, which is why they are first: the component most likely to be subtly wrong is the cheapest one here to test exhaustively, and debugging it through a live fleet would be miserable.

### Step 1 — Identifier correlation ✅ **done**

**Delivered** the verified assumption everything downstream inherits: a landed `.eval` maps back to a manifest task. `tests/evalset/test_selection.py` — originally five cases offline plus one behind `network`, eleven process launches, 33s serial and under 10s with `-n auto` (§10). Step 6 took two of them: the concurrent fleet and the resume were the production shape, and they belong on the production spawn. What is left here is the case production never has — one worker running a whole fifteen-task manifest.

The property holds. What sharpened during the work is *which half* needed verifying: a worker matches its selection by recomputing identifiers from its own resolved tasks, so a selection that runs at all already proves capture↔resolve agreement across processes. The unexercised half is resolve↔**log** — the half `reconcile` reads — and upstream touches it only when validating a resume target.

The fixtures are built so a field that silently dropped out of the hash would show up as a **collision** rather than a vacuous pass: fifteen tasks sharing one name and one args set, each differing in exactly one identity-relevant field (args, version, model, model args, model roles, solver chain, generate config, and every execution limit including the `output:<n>` token encoding). Fifteen shapes, fifteen distinct identifiers, fifteen logs, exact match. A second fixture covers the eval-set-level args, where the interesting case is a task whose own limits are *shadowed* by the eval set's — capture merges them into the hash while the log reads `eval.config`, and that merge round-trips.

Three findings went back into the design:

- **`identifier_version` was being dropped.** `EvalSetCapture` carries it; `read_eval_set()` discarded it. A manifest is committed as desired state and outlives an inspect upgrade, so a `TASK_IDENTIFIER_VERSION` bump would have made a finished sweep read as unstarted. Now carried (config §4); refusing on it is step 5.
- **A selection's `log_dir` does not reach pre-boundary writes** (exec §4). A flow worker given only the override drops `flow.yaml` into the definition's log directory. A worker needs both channels — step 6.
- **cwd does not break correlation**, contrary to the hazard upstream's error text warns about, because `definition_command` resolves the definition absolutely and flow anchors task files to the spec. Pinned as a test — which now runs against the real spawn, where losing it would matter.

### Step 2 — Workspace and `init` ✅ **done**

**Delivered** a real `steward init`, plus `Workspace` — the one place the layout is expressed, so no later step builds a path from a string. `tests/workspace/`, 19 cases, 4s, no subprocesses but `git init`.

Two decisions taken during the work:

- **The journal marks the workspace, and `init` opens it with a real `initialized` event.** Nothing else could mark it: `.steward/` is disposable, a definition can sit anywhere, and `_steward.md` is optional (step 12). This pulls a minimal envelope and append path forward from step 3, which keeps the fold and the vocabulary where they belong, and it gives `Workspace.find()` something to walk up for (workflow §5.1).
- **The definition placeholder is empty**, and `--type` chooses only its filename — including `hawk.yaml`, since with nothing being authored there was no reason to exclude it. workflow §5's promise of a runnable scaffold was amended rather than left contradicting the code; what a good starting point contains is deferred.

A **skeletal `steward runbook`** ships too, so `AGENTS.md` can take its final shape now instead of naming a command that errors. Not a stub: agent.md §6's prohibitions and §5/§9's reading disciplines are settled, so it carries those for real and marks the operational sections *not yet written*.

One test defect found and fixed: the pre-existing `test_cli_init` invoked `init` with no directory, which wrote a workspace into the repository and appended to its `.gitignore`. `init` was right; the test was not.

### Step 3 — The journal ✅ **done**

**Delivered** the append-only record as something that can be read after a crash: a safe concurrent append, a damage-tolerant reader, and `summarize()` — the first fold, and the shape every later one takes. `tests/workspace/test_journal.py`, 14 cases, 2.2s.

Three decisions, each the opposite of how Steward treats a selection document — and deliberately, because **a selection is input being validated before it changes what runs, while a journal is history being read**:

- **An unrecognised event type reads as a generic event.** A workspace outlives the Steward that wrote it, so refusing a file because a later version put something new in it would be the wrong trade. Selection documents forbid extras for exactly the opposite reason.
- **Damage costs one line, never the file.** `read_journal` returns what it parsed *and* what it could not, with line numbers; a missing journal is an empty history rather than damage. Nothing raises, and nothing is swallowed — where a complaint goes is `steward.log`, step 22.
- **Only `initialized` is typed.** The other eight types in workflow §5.6 arrive with the steps that write them; in particular the five anomaly types stay with step 23, which keeps the three-level model as one piece rather than transcribing a table ahead of the code that gives it meaning.

One thing measured rather than assumed, because the first version of the test could not have failed: **splitting a record across two writes** (payload, then newline) loses about a quarter of the events under four concurrent writers. Size is not the hazard and neither is the platform — a buffered whole-line append is safe on a local filesystem. So the guard is one `os.write` of a pre-built line, and the test's docstring records what it does and does not catch.

### Step 4 — Observed state, and the fixtures that prove it 🔧 ✅ **done**

**Delivered** the read half of convergence — `observe_logs` turns a log directory into attempts grouped by identifier, `observe_tasks` reads those against a manifest — and `tests/_logs.py`, which synthesizes such a directory without running anything. `tests/evalset/test_observe.py`, 23 cases, 1.2s, **zero process launches**.

Four decisions:

- **The split is at the filesystem boundary, not at the manifest.** `observe_logs` does the I/O and knows nothing about what was supposed to run, which is what lets it serve `logs-archive/` and the flow store — neither of which has a manifest to compare against. Completeness is a second, pure function, so step 5's inputs stay pure.
- **Four states, one carrying a reason.** `complete`, `incomplete`, `missing`, `orphaned` — deliberately the domain of the action vocabulary rather than a taxonomy of log conditions. Every incomplete task takes the same action, so *why* (`started`, `short`, `invalidated`, `error`, `cancelled`, `no_results`) is reporting material. Complete-clean and complete-with-errors are one state and a count, for the same reason: both mean *do not spawn*, and the errored samples are step 23's queue.
- **Attempts order by `eval.created`, and the latest *successful* one is current.** Both halves diverge from upstream, which sorts by mtime and takes the newest whatever its status. Mtime is not intrinsic — restoring a log from the archive rewrites it, and the archive is a cache the design intends to hit — while `created` survives even the mid-run header fallback. And a deliberate re-run that errored must not displace a good result (exec §5.8).
- **An unreadable log costs one log, never the directory** — step 3's rule, applied to the other thing Steward reads on a schedule. Not hypothetical: a worker's zip has no readable header for the moment between creation and its first journal entry, and a tend that raised on that is a tend that never ran.

Two things measured or read rather than assumed. **Header reads are concurrent**, because an `.eval` header read genuinely awaits on I/O where a `json` one is synchronous inside its `async def` — so the fixtures can prove the reader right and can say nothing about its speed, and a local benchmark over them would argue for exactly the wrong thing. And **the mid-run `.eval` case is layer 1 after all**: a zip with one member, `_journal/start.json`, reproduces it in ten lines.

Two findings, both recorded upstream (exec §12, items 6 and 11): `read_eval_log_headers_async` raises on any single unreadable file, so a scheduled reader cannot use it; and the capture manifest discards the epochs *reducer*, so a reducer-only change reads as complete where `eval_set()` would re-score. A third went to workflow §2.1 — **renaming the definition file orphans the entire run**, since `task_file` is in the identifier, and the workspace's fixed definition name is what makes Steward immune.

**Generator and reader were one step because they are mutually defining.** Neither is testable alone. What made the generator trustworthy is that one `_eval_spec()` builds the `EvalSpec` both the manifest row and every log derive from, so they agree on the identifier by construction rather than by a literal repeated twice.

### Step 5 — `reconcile` ✅ **done**

**Delivered** the decision function — `(manifest, inflight, observed, pool, paused) -> (actions, queued, summary)`, pure. `tests/schedule/`, 29 cases, 1.4s, no process ever started.

Four decisions:

- **The state enum *is* the action vocabulary.** Step 4's four states map one to one onto what to do — `complete` leave it, `incomplete` resume it, `missing` spawn it, `orphaned` report it — so `reconcile` has no classification logic of its own. Every incomplete task resumes whatever went wrong; there is deliberately no branch on the reason, because resume reuses exactly the samples worth keeping.
- **A manifest from a different inspect raises rather than reports.** Unmatchable identifiers make every task read *missing* and every log read *orphaned*, so a finished sweep would re-run from scratch — and a summary carrying that looks entirely normal. A returned flag asks every future consumer to remember to check it; an exception cannot be forgotten. Step 13's `tend` catches it in one place and says `steward launch`.
- **`archive` is not in the vocabulary yet.** Orphans are named in the summary and nothing acts on them. An action nobody can execute is a stub, and a tend computing twelve archive actions every ten minutes and running none of them is noise. It arrives with the launch gate (step 16) and signoff's sweep (step 26).
- **The queue holds `SpawnWorker`s, not identifiers** — the same decision deferred, so *approved re-runs go first* (step 25) becomes a sort rather than a second code path.

The crash-recovery case is the one worth naming: **a live worker and one that died mid-run leave exactly the same thing in the log directory** — a `started` log with no results. Only the in-flight record separates them, which is why `reconcile` takes it, and getting it wrong means either double-spawning a live task or never recovering a dead one. It has its own test.

Two things changed in the design while building it.

**`max_samples` was not in the capture manifest**, so scheduling.md's *yield to whatever the definition set* silently could not happen — a definition asking for 60 got Steward's 40 and nobody was told, and the log records only the effective value so there was no read-side workaround. Fixed upstream: `options` now carries `max_samples`, no schema bump needed since `options` is a free-form dict.

Reading it exposed a second thing, which is why `resolve_max_samples` has **three** sources rather than two: Steward's default and an operator's explicit instruction are not the same claim. Collapsing them means either the fallback outranks a definition or a typed `--max-samples` loses to one, and neither shows up in the resulting number. So `Pool.max_samples` is `int | None`, where `None` means *no preference*, and the order is operator → definition → 40.

**The ceiling stopped being derived from cores.** §2.2's argument — *past core count another process buys no parallelism* — misreads the workload: a worker is on the CPU in bursts and waiting on a model API in between, so ten workers on four cores is ordinary. One process per task buys isolation of those bursts, not core saturation, which makes the ceiling a resource guard rather than a parallelism budget. It is now a flat **10**, matching where `eval_set()`'s own `max_tasks` starts, and expected to be raised. That deleted `available_cores()` and its cgroup reading — the cgroup lie is still real, but it now surfaces only inside Docker's `default_concurrency()` at step 27, where §3.6 records it. Recoverable from `f1e1822` if that step wants it. The formula also needed the running-workers term: `min(ceiling − running, pending)`, since eight running and five pending under a ceiling of ten is two spawns, not five.

Sandbox division is *not* here; it is step 27, blocked upstream. `reconcile` divides one budget at this step and grows a second later.

## 3. Execution — processes, and the machinery that survives them

### Step 6 — Worker spawn ✅ **done**

**Delivered** `Fleet.spawn(action)` — a `SpawnWorker` becomes a selection document and a detached process, and the log it lands recomputes to the identifier that asked for it. `tests/worker/test_spawn.py`, 8 cases, 13s, 12 process launches; half of them need none.

The loop now closes end to end: capture → reconcile → spawn → land → reconcile, and the second reconcile returns no actions.

Four decisions:

- **`spawn` builds its own command.** `log_dir` has to reach both the frontend, which writes before `eval_set()` is ever called, and the selection, which reaches the boundary — and a caller that passes one and forgets the other gets a flow worker dropping `flow.yaml` into the definition's log directory (step 1 measured it). Taking a prepared `DefinitionCommand` would leave that gap open forever; building it inside is what makes the two channels one parameter. `max_samples` needs only the selection: nothing before the boundary runs samples.
- **Each worker's output goes to a file** beside its selection, merged and appended. `DEVNULL` is what a Hawk worker dying in `uv pip install` would leave behind, and `PIPE` is a deadlock waiting for a reader who has exited. It is the only account of the pre-boundary window (exec §7.3) and it is cheap — a `plain` display leaves twenty legible lines per worker.
- **The handle carries its `Popen`.** Detached is not reparented: `start_new_session` detaches from the terminal, and a worker stays its spawner's child until the spawner exits. Dropping the reference is worse than keeping it — `Popen.__del__` files the child in `subprocess._active`, where the next `Popen()` reaps it, so a later `waitpid` fails intermittently. The field is documented as valid only while this process lives; step 8's record is what survives.
- **The eval set id is resolved once, by the caller.** `resolve_eval_set_id` is a one-line delegation to upstream's `eval_set_id_for_log_dir`, which reads-or-mints-and-writes `.eval-set-id` idempotently and refuses a conflicting id. Worker mode never touches that file (exec §4.1), so keeping it out of `spawn` is what keeps *once, at run start* a visible constraint rather than an accident.

Three things read rather than assumed. **The control server needs nothing at spawn**: it is on by default and binds a per-pid socket, so N workers cannot collide, and `INSPECT_EVAL_CTL_SERVER` is a CLI envvar that a definition script never consults — no ambient environment can switch a worker's control surface off. **A worker prints no launch handoff**, because the handoff and the ctl pointer are armed by the CLI; Steward knows the pid because it spawned it. And **`INSPECT_EVAL_SET_CAPTURE` must be stripped**, not merely left unset — capture and selection are mutually exclusive, so a worker inheriting an exported capture path would die at startup instead of running.

Two corrections to execution.md. **§7.3's "passes that path in argv" is not implementable**: argv is Steward's to compose only for a raw script, and a Flow or Hawk worker is that platform's CLI, where an extra positional argument is a parse error — exactly the two types whose pre-boundary window is long enough to need self-identification. The marker is the environment (`INSPECT_EVAL_SET_SELECTION` already *is* the path), read with `psutil.Process.environ()`, verified readable for a same-user detached child. And **§7.1's `exited` row overstated the exit status**: a tend exits in seconds while its workers run for hours, so by the time anything observes one gone it has been reparented and its status reaped by init. *Observed gone* is what the record can promise, which is why the reap action carries nothing else.

One decision the step forced: **Steward is POSIX-only, declined rather than deferred.** `start_new_session` is silently ignored on Windows, so a worker there stays attached to its console and dies with it — the central guarantee failing quietly. A port needs `creationflags` plus a second story for AF_UNIX control sockets, `getsid`, process-table identification, and the signals step 11 sends, which is a second execution model rather than a flag. Declared in the package metadata and refused at the spawn, because a classifier is metadata `pip` does not enforce and a platform that appears to work is worse than one that does not.

One finding for later: **a definition declaring a scanner cannot be run by Steward today** — worker mode rejects scanners outright (exec §4.2), so the worker exits with an upstream `PrerequisiteError`. The capture manifest records `scanners` in `options`, so `launch` can refuse early with a better message. Step 16, and answered properly at step 28.

### Step 7 — Once-per-run pre-boundary work ✅ **done**

**Delivered** a flow fan-out that keeps its once-per-run work out of the run's log directory, and an accurate account of what Hawk's fan-out actually costs. `tests/worker/test_flow_worker.py`, one case, three process launches.

**The skip mechanism the step was scoped around was the wrong thing to reach for**, because a frontend change is not available: Steward runs `inspect_flow` and `hawk` as released. What *is* available is aiming the work somewhere harmless with controls both already ship — and for Flow that reaches two thirds of it, using nothing but `flow run` options.

**Every flow worker gets a scratch directory of its own** (`.steward/workers/<stem>/`) through the frontend `--log-dir` channel, while the selection carries the run's. All three pre-boundary items key off `spec.log_dir`, so `flow.yaml` and the requirements snapshot stop being N concurrent writes to two shared paths, and `find_existing_logs` scans an empty directory instead of one that grows all run. Safe because the scan's result never reaches `eval_set()` — `_runner/run.py` uses it only for the display and the store. **The two channels were never meant to carry the same value**, which is a correction to exec §4 and config §8.2, and reads had already been doing the right thing.

Measured, one flow worker against a 3.0s plain-`eval_set()` baseline: **4.36s → 4.14s** on an empty log directory, **4.79s → 4.21s** with 150 logs in it. The growing term is gone (it was ~2.4ms per log, so ~12s per worker at five thousand), and the residual ~1.1s is **entirely the requirements freeze** — two `uv` shell-outs that cannot be skipped from outside, only redirected. The original attribution spread that 1.1s across resolution, `flow.yaml`, and the scan; measurement says otherwise, which sharpens what is still worth asking Flow for.

**Two other things came with it.** `--no-store-read --no-store-write` makes exec §5.4's "workers run with `store=none`" true, having been unimplemented — which leaves the store inert until step 33 hands both halves to Steward, and that is the right way round: what workers were writing were unattested claims. And `--set execution_type=inproc` fixes a **live bug**: a spec declaring `execution_type: venv` sent `flow run` to `venv_launch`, so every worker built a virtualenv and ran the eval in a grandchild — the pid Steward recorded, its discovery entry, and every liveness check keyed on them all naming the wrong process. That is the same instruction as hawk's `--direct`, so it is now stated once in config §8 as a rule for every adapter rather than twice as a detail.

**Hawk got no code and one correction, which is the honest outcome.** It exposes no `--log-dir` to redirect and no flag to skip, so nothing here reaches it. But the "actively unsafe" framing was wrong in three ways: **uv takes an exclusive lock on the target environment** (verified — two concurrent installs, one logs `Waiting to acquire exclusive lock`, both succeed), so concurrent installs serialize; **capture has already installed** into Steward's interpreter before `launch` spawns anything, so worker installs are satisfied no-ops; and the **downgrade hazard is an N=1 problem** that `steward tasks` triggers, not a fan-out one. What is genuinely N× is the remote reads — secrets and provider env — and those produce *process state*, so redirection cannot help and the ask needs a second half: the frontend must be able to **report** what it resolved, for `DefinitionCommand.env` to carry.

So **the serialization workaround is declined rather than deferred**, and *Hawk fan-out is not blocked*: it works, at the cost of N× startup remote calls and N× redundant installs. Waste and a throttling risk, not corruption.

**Review found three consequences of the two overrides, and each was taken a different way.** The `inproc` override also drops what a spec declares in `dependencies` and `python_version`, because the venv is where Flow applies them — so Steward now **warns** when a YAML spec asks for one, and does not refuse: the environment usually satisfies the spec, since the author provisioned it, and the check cannot see a declaration that arrives through an `include:` or a Python spec anyway (config §8). The scratch `--log-dir` leaves Flow's global `last_log_dir` pointing at a disposable directory, which is **accepted**: correcting it means writing another tool's user-global file, and the only knob that would redirect it is `HOME`, which a worker needs (config §8.2, exec §13 q1, now a second small ask of Flow). And the same write was reaching the *real* home from the test suite, which is neither accepted nor warned about — an autouse fixture redirects `HOME` and `XDG_DATA_HOME` for every non-network test (testing.md §3).

### Step 8 — In-flight record and liveness ✅ **done**

**Delivered** `.steward/inflight.jsonl` and `resolve_inflight(record, workers_dir)` — what was spawned, resolved against what is running, into the `InFlight` that step 5's `reconcile` already took. `tests/worker/test_inflight.py`, 18 cases, 14s, **three** eval workers launched plus four `python -c` processes that cost nothing; twelve of the cases are a table over a stubbed scan.

**One measurement inverted the design.** The plan treated the process scan as a recovery route — the record answers, and the table rebuilds one that was lost. Measured on a table of 756 processes, `process_iter` costs 52ms and `environ()` over all 544 same-user ones costs 6ms: **~60ms**, affordable on every tend. So the scan became the liveness source and the record shrank to what a process cannot tell you — that a spawn was attempted, and which task, attempt, and command a running worker belongs to. The scan is then *exercised*, where a path taken only after a crash is not.

**Review caught the consequence, which is that a marker in the environment is inherited.** Every subprocess an eval starts — a sandbox's `docker`, a frontend's `uv` — carries the same `INSPECT_EVAL_SET_SELECTION`, so the marker is a subtree test rather than a process test. The first draft collapsed matches by selection path and kept whichever came last, which is a child: the worker's socket lost, its pid wrong for anything step 11 would signal, and a task held open indefinitely by an orphan that outlived it. Two rules fix it. The scan keeps only the **ancestor-most** match of each subtree, which is unambiguous while the worker lives, since its own parent is the tend that spawned it and carries no marker. And where the record knows a pid, **that pid decides** — a selection whose only surviving process is a leftover child reads as departed. So the path match did not replace the pid; it made the pid safe to use. Neither half answers alone: a recycled pid fails the path test, and a descendant fails the pid test. One degradation survives and is pinned by a test rather than left implicit — with the record lost there is no pid to check against, so an orphaned child of a dead worker reads as the worker.

Three decisions:

- **`Fleet.spawn` writes both records**, rather than the caller bracketing it. Intent-before-spawn is a safety property, and safety properties belong inside the mechanism they protect — the same argument spawn.py already makes for its two log directories. `Fleet.inflight` is required for the same reason: an argument that can be forgotten eventually is.
- **`RunningWorker` split into two types.** It could not express an `intent` with no `launched`, which has no pid, and inventing one would put a number in the record that never named a process — so `DepartedWorker.pid` is `int | None`. The other half is the interesting one: `RunningWorker.socket` is `None` until the worker binds one, which makes the pre-boundary window **a state a summary can report** rather than something inferred from two absences.
- **The JSONL mechanics moved to `_util/jsonl.py`**, shared with the journal, which wants the same three properties for the same reason: both are read after something went wrong. Durability is where they differ, so `append_event` takes `sync` — `True` for the one file nothing can rebuild, `False` for the one the next resolve reconstructs.

Two corrections to execution.md, both from §7.1 claims the scan invalidated. **Process start time is no longer a correctness mechanism** — it was there to defeat pid recycling, which the selection-path match now does better; it stays as provenance. And **`launched` cannot carry the control socket**: it is bound at the `eval_set()` boundary, long after the spawn returns, so it is discovered rather than recorded. §7.2 narrowed to match — discovery supplies the socket of a worker already known to be running, not the liveness answer.

**And a finding that matters more than the step: since step 7, every test had been running without a control surface.** The autouse home-directory fixture put the fake home under `tmp_path_factory`, and inspect binds its control socket at `<data dir>/inspect_ai/control/<pid>.sock` — which under `/private/var/folders/../pytest-of-user/pytest-93/popen-gw6/home0` exceeds the 104-byte `sun_path` limit. Inspect warns and runs on without one, so nothing failed; the socket assertion here is the first thing that asked. The fake home is now made under `/tmp` (testing.md §3). Step 9 is entirely control-channel client work and would have hit this head-on with no clue where to look.

### Step 9 — The control channel client ✅ **done**

**Delivered** `_worker/ctl.py` — reading the fleet and retuning a worker, with every outcome against a worker that has already gone as a value rather than an exception. `tests/worker/test_ctl.py`, 16 cases, 18s, one launch.

**The step got smaller because the client is `inspect ctl`.** The plan was an in-process `httpx` client over the AF_UNIX routes; the measurement that killed it is that **`inspect ctl task list` spans every live process in one call**, reading them concurrently and stamping each row with its pid. A fleet read is 1 × 1.3s, not N × 1.3s, which was the whole case for going in-process. What comes with the CLI is worth more than the round trip: a closed error-kind vocabulary, the busy/retry policy (the control server shares the eval's event loop, so **a timeout means alive-but-busy and a connection error means gone** — reads retry, mutations never do), and `--author` / `--reason` provenance. Steward runs it as `[sys.executable, "-m", "inspect_ai._cli.main", "ctl", …]`, the same in-this-interpreter pattern `definition_command` uses for the frontends. No `httpx`, no `anyio`, no async core, no hand-rolled classification.

Three decisions:

- **Primitives are `inspect ctl`'s; compositions are Steward's** — the doctrine this step exists to settle, now in exec §8.2. It also answers the question that prompted it: Steward's CLI stays small not because it is forbidden verbs but because most worker operations are single directives against single targets, which an agent runs itself against a CLI that already has an agent output contract. What Steward builds commands for is invalidate-and-resume, requeue, and a fleet-wide latch — operations with preconditions nobody should re-derive from a prompt.
- **Only two functions, because only two have callers.** `list_tasks(pids)` and `task_config(...)`. `pause` was in the scope line and is **not** here: exec §6.4 declines automatic model pause, workflow §3.1 puts `steward pause` on the scheduling side with no channel involved, and its one real consumer is step 25's composition — which is not obviously served by a single-target wrapper. Writing it now would be exactly the speculative surface the doctrine argues against.
- **Steward models the envelopes, because upstream does not.** Checked first: every producer returns `dict[str, Any]`, the routes are `-> Any`, and `_cli/ctl` holds no `BaseModel` or `TypedDict` either — the contract is held by upstream's tests, not by a type Steward could import. (The three `TypedDict`s in `_control/requeue.py` are the exception, and step 25 should reuse them.) So Steward validates the fields it depends on at the boundary, `extra="allow"`. That earned its place within the hour: `persisted` is **per-knob** (`{"max_samples": true}`), not the bool the read's `null` suggested, and the boundary is where that surfaced instead of a `KeyError` somewhere downstream.

**Two corrections to execution.md, and one to scheduling.md.** §8.2's *writes go through the supervisor* rested on the supervisor holding the accounting; `--author` / `--reason` put every applied change in the eval log, so it is in one place both parties write to, and the rule that survives is narrower — *Steward never undoes a latch it did not set*. §8.5 said `POST /config`; it is `PATCH` at two scopes, and `max_samples` is on the **task** one, which makes the fleet listing a precondition for retuning rather than an independent call (sched §3.2 now says so).

**And §8.5's open hazard is closed by measurement.** `time_limit`, `token_limit`, and `message_limit` are patchable *and* are inputs to `task_identifier`, so a patched value written back into `eval.config` would leave a finished task reading as an orphan. It does not happen: patching a `token_limit` on a live worker and recomputing the identifier from the log it went on to land yields the identifier the manifest scheduled. Pinned as a test, because it is a correlation property that would fail silently.

One thing worth carrying forward for the budget: **an `inspect ctl` invocation costs ~1.3s** — cheaper than a launch, dearer than anything else — which is why the live test folds polling and pid-filtering into one call, and why the gated fixture's sleep is now settable.

### Step 10 — The run claim ✅ **done**

**Delivered** `_workspace/claim.py` — `acquire`, `Claim`, `Held`, `read_claim` — plus `Workspace.claim` at `.steward/claim`. `tests/workspace/test_claim.py`, 19 cases, 7s serial, no evals; five `python -c` claim holders and seven bare sleepers that exist only to be live pids a payload can point at. Nothing calls it yet: `tend` is step 13 and `launch` step 16, and both rest on it.

**The mechanism is `fcntl.flock`, and the reason is the case that actually happens.** exec §5.7 specified a pid-keyed registry with a heartbeat, judged stale by age. Four semantics were verified on macOS rather than assumed, and the fourth is the one that reshaped the step: another process is blocked, a **second descriptor in the same process is also blocked**, a live holder is blocked, and a **killed holder's lock is acquirable immediately** — the kernel releases it. So a tend that crashed, was Ctrl-C'd, or was OOM-killed leaves *nothing to reap*: no timeout to sit through, no age to compute, no clock to be wrong about. The same-process case is not a curiosity either — it means the double-invoke a coding agent produces is caught by the same mechanism as the double-invoke a timer produces, and that eight of the twelve tests need no subprocess.

**Breaking a wedged holder is on by default, and that reversed the first plan.** A holder alive past the threshold is the only staleness `flock` leaves, and it cannot be taken without killing. Escalate-by-default was the first answer and it was wrong on its own terms: escalation is worth nothing at 2am, which is the hour the product exists for — the same argument §8.3 already makes when it refuses to let a launch-time warning stand in for an armed timer. What makes killing safe is step 8: workers are detached and outlive their supervisor, an `intent` with no `launched` resolves as departed and respawns, a torn journal line costs one line, and the process scan means a half-finished tend's workers are not spawned twice. A tend is built to be interrupted, so breaking one is the ordinary path rather than a hazard. `--no-break-claim` inverts it for someone attached who would rather examine the wedge. The cost is named rather than hidden: a *deterministic* wedge becomes a kill loop, journaled each round.

Three decisions:

- **`STALE_AFTER = 30 minutes`, and `stale_after` is a parameter.** Ten minutes was the first number, on the reasoning that a tend takes seconds — true locally, false for `observe_logs` over a few thousand logs in S3, and a threshold tuned for the local case would kill healthy tends on exactly the deployments that most need supervising. The threshold has to clear the slowest *honest* tend. Being a parameter is what a later `_steward.md` key will set, and it is also what lets the break tests run against a live process and a real clock instead of a backdated payload.
- **The payload is evidence, not authority** — the correction review forced, and the more important half of the step. Release truncates the file and a crash between locking and writing leaves it empty, so an unheld claim never reads as its last holder; the first draft treated that as sufficient and said so in exec §5.7. It is not. **Taking the lock and writing the payload are two operations**, so for the instant between them the file still names the previous holder, which is a live pid to signal once that pid has been reused. Three things are now re-established before anything is killed: a positive pid, this host, and a process that started *before* the claim's own instant — nothing can have recorded a claim before it existed, which is the general answer to recycling rather than a patch for one window. Success is still judged by re-taking the lock rather than by polling the pid: a killed holder nothing has reaped is still a pid, while its descriptors went at exit. And the check runs before **each** signal rather than once — two tends breaking one wedge is the ordinary race, and the loser waits out the whole grace period before it would escalate, by which time the holder is dead and the claim is the winner's. Re-reading turns that into an immediate stop instead of a `SIGKILL` aimed from stale notes.
- **`Claim.broke` carries what was killed, rather than journaling from inside.** `claim.py` has no business knowing what a journal is, and step 13 is where a tend writes its other events.

**Four corrections to execution.md, one each to workflow.md, testing.md, and roadmap.md.** §5.7 loses the registry, the heartbeat, and the long-lived supervisor — all three already rejected by §8.3 and surviving only there — and gains the lock, the break, and the reason the claim is keyed on the workspace rather than on `log_dir` (which two workspaces can share, and which is frequently S3). Its restart paragraph still pointed liveness at control discovery, which step 8 replaced with the process table. §10's clock paragraph stops calling itself "safe by accident": the crash case never reaches a clock now, and the wedge case has two asymmetric directions — backwards refuses, forwards over-breaks, and over-breaking costs one tend. §8.1's caveat pointed at a heartbeat note that no longer exists. testing.md's fault table splits *stale the claim* into the crash and the wedge, which are now different mechanisms.

**Review also found that a corrupt pid field reaches `os.kill` as a process *group*.** `{"pid": 0}` signals the caller's own group and a negative pid signals another; the first draft's integer check accepted both. Demonstrated rather than reasoned about — reverting the guard and running the suite killed the test runner and the shell around it. Non-positive pids are now dropped at the payload boundary, which is the right place: a field that cannot name a process should never become one.

**And one hazard the mechanism cannot defend against, found while writing the docs.** `flock` attaches to the inode, so unlinking `.steward/claim` while a claim is held leaves the holder holding it and lets the next tend create a fresh file, lock that, and run concurrently — and nothing on the new file's side can detect it, because the old inode is unreachable by name. workflow.md advertises `.steward/` as disposable, so this is a real thing a person can do. It joins the starting-worker case under the same rule: delete it when nothing is running.

### Step 11 — Fault-injection harness 🔧 ✅ **done**

**Delivered** `tests/evalset/fixtures/faulty_evalset.py` (a definition that fails wherever it is told to), `tests/_fault.py` (the shared waiting and arming), and `tests/worker/test_faults.py`. Five tests, 23s serial, three launches and three captures. testing §4's table now names, per row, the module that falsifies it — or the step that will.

**The inventory was the surprise: ten of the sixteen faults already passed.** Not one had been written as fault injection; each arrived as the natural test of the step that built its subject. That is the strategy working rather than an accident, and it changed what the step was. It is not a suite — it is three pieces of shared machinery, one real bug, and the column that makes *the faults are expressible* checkable rather than asserted.

**The bug is that deleting `.steward/` mid-run was not safe, and both docs said it was.** testing §4 and workflow §5.1 drew the line at *while a worker is starting*; the line was in the wrong place. The directory holds the in-flight record **and** every selection document, and the scan read a worker's *identity* from the document — so a deletion left a live worker the next tend could see but not name. Its task then read as a `started` log with nothing running it, which reconciles to a respawn **with `resume` pointing at the log the first worker is still writing**, and upstream recovers that log from a live buffer database. Not "a duplicate that reads as an ordinary retry": two processes over one task's log state.

**The fix is step 8's inversion finished.** The scan was half process-table — liveness from `environ()`, identity from a file. `_worker_env` now stamps `STEWARD_WORKER` and `STEWARD_TASK` beside the marker, `scan_processes` reads them in the sweep it was already doing, and `_selected_identifier` is deleted rather than kept as a fallback. Both faults become safe in different ways, which is why they are pinned separately: mid-eval the worker is seen running and nothing is scheduled over it; mid-startup it has not read its document yet, so the deletion kills it — and a dead worker *should* be respawned, once, from nothing. Both were confirmed to fail against the pre-fix scan before being asserted.

Three decisions:

- **`reached` and `go`, not a delay.** The fixture writes `<point>.reached` on arrival and `hang` waits for `<point>.go`, so testing §4's *inject at decision points, never at wall-clock times* is the only thing it can do. `slow` was dropped from the taxonomy for the same reason — a delay is a race and a gate is a state. That took `STEWARD_TEST_SLEEP=120` out of `test_ctl.py` and a 5-second sample out of `test_inflight.py`, which were the last wall-clock waits in the worker suite. `run:hang` waits on the event loop rather than blocking it, because the control server shares that loop and an unreachable held worker is the opposite of the point.
- **Three points, and `post` earns its place.** `pre`, `run`, `post` are the whole taxonomy of a worker's lifetime, and the third is the only way to observe a landed log whose process has not exited — fault row 1, which had only its benign half.
- **testing §7 q1 closed: a fixture.** The safety argument (keeping a process-killing capability out of the package) was the weaker one. What settled it is that nothing in `src/` would import any of it — the two pieces that looked most like a tool turned out to be an eval definition and a `chmod`, and the `chmod` was cut for having no caller yet.

**Review found the harness's own hazard, which is that a held worker is a leak.** A worker is detached so a run outlives its tend, and a test that fails or times out while holding one leaves it spinning on a marker nothing will ever write — pytest exiting does not touch it. Two mechanisms, because one failure mode each. An autouse fixture sweeps the test's workspace at teardown with the production scan, which is the right tool because a leak is by definition what no `finally` caught; and a hold watches for its spawner disappearing and exits, covering the case teardown cannot — pytest killed, or Ctrl-C. The second is a state rather than a timeout: the ppid captured at startup against the current one, so a subreaper adopting the orphan reads the same as init doing it. Both were verified against a deliberately leaking test rather than reasoned about.

`until` had drifted into three copies, which is what moved it to `tests/_fault.py`; `gated_evalset.py` is gone, absorbed. `slow_evalset.py` and `raises_early.py` stay — they are *capture* fixtures, a different subject with a different arming condition, and the new one is armed only in worker mode so that reading a manifest stays free.

It landed here rather than at the end because this is the first point where every recovery claim has machinery behind it, and because the harness then grows with each later step instead of being retrofitted onto all of them at once. The inventory is the evidence that it was late rather than early — but it found a bug that had been in the design docs since they were written, so not too late.

### Step 12 — `_steward.md` ✅ **done**

**Delivered** `_workspace/directives.py` — `Directives`, `read_directives`, `resolve_pool`, `REFUSED` — plus `Workspace.directives` at `_steward.md`, and the template `init` writes in place of `policy.md`. `tests/workspace/test_directives.py`, 46 cases, 3.7s, no evals and no processes.

**Moved ahead of the tend loop**, from where it sat after `launch`. The step-13 design pass produced two settings with no home — the non-convergence threshold and the digest cadence — on top of the tend interval §5.9 had already stranded. Building tend first would have meant inventing a stub home and replacing it, which is the cost this reordering avoids.

- **One file, not two, and the merge is the deliverable.** `policy.md` becomes the prose half of `_steward.md`, under YAML front matter Steward acts on alone. The reason is not tidiness: the line between *executable* and *interpreted* is a fact about Steward's current capability rather than about the author's intent, and it moves as Steward improves — so two files would make the reader's model track the implementation. It also closes a hole with no other answer, since a pre-authorised rule written only as prose fires only when an agent is in session, which is exactly when it is not needed. workflow §5.10.
- **Exactly one key, `max_workers`.** The rule turned out to be sharper than *does Steward need this*. `max_samples` is needed, is written into every selection document, and is still **refused by name**, because `eval_set(max_samples=...)` can say it and a workspace constant would silently outrank an author who did. `max_workers` passes in the other direction: the fan-out into processes is Steward's invention and no `eval_set()` argument reaches it. The test is whether the *definition* could express it.
- **`headline_metric` was proposed and rejected on the same ground.** One workspace-wide answer to a question each task answers differently. It belongs in the log header where every reader agrees — roadmap §5, **upstream item 14** — with an interim convention in step 14.
- **The body is never parsed.** The agent opens the file itself, so no prose, however malformed, can break a tend.
- **Strict validation, and the first attempt got this wrong.** Typing the keys looked like the answer to YAML coercion — the hazard §5.3 could dismiss for the journal and cannot here, since a human writes this file. It is not: a coercive validator composes with YAML's coercion instead of cancelling it, and `max_workers: yes` went `True` → `1`, silently throttling a fleet to one worker. Caught in review. `strict=True` refuses it, and the error names the value that arrived, because seeing `not True` is the only way an author learns what YAML did to what they typed.
- **Strict now, degrading later.** A malformed file refuses the command. Falling back to the last known good is the better behaviour for a file someone may edit at 10pm with a fleet up, and it needs the per-tend `observation` record to read the last good values from — so it lands with step 13, not here.
- **§5.3 is distinguished rather than contradicted.** It rejected markdown-with-front-matter for `journal.jsonl` because block delimiters fail *globally* across thousands of appended records. One human-authored block read at startup has a single fence to get wrong, and an unterminated one is reported rather than absorbing the file.

### Step 13 — The tend core ✅ **done**

**Delivered** `_tend/` — `tend`, `status`, `TendResult`, `Refused`, `status_markdown` — with `steward tend` and `steward status` over it; `_evalset/archive.py`; `write_manifest`/`read_manifest`/`definition_hash` and `Workspace.manifest`, so desired state is finally *stored* somewhere; `_workspace/log.py`; and in `reconcile`, `ArchiveLog`, the stall guard, `Pool.stall_after`, `Summary.stalled`/`archiving`, and `InFlight.spent`. Tests: `test_tend.py` (19, no evals), `test_tend_cli.py` (11), `test_tend_live.py` (1 test, **3 launches**), plus new `test_manifest.py`/`test_log.py` and additions to `test_reconcile.py` and `test_directives.py`.

**Done when** a repeated tend is a no-op and a tend interrupted at any point is recovered by the following one — both hold, the second by deleting the journal and `status.md` between two turns and getting the same answer.

- **The convergence guard is a repair, not a feature.** An action is safe unattended when it is non-destructive, derived from a state rather than a timeout, idempotent, and **convergent** — and `SpawnWorker` failed the fourth: nothing stopped a task being respawned forever. The signal is progress rather than attempt count, since a task on attempt four with 490 of 500 samples done is converging and one repeating the same twelve failures is not.
- **It needed a second half nobody had specified.** Progress-across-`superseded()` reads the log directory, and the failure roadmap §1 calls the probable one for a large sweep — a definition that will not import, an OOM at startup — **lands no log at all**, so every turn sees the identical `missing` and respawns forever, invisibly. The record is the only witness, so `resolve_inflight` now counts spent attempts per identifier and a task with no logs stalls on that count instead.
- **Which exposed a real defect in step 5.** `SpawnWorker.attempt` was `len(superseded) + …`, i.e. derived from logs alone — so a worker that lands no log is attempt 1 forever, and since the attempt *names the worker*, every respawn overwrote the previous in-flight entry. The record could not count what it was being asked to count. `attempt` is now `max(logs, spent) + 1`; the larger rather than the sum, because an attempt that landed a log is in both.
- **And a second one, in review: the attempt number is an estimate, and the stem cannot be.** Both inputs to that `max` are disposable, so two landed logs plus a deleted `inflight.jsonl` number the next attempt 3 when 3 has already been used. `Fleet.spawn` now advances past whatever stems the workers directory already holds and records the number it actually used — uniqueness enforced by the namer, which is the one party that can see what names are taken.
- **And a third, which was the same mistake in the opposite direction.** `An invalidation clears it` was implemented as `return False` — a permanent exemption, not a reset. A retry that dies before landing a replacement leaves the invalidated log current, so the branch had no ceiling at all: an import error under an invalidated log respawns every ten minutes for as long as the run lasts. The clause now forgives what happened *before the human acted* and counts what happened after. When they acted is the log's **mtime** — invalidating rewrites the log, and nothing else touches a finished one — which is the only record of it there is; resetting at the log's `created` instead would have made invalidation useless as the unstick it exists to be, since a crash loop's crashes all postdate the log they follow. `LogAttempt.mtime` was already carried and had **no reader**, so nothing had ever exercised its units: `EvalLogInfo` normalizes to milliseconds, and reading it as seconds lands in the year 58614. A test now pins the unit.
- **Merging the two halves of the guard, also in review.** Consulting the record only when a task had *no* logs left the mixed case open: one partial log and then twenty crashes at import reads as one attempt that made progress, and respawns forever. So `InFlight.spent` carries each finished attempt's **start time** rather than a count, and the spent attempts that began after the newest log did are folded into the same fruitless run. The times are load-bearing — a count cannot tell a task recovering from crashes apart from one that got somewhere and then started crashing.
- **Archiving orphans is mechanical, because the gate is somewhere else.** Step 5's notes deferred this to 16/26 while workflow §2.3 says *anything that archives — escalate, always*. Both are satisfied by putting the gate at **commit** time (launch, step 16, `--accept-archive`): a one-character arg edit and a deliberate removal read identically, and only at the moment the manifest is committed is a human present and the delta showable. Once desired state says a task is not in the eval set, converging toward it is bookkeeping. Orphans only — superseded attempts of live tasks wait for signoff. A running orphan is reported and its worker left alone. And it is never a delete: the sibling `logs-archive/`, journaled with its reason and its destination.
- **`paused` suppresses archiving too, and still reaps.** A paused run makes no changes to itself and a move is a change; reaping only records what already happened, which stays true either way.
- **Drift is report-only.** One hash of the definition file against `manifest.source.content_hash` each turn, one line in the summary and `status.md`. It costs one `sha256` and closes the gap where a definition edited weeks ago is silently not what is running. Capture and drift agree *by construction* — `read_eval_set` calls the same `definition_hash` — so the only test that can fail is the live one, where a real capture is followed by a real turn.
- **`_steward.md` degradation landed as planned, and needed `steward.log` to land with it.** Strict parsing stays; the *caller* degrades, recovering `max_workers`/`max_samples`/`stall_after` from the most recent `observation` and saying so. With no prior observation there is nothing to fall back to and the turn refuses, since defaults would silently discard what the operator wrote. Where the *reason* goes was the open question, and the answer was neither the journal (it is not a fact about the eval set) nor step 22: a **minimal** `steward.log` writer, ~15 lines, that never raises. Step 22 still owns rotation, bounding, and the sync.
- **`_tend/` is its own package, not `_schedule/tend.py`.** The plan said the latter; it is an import cycle — `_schedule` → `_workspace` → `directives` → `_schedule` — that only breaks when `_workspace` is imported first. The turn is the composition of all four subsystems, so it sits above them rather than inside one.
- **Pause moved to step 15**, where the timer creates the need for a brake. `reconcile` has taken `paused` since step 5; nothing persists or sets it yet.

`status.md` is here rather than with the sync because writing it is part of what a turn *is* (workflow §3), and because M2 wants a human-readable snapshot even though nothing syncs yet. It is deliberately thin — step 14 owns the real surface.

### Step 14 — The tend surface

**Delivers** what a turn *says* — to a human, to an agent, and to a channel that does not exist yet.

- **Scope.** The attention list (human) and the work list (agent) as two projections of one item type; the set diff against the previous tend; the verdict glyph; the body table; the interim headline-metric convention.
- **Refs.** exec §8.3, §8.4; workflow §11.1, §12.1; agent §2.2, §4.
- **Done when** a synthesized parked worker, stalled task, and spawn failure each appear in the right projection, and the verdict reflects the run rather than the worst item.

**Split from step 13 because the turn and its rendering are separately testable**, and because 13 is what steps 15–16 depend on.

**Items are a projection of the latest `observation`, not a second lifecycle to maintain.** A stalled task, a parked worker, a no-completion worker are all computed from what a tend already sees, so the open set needs no new journal event kinds — only a *ruling* is unobservable, and that is step 23. The diff between consecutive observations is what an edge notification would fire on.

**The verdict is a level, not an event.** ✅ nothing needs you · ⚠️ something does, work continues · 🛑 nothing progresses until a person answers · ⏸ paused. Computed run-level rather than item-level: one parked worker among twenty running is ⚠️, because a rule that paints red whenever any single thing is blocked makes red meaningless. The consequence — a `stopped`-kind post can carry a ⚠️ verdict — is deliberate, since the kind says *why you are hearing from me* and the glyph says *where the run stands*.

**Nothing is sent here.** Step 24 owns the channel and the kinds. This step owns the queue model and the diff, both of which `status` needs regardless.

### Step 15 — The timer

**Delivers** the guarantee that the mechanical tend happens whether or not anyone is watching.

- **Scope.** Detect a system scheduler and install; fall back to the ticker; refuse if neither. Disarming. What a missed interval looks like afterwards. Plus **pause**: persisting the flag, `steward pause`/`steward resume`, and the ⏸ verdict — `reconcile` has taken `paused` since step 5 and nothing sets it.
- **Refs.** agent §2, §2.1; exec §8; workflow §7.
- **Done when** a run tends on schedule with no agent attached at all, and a paused one does not.

**Pause is here rather than at 13 because the timer is what creates the need for a brake.** Before an armed timer, not tending *is* pausing — the operator simply stops typing `tend`, and a flag would be a second way to express what already works. After it, a fleet reconverges every ten minutes whether or not anyone wants it to, and the only alternative to a flag is disarming the timer, which also stops the reaping and the reporting that make a paused run legible.

### Step 16 — `steward launch`

**Delivers** the entry point: a definition becomes a run.

- **Scope.** Capture; commit the manifest as desired state; report the delta against what is already there; apply the initial concurrency allocation; refusing to launch without an armed timer.
- **Refs.** workflow §7, §2.3; config §4; sched §2.2, §3.1–3.3.
- **Done when** a launch on a fresh workspace and a re-launch over a partial log directory both do the right thing.

**Last in the group, because launch is a composition rather than a component.** In the convergence model it is *capture, commit desired state, arm the timer, tend* — so it needs the tend core and the timer both, and building it earlier would mean stubbing the thing it is mostly made of. Tend is testable ahead of it against a hand-committed manifest and step 4's fixtures.

### Step 17 — Fan-out width

**Delivers** control over how many tasks a worker runs, from one to all of them — so that a runtime whose per-process side effects dominate can be run whole instead of divided.

- **Scope.** The width setting and what each end of it costs (below). How a batch is packed against `max_workers` — *k per worker* and *spread across the ceiling* are different policies and only one can be the default. What the in-flight record and `RunningWorker` carry when a worker owns several tasks, and whether attempt stays per-task. Whether *all* is spelled as every task enumerated or as a selection that filters nothing. What `max_tasks` means once it stops being moot.
- **Refs.** exec §2, §3, §4, §4.1, §7.3; config §7; sched §2.2.
- **Done when** the same manifest runs correctly at width 1, at width *k*, and at width *all* — and a worker at every width is found by the scan and reaped correctly.

**The motivation is that splitting is not always the cheaper side of the trade.** A process costs ~3s (step 1 measured it), and for Hawk it is `uv pip install` plus secrets resolution *per worker* — work step 7 attacked from the other side and could not reach, because Hawk exposes no knob to redirect or skip it. Five hundred small tasks is around half an hour of pure startup. Where a runtime's per-process side effects dominate like that, dividing the run is the expensive choice, and Steward needs a way not to make it. This is also the honest hedge against a frontend that turns out to behave badly under fan-out for a reason nobody predicted: run it whole, and supervise the one process.

**The selection document is written at every width, including *all*.** It is tempting to read *run it as the author intended* as *do not intercept it*, and that is the wrong shape — four things ride the selection document and each would have to be given up: the `INSPECT_EVAL_SET_SELECTION` path is the marker the process table is searched for (exec §7.3), the `log_dir` override is what puts logs where Steward is watching, the forced kwargs (`fail_on_error=False`, `task_retry_attempts=0`, `acp_server=True`) arrive through it, and skipping eval-set bookkeeping is what keeps `eval-set.json` and `logs.json` out of a directory Steward owns. Losing the ACP surface alone would reinstate the silent failure exec §7.4 exists to prevent. Width changes how many tasks one document names; it does not change whether there is one.

| width | selection document names | processes | crash costs | intra-process concurrency |
|---|---|---|---|---|
| **1** (default) | one task | N | one task | `max_samples` |
| ***k*** | *k* tasks | ⌈N/*k*⌉ | *k* tasks | `max_tasks` × `max_samples` |
| **all** | the whole manifest | 1 | the run | `max_tasks` × `max_samples` |

**The wire format needs nothing.** `EvalSetSelection.tasks` is already a list, so every width is expressible against inspect as released. The one thing worth deciding rather than defaulting into is whether *all* enumerates the manifest or asks for a **no-filter** form of the document — the latter is a protocol change, but a cheap one to ask for while the selection format is still unshipped and Steward is its only writer. Enumerating is the conservative choice and is what a stale manifest makes visible rather than silently overrides.

**What changes is all Steward-side.** `RunningWorker.identifier` and `DepartedWorker.identifier` are singular and `running_identifiers` is built from them; the `intent` record carries one identifier, key, and **attempt** — and attempt is per-task, so a worker spanning tasks at different attempt numbers is representable and unpleasant; `worker_stem` is `key_hash_attempt` and has no single key to build from at width > 1; and `reconcile`'s `min(ceiling − running, pending)` becomes packing. `max_tasks` stops being moot, which sched §2.2's *one budget spent twice* has to absorb — though the total-concurrency identity itself survives, because inspect's `max_samples` caps a process across all its tasks however many it is running.

**The cost to state rather than discover** is that a worker which dies partway through a batch leaves some of its tasks complete, some started, some untouched — and resolving that *is* eval-set bookkeeping, which exec §2 gives as the reason not to run `eval_set()` per worker in the first place. Width therefore trades crash isolation for startup cost, monotonically, and the default stays **1**: at width *all* a single failure costs the run, which is precisely the exposure Steward was built to remove. It is an escape hatch for runtimes that leave no better option, not a tuning knob to reach for.

**Placed before worker startup at scale because it is the unblocked half of the same problem**, and because the escape hatch is worth having *before* M2 rather than after it discovers the need. The width is the key `_steward.md` gains at this step — the file ships one key per step rather than a schema up front, since a key that parses and does nothing is a lie about what the workspace controls.

### Step 18 — Worker startup at scale ⚠ upstream 5

**Delivers** a worker whose startup cost is proportional to its own task rather than to the whole eval set — and, until that is true, a launch that says so instead of dying at startup.

- **Scope.** Emit the Layer 2 identity facets (`name`, `args_hash`, `model`, `sequence`) in the selection document at the schema version that admits them. The interim guard: what launch measures a large manifest against, and whether it refuses or warns. Whether the guard is removed when pruning lands or kept as a backstop.
- **Refs.** config §6.1, §6.2; exec §12 item 5, §13 q3; sched §2.3.
- **Workaround.** The flat ceiling of ten bounds exposure to ten copies of the manifest rather than one per core. That is the whole mitigation, and it is why a large sweep is a *precondition for raising the ceiling* rather than a blocker.
- **Done when** a worker on a several-hundred-task manifest constructs one task's dataset, and a launch that would exceed the envelope says so.

**Steward's half is four fields, and it cannot ship first.** Pruning fires on a `(name, args_hash)` mismatch *before* the boundary, so the worker needs those as fields rather than as substrings of an identifier — config §6.1's schema comment already reserves them. Both selection models are `extra="forbid"`, so sending a field inspect does not know is refused rather than ignored: the facets can only go out at the `EVAL_SET_SELECTION_VERSION` bump that introduces them. `ManifestTask` already carries all four, so when that lands `worker_selection` is the only function that changes.

**Placed after launch because half of it is a launch-time check.** The guard sched §2.3 calls for — "a launch-time check against a measured figure, a guard rather than a term in the default, so that it can be removed cleanly" — cannot exist before `launch` does. **M2 does not wait for this step**: ten workers on a modest manifest are fine today, and this is what makes ten workers on a five-hundred-task manifest fine.

**Nothing here fails loudly, which is the thing to design against.** Pruning is an optimization whose safety property is that it can only *under*-fire, so a Steward that stops emitting the facets, or an inspect that stops honouring them, costs time and reports nothing. The step's own test is therefore that the facets are in the document Steward writes — the half Steward controls — rather than that pruning fired.

> ### ▸ Gate M2 — run a sweep
>
> A manifest runs as one process per task, with crash isolation and real CPU parallelism. Logs land, nothing is lost, a human reads `status`. It notices nothing: errors are counts, not anomalies. ([roadmap.md](roadmap.md) §3.1)

## 4. Observability and tuning

### Step 19 — The tend summary and the collection queue

**Delivers** the most-executed interface in the system.

- **Scope.** The summary schema; the queue semantics — at-least-once, acknowledgment as a position rather than per item; what an arriving agent reads as its delta; context cost per tend.
- **Refs.** agent §4, §2.2, §2.3, §8, §5; workflow §5.6.
- **Done when** an agent that missed six tends reads exactly what happened across them, once.

**Renamed to keep it distinct from step 14's lists, because the two have opposite semantics and were both called "the queue".** This one is a **stream with a cursor**: tends accumulate, collection advances a position, and the delta is what happened since. Step 14's are a **set with per-item lifecycle**: an item is closed by a ruling or by re-observation, never by being read. The property that makes the split load-bearing is that **reading is not acknowledging** — an agent that collects a tend and then dies must not have consumed the only notice that something needed judgement.

### Step 20 — Human interaction in a detached worker ⚠ upstream 12 + 13

**Delivers** the one thing a detached worker cannot do for itself: reach a person.

- **Scope.** Reading the pending interaction out of the control channel's sample row; the parked worker as an in-flight condition `reconcile` neither reaps nor replaces; the blocked section of the summary and `status.md`, with the `inspect acp` command built from the per-pid ACP discovery file; the latched `stopped` notification, which is the first one Steward sends itself; how much of the request to render.
- **Refs.** exec §7.4, §12 items 12–13, §13 q13; workflow §8, §11.1, §12; agent §2, §2.2, §6; sched §2.2.
- **Done when** a worker parked on an approval appears in `status.md` as blocked with a command that attaches to it, reconcile leaves it alone, and answering it through that command lets the run continue.

**A park is a state, not an anomaly, which is why this is here and not in Judgement.** With the agent barred from answering, there is nothing to rule on and nothing to group — the work is detection, surfacing, and one notification. Placed after step 19 because the summary is what it feeds, and because the notification half rides step 24.

**Both upstream items are outstanding, and the gate does not wait.** Until item 12 lands, worker behaviour is unchanged: `approver: human` and `ask_user` fail as errored samples, loudly enough to notice and rare enough in an unattended sweep to live with. Item 13 must not lag item 12, though — a park with no signal is worse than the errored sample it replaces, because a pending `ToolEvent` makes it look exactly like a slow tool call.

### Step 21 — The tuning loop

**Delivers** concurrency that adapts over the night rather than being fixed at launch.

- **Scope.** The growth signal — rate limits, not saturation — carried in the summary; the envelope as policy; the asymmetric ratchet; retuning through step 9; recording tuning precedent.
- **Refs.** sched §3.2, §3.4, §3.5; workflow §10.5–10.7, §10.10, §10.11, §10.13.
- **Done when** an envelope and a synthesized signal produce the right retune, and the ratchet's asymmetry is a test rather than a comment.

Note the honest limit up front: `mockllm` never returns a 429, so the *end-to-end* growth path stays untested until something emits rate limits on a schedule (testing §6).

### Step 22 — `steward.log` and the sync

**Delivers** durability of the workspace outward, and the record of whether Steward itself worked.

- **Scope.** `steward.log` as the machinery record, separate from the journal's record of decisions, and the rule that says which goes where; the exclusionary sync policy; what leaves and what must not; the rule that the sync never raises.
- **Refs.** workflow §5.7, §9, §9.1–9.4; exec §9.
- **Done when** an unwritable destination degrades a run instead of stopping it.

## 5. Judgement

### Step 23 — Anomalies, proposals, and precedent

**Delivers** errors as structured state with a lifecycle, rather than counts.

- **Scope.** The three levels — instance, class computed from exception type plus raising frame, and proposal grouped by the agent across classes. The state machine and the fold. The window closing on a ruling rather than on a clock. Per-class ruling records, so a bad grouping is recoverable. Precedent lookup and how it travels. Ruling versus policy.
- **Refs.** workflow §12, §12.1, §12.2, §12.8, §6.1; sched §5.1, §5.3; exec §6.7.
- **Done when** synthesized error populations produce stable classes across tends, a ruling on a proposal produces correct per-class records and closes exactly the right window, and precedent surfaces on a recurrence.

**The three levels are one data model, so they are one step.** The seam between them is real — instance and class are computed, proposal is the agent's judgement — but it runs *through* the model rather than between two of them, and building the halves separately would mean designing the same thing twice and getting the second attempt subtly out of line with the first.

### Step 24 — Notification ⚠ upstream 7

**Delivers** the channel that reaches an absent human.

- **Scope.** The kinds and which are Steward's alone; Apprise wiring; the notification-policy keys `_steward.md` gains; what a failed notification does. Plus the triggers, which step 14 computed and discarded: an item **appearing** in the attention list, the list **emptying**, and the clock.
- **Refs.** workflow §11, §11.1–11.4; agent §7.
- **Workaround.** Smaller than it looked. `build_apprise` / `init_apprise` are importable from `inspect_ai.util._notify` — the same reach-around Steward already does for `_eval.evalset`, `_control.discovery`, and `_cli.main` — so nothing is duplicated and upstream item 7 is *make them public* rather than *unblock us*.
- **Done when** each kind fires from a synthesized condition.

Before adjudication rather than after, because anomalies need somewhere to escalate and the channel's shape constrains the lifecycle. Building the lifecycle first risks discovering that late.

**Three triggers over two renderings, so the channel reads as a column.** An item appearing posts `stopped` or `attention`; the list emptying and the clock both post `clear`, which makes the all-clear and the periodic digest one message with two triggers. Every post opens with the verdict line, which is also its title — so the last message in the channel is true modulo the reader's own actions, since a queue shrinks because *they* answered something. Additions are set-based rather than count-based: one item resolved and another arriving in the same tend must not read as no change. Batching is automatic, because the tend is the clock and the diff is per-tend.

**A tend that raises must still notify.** `steward.log` (step 22) covers a *transient* failure, but a permanent one — a malformed `_steward.md`, an unreadable log directory, expired credentials — fails identically every interval and is otherwise silent forever. So the notification path must not depend on the parts that can fail.

### Step 25 — Adjudication actions

**Delivers** doing something about a ruling.

- **Scope.** `invalidate_samples` plus respawn with `resume`; approved re-runs scheduled ahead of pending fresh tasks; the task-level attempt ceiling; the conversation's rules.
- **Refs.** exec §6.5, §6.6; sched §5.5; workflow §15; agent §6.
- **Done when** an invalidate-and-resume cycle reuses completed samples and re-runs only the invalidated ones.

### Step 26 — Signoff ⚠ upstream 6

**Delivers** the attestation, and the end of the run.

- **Scope.** The completion criterion and the gate latch; `anomalies.md`; approval terminations; curation into `logs-archive/`; what a stopped run leaves behind. **Who commits the journal** (workflow open question 4) — `init` prepares the repository and nothing commits, so the durability-through-git story does not happen by itself; signoff is the natural owner as the terminal act, but the question is assigned here rather than answered, and a runbook instruction at step 34 is the live alternative.
- **Refs.** workflow §13, §13.1, §14, §14.1, §2.2–2.4.
- **Workaround.** Reimplement the supersession predicate (`latest_completed_task_eval_logs` is private and exported nowhere), accepting that it can drift from `eval_set()`'s definition.
- **Done when** signoff refuses while anomalies are open, and curation moves rather than deletes.

> ### ▸ Gate M3 — walk away
>
> An overnight run tends itself, notices what matters, escalates it, and ends in an attestation. This is the product. ([roadmap.md](roadmap.md) §3.2)
>
> One caveat stated plainly: only the runbook's *bounds* exist at this gate — its operational half is step 34 — so a human is in the loop each session. That is deliberate; see §7.

## 6. Completeness and trust

### Step 27 — Sandbox division ⚠ upstream 9 + 10

**Delivers** a fleet that does not ask a Docker host for `workers × 2 × cores` containers.

- **Scope.** Sandbox type from the manifest; elastic versus host-bound; the division and its floor; redistribution when a worker exits.
- **Refs.** sched §3.6, §3.7; exec §12 items 9, 10.
- **No workaround.** The override does not exist and patching after spawn is too late — the containers are already open.
- **Done when** the arithmetic is unit-tested and the override is exercised against a real Docker sweep.

Positioned by an external dependency rather than by design. It is an M2-on-Docker concern: the arithmetic belongs in step 5 and the override in step 6. **k8s and unsandboxed evals are unaffected** — `k8s_sandbox` does not override `default_concurrency`, so the base `None` applies and its sandboxes are elastic.

### Step 28 — The scan boundary mode ⚠ upstream

**Delivers** a third mode at the `eval_set()` boundary, with Steward as the single writer of scan results.

- **Scope.** How the mode is signalled and what it hands back; taking the scan over with the definition's own `scanner` in hand; what enforces single-writer against a directory that other processes are still landing logs into.
- **Refs.** exec §4.2, §4.3, §5.7.
- **Done when** a scan runs over a synthesized log directory and writes exactly one result set.

Protocol work, and the reason the scanning trio is split: this step is a boundary contract, step 29 is scheduling, step 30 is reporting. They have different dependencies and different failure modes, and one step covering all three would be the largest in the plan by a wide margin.

### Step 29 — Scan passes as scheduled work

**Delivers** scans as detached children a tend spawns and reaps.

- **Scope.** Spawned immediately rather than queued behind a core, because a scan is not competing for one; one log at a time, and where that has to bend; eager drain; a crashed pass as an anomaly rather than a retry; how a scan appears in the in-flight accounting.
- **Refs.** sched §4, §4.1–4.3; exec §4.4.
- **Done when** a sweep's logs drain through scanning across successive tends, and a killed pass surfaces as an anomaly under step 11's harness.

### Step 30 — Scan results as leads

**Delivers** scan output the agent can act on.

- **Scope.** Reporting distributions rather than verdicts; a scan result as a measurement only the agent can read; findings as anomalies that arrive last; collection versus investigation.
- **Refs.** workflow §12.6, §12.3, §12.5; agent §1.
- **Done when** a flat distribution and an outlier distribution over the same scanner produce visibly different leads in the tend summary.

One frontend caveat covering all three: **Hawk rejects `scan:` locally**, so none of this has a Hawk path until §8.

### Step 31 — `scanning.md` and `analysis.md`

**Delivers** what investigation produces, per task, mirrored where the data lives.

- **Scope.** Skeleton rendering; the unprobed count; adjudicating as you go; mirroring into `log_dir` including on S3.
- **Refs.** workflow §12.7, §12.4.
- **Done when** both files exist per task and reach the log directory.

### Step 32 — Smoke gate ⚠ upstream 8

**Delivers** the rehearsal before the sweep.

- **Scope.** The dataset `limit` override; the Steward-side wall-clock cap (*not* a passed-through `time_limit`, which is in the identifier); what a smoke failure blocks.
- **Refs.** workflow §7.1; exec §12 item 8.
- **No workaround.** Without `limit`, a rehearsal runs the whole dataset.
- **Done when** a smoke run truncates, caps, and gates the real launch.

### Step 33 — Store read and publish

**Delivers** reuse across runs, and publication as an act of signoff.

- **Scope.** The read half (a cache); publication gated on the attestation, not on landing; configuration.
- **Refs.** exec §5.3–5.6; workflow §13.2.
- **Done when** publication happens at signoff and never before.

**The workers' side of this landed in step 7**, which turned both store halves off in the flow adapter. So the store is inert for Steward runs from step 7 until here — nothing indexes, nothing is reused — where before it was flow workers appending rows on flow's `write=True` default. That is the right way round: those rows were claims that a log is a valid result, made at the moment a log landed, which is before any scan, adjudication, or signoff. This step is what makes the store non-empty again, on Steward's terms and for every definition type rather than only for flow.

> ### ▸ Gate M4 — close the loop
>
> The result is trustworthy: scanned, smoke-gated, reusable. ([roadmap.md](roadmap.md) §3.3)

## 7. The agent surface

### Step 34 — Filling the runbook, and cold pickup

**Delivers** the prompt artifact that determines most of what a user experiences.

The command and `AGENTS.md` already exist (step 2), carrying the bounds that were settled in advance. What is left is the half that had to be learned: the sections the skeleton marks *not yet written*.

- **Scope.** Cadence and how it is armed; cold pickup as a specified, testable procedure; tuning inside the envelope; when to notify; the hard stops. The launch-time pre-authorization exchange. The agent scenarios of testing §5.
- **Refs.** agent §3, §5, §6, §9; testing §5, §7 q2; workflow §10.7.
- **Done when** the three bound scenarios pass — refusing signoff, raising a definition change as a question, notifying with kind `stopped` rather than only speaking into the conversation.

**Deliberately last before ship, and after the M3 gate it appears to belong to.** A runbook is a set of rules for operating machinery, and rules written against machinery nobody has operated are guesses. Steps 18 through 23 each surface rules as a side effect of being built — what the summary makes obvious, what the anomaly lifecycle actually asks of a reader, which escalations turn out to matter — and those accumulate as notes rather than as a document. This step is where they become one.

The split step 2 made keeps the cost of that honest. The **bounds** did not need discovering — they follow from decisions taken in other documents, so they ship from the start and an agent is never unbounded. What waits is the **operational** half, and until it lands, running overnight means a human in the session each time. That is a slower path to the same place, and it is also how the rules get discovered rather than invented.

## 8. Hawk in the pod — after ship

[hawk.md](hawk.md) §11 stages Hawk in three, and only the third is here.

**Stage 0** — read and run a Hawk config — is done. **Stage 1** — Hawk on an ordinary machine, with the full workspace, tend loop, anomalies and signoff — is not separate work: a Hawk config is a definition type, so it falls out of steps 1–33. Its one Hawk-specific obligation is **step 7**, pulled to the front of execution for exactly that reason, plus two local caveats with no design content: `isolation: strict` hard-fails without `HAWK_RUNNER_PATCH_SANDBOX`, which only the Helm template sets, and `scan:` is rejected locally, so steps 25–27 have no Hawk path until this group.

**Stage 2 — Steward inside the pod — lands after ship.** It is architecture rather than configuration, it is the one stage needing a change on someone else's roadmap, and the three stages before it de-risk it. Nothing above waits on it.

### Step 35 — Blocking launch and exit codes

**Delivers** `steward launch --wait-signoff`: a process that holds the pod open for the whole lifecycle Steward defines.

- **Scope.** Workers as ordinary children rather than detached, since the runner is PID 1 and detaching buys nothing; the in-pod timer as a second driver of the same `reconcile`, sharing the run claim with an external tend; the exit-code mapping.
- **Refs.** hawk §7, §7.1, §8; exec §11.3.
- **Done when** the mapping is exercised end to end, including the non-obvious row: **terminal without signoff exits 0**, because a non-zero exit trips `backoffLimit` and the restarted runner resurrects the eval.
- **Also settles** hawk §12 q2 — how long a parked run waits before its deadline fires, and what the timeout writes.

### Step 36 — The relay surface

**Delivers** driving a pod-resident Steward from outside.

- **Scope.** A loopback TCP server inside the pod — Inspect's control channel cannot be borrowed, since the bind is hardcoded `AF_UNIX` with a PID-derived path and a `SO_PEERCRED` check, and `acp_server`'s `int → TCP 127.0.0.1:<port>` path is the precedent. A `steward --remote` client shaped by the relay's limits: **one connection per command**, because a five-session-per-principal cap and a 900-second idle timeout that keepalives do not reset both punish a pooled client. Recording a relay signoff as claimed-but-unverified, and declining to add a shared-token scheme below a real gate.
- **Refs.** hawk §9, §9.1, §9.2.
- **Done when** a full tend cycle runs over `hawk attach` without approaching either limit.

### Step 37 — The Hawk call site ⚠ Hawk-side

**Delivers** Hawk invoking Steward instead of `eval_set_from_config`.

- **Scope.** A runner type or flag at the one call site in `run_eval_set.py`. Not Steward's code.
- **Refs.** hawk §11; config §8.3.
- **Also settles** hawk §12 q4 — whether a resumed Hawk job is the same Steward run, which decides whether the journal survives a pod restart at all.

## 9. Why this order

Seven ordering choices are load-bearing. The rest of the sequence is just dependencies.

**`reconcile` before any process exists (5 before 6).** It is the component most likely to be subtly wrong and the cheapest to test exhaustively. Building the fleet first would mean debugging scheduling logic by watching it.

**Test infrastructure ahead of its subject (4 before 5, 11 before the recovery work).** The fixture generator is what makes `reconcile` a table instead of a fixture suite, and the fault harness lands at the first point where all the recovery claims have machinery behind them — so it grows with each later step rather than being retrofitted onto all of them at once. Both read backwards. Both are the reason the steps after them are cheap.

**Once-per-run ownership as a general step, not a Hawk one (7).** Flow and Hawk hit the same wall from opposite sides — one wastefully, one unsafely — and the ask is one mechanism. Filing it under Hawk is how it ends up implemented twice, so it sits in execution beside the spawn it constrains.

**Launch last in its group (16, after tend and the timer).** It is a composition of both, not a peer of either.

**Standing settings before the loop that reads them (12, moved up from after launch).** The tend interval, the non-convergence threshold, and the digest cadence are all standing properties of a workspace, and building the loop first would mean inventing a stub home for each and replacing it. The generalization is worth stating because it recurs: **when several steps need somewhere to put a decision, the somewhere goes first.** The file then grows one key per step rather than shipping a schema of keys nothing reads.

**Concurrency tuning split across the M2 gate (9 and 21).** The mechanism — the control channel client — is a wire protocol testable against one live worker, and `pause` and adjudication need it anyway, so it lands early in execution. The policy — signal, envelope, ratchet — needs the tend summary to carry the signal, so it lands immediately after step 19. Nothing breaks without the policy; the run is only slower, which is why the M2 gate does not wait for it.

**Notification before adjudication (24 before 25).** Anomalies need somewhere to escalate, notification is independently testable, and the channel's shape constrains the lifecycle.

**The runbook last before ship (34).** Argued in §7 above. It is the one step placed by an argument about *how design happens* rather than by a dependency.

Three steps are placed by external dependency rather than by design, and each is called out where it appears: **sandbox division (27)** would sit across steps 5 and 6 if upstream items 9 and 10 existed, **smoke (32)** would sit beside launch if item 8 did, and **worker startup at scale (18)** waits on item 5 — though half of it, the memory guard, is placed by a real dependency on launch.

## 10. The test budget

Thirty-seven steps each adding "just a few end-to-end tests" is how a suite reaches twenty minutes, and by then no one runs it before pushing. Step 1 measured what the cost actually is, so the rest of the plan can be held to a number instead of an intention.

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

**Which steps genuinely need real workers**: 1 (done), 6–9, 11, 17, 20, and parts of 13, 16, 25, and 28–30 — call it twelve. Steps 2–5, 10, 12, 14–15, 18–19, 21–24, 26–27, and 31–33 are layer 1 or near it: synthesized state, pure functions, no eval runs at all. **Budget ~12 launches for a layer-2 step**, which is roughly 35s serial and under 10s with `-n auto`. Ten such steps lands the whole suite near five minutes serial and one to two minutes on CI. That is the line; a step that wants more should say why in its design pass.

**Running total after step 13**: 366 offline tests, 50s with `-n auto`. Step 13 is the largest step so far — the turn, two CLI verbs, three new actions — and added ninety-one tests for **three launches**, at no measurable cost in wall time. Rule 3 did nearly all of it: the turn takes a workspace, and a workspace is a directory, so nineteen of those tests drive the real `tend` against a hand-committed manifest and a log directory `tests/_logs.py` wrote.

The one thing that could not be faked is the crash loop that lands no log, since the point of it is that the log directory shows nothing — and that is the fourth category again, at its cheapest yet: **the definition is `# a definition that exits`**. A worker spawns, the interpreter starts and stops, and the in-flight record is the only trace, which is precisely the state under test. Five of them, ~30ms each, against real `Fleet.spawn`, real `resolve_inflight`, and the real guard. The alternative was mocking the fleet, which would have tested the mock.

That left one launch group worth paying for, and it buys the claim no fixture can make: **that the layers agree about the same run.** Capture writes identifiers and a content hash, two workers write logs, observation matches the two, and a third turn does nothing — one capture and two workers, in one test, asserting six things. The live *stall* test the design pass had budgeted was dropped rather than written: it would have re-run a path already covered with real processes, in exchange for learning that `faulty_evalset.py`'s `pre:crash` lands no log, which step 11 already establishes.

**Running total after step 12**: 275 offline tests, 45s with `-n auto`. Step 12 added forty-six tests and **no launches at all** — the whole step is a parser, a refusal table, and a precedence chain, which is rule 3 in its purest form. The 2.4s it cost is fixture overhead rather than work, and the one test that touches the filesystem meaningfully is the one asserting that the template `init` ships actually parses as front matter, which is the failure that would otherwise break every workspace at once. Step 11 added five tests and three launches; the seven seconds it cost are roughly what the migrations gave back, since the tests it absorbed had been paying fixed sleeps to keep an eval alive. Step 10 added nineteen tests and no evals at all, for 7s serial and no measurable change in parallel. Step 9 added sixteen tests, one launch, and about ten seconds — nearly all of it in `inspect ctl` invocations rather than in evals, which is a cost category the budget below did not have. Step 6 was the first layer-2 step since step 1 and came in at 12 launches, exactly the budget — but eight of those are *relocated* rather than new, because the two step-1 selection tests that ran the production shape moved onto the real spawn instead of being duplicated beside it. Step 7 added one test and three launches, step 8 eighteen tests and three. Wall time has moved by about three seconds across all three, which is rule 1 working.

Step 8 is the clearest case yet for rule 3: the scan is a parameter, so twelve of its eighteen cases are a table over a stubbed one, and the three launches are spent only where the process boundary genuinely is the subject — a worker held in the window before its eval, one killed inside it, and one that finishes. Its one deliberate cost is a five-second sleep in the gated fixture, bought because a socket that exists for less time than it takes to look for it cannot be asserted on.

It also found a fourth category the rules did not name: **a real process that is not an eval**. Proving that a worker's children are not the worker needs actual parent-and-child processes and no eval whatsoever, and `python -c "time.sleep(120)"` costs about 50ms. Four of them appear here. The rule is worth stating — *when the process boundary is the subject but the eval is not, do not launch an eval* — because the instinct is to reach for the fixture that already exists.

Step 10 put a price on that category, and it has two tiers. Its five claim holders each import `inspect_steward` and so cost **1.3s** rather than 50ms, because `inspect_steward/__init__` reaches `inspect_ai`; the alternative — a child writing the claim payload with plain stdlib — was declined, since a test that hand-writes the format it asserts on is testing itself. Its seven *bystanders* exist only to be a live pid that a hand-written payload can point at, import nothing, and cost the full 50ms discount. The rule: pay the import only when the child has to run Steward's own code. Under `-n auto` the whole step is free either way.

Step 9 added a fifth, which sits between the two: **an `inspect ctl` invocation costs ~1.3s**, all of it importing inspect_ai, so it is a tenth of a launch and two orders of magnitude more than a function call. Two consequences fell out of that number rather than out of taste. A live test must not spend invocations casually — the one here folds polling and pid-filtering into a single call — and **the eval has to outlast them**, which is why the gated fixture's sleep became settable. The other half of the step is table-driven over a `_decode` split out from the subprocess, which costs nothing and covers the outcomes no test can manufacture: a wedged event loop, a malformed body, an error kind this version has never heard of.

One thing the budget did not anticipate: **the two measurements step 7 took are not tests and should not become them.** Timing a flow worker against a baseline, and racing two `uv pip install` to see whether uv locks, each answered a question the design had been guessing at — and each would be a slow, flaky, third-party-dependent test if pinned. Measure, write the number in the design with its date and method, move on.

**Levers held in reserve**, in the order they become worth their complexity: cache captures across tests in a session (deterministic by the contract in configuration.md §4, so it is safe — but xdist gives each worker its own cache, so it pays off only once a single xdist worker runs many tests); share one worker run across several assertions; and, last, split the suite so layer 2 runs on a different cadence than layer 1.

## 11. What this plan does not decide

- **The internals of any step.** Each gets a design pass, starting from its **Refs** line.
- **Sizing.** [roadmap.md](roadmap.md) §3 declines to attempt dates and this document does too.
- **Where ship falls** between the M3 gate and step 34. Only that it falls before §8.
- **The open questions.** Twenty-odd remain across the docs, distributed over the steps that own them. None blocks step 1.
- **The one real gap.** Nothing surfaces an agent's mistake ([roadmap.md](roadmap.md) §7). It belongs to step 23 and is not yet solved there.
