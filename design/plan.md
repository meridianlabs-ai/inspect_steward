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

- **The journal marks the workspace, and `init` opens it with a real `initialized` event.** Nothing else could mark it: `.steward/` is disposable, a definition can sit anywhere, and `_steward.yaml` is optional (step 15). This pulls a minimal envelope and append path forward from step 3, which keeps the fold and the vocabulary where they belong, and it gives `Workspace.find()` something to walk up for (workflow §5.1).
- **The definition placeholder is empty**, and `--type` chooses only its filename — including `hawk.yaml`, since with nothing being authored there was no reason to exclude it. workflow §5's promise of a runnable scaffold was amended rather than left contradicting the code; what a good starting point contains is deferred.

A **skeletal `steward runbook`** ships too, so `AGENTS.md` can take its final shape now instead of naming a command that errors. Not a stub: agent.md §6's prohibitions and §5/§9's reading disciplines are settled, so it carries those for real and marks the operational sections *not yet written*.

One test defect found and fixed: the pre-existing `test_cli_init` invoked `init` with no directory, which wrote a workspace into the repository and appended to its `.gitignore`. `init` was right; the test was not.

### Step 3 — The journal ✅ **done**

**Delivered** the append-only record as something that can be read after a crash: a safe concurrent append, a damage-tolerant reader, and `summarize()` — the first fold, and the shape every later one takes. `tests/workspace/test_journal.py`, 14 cases, 2.2s.

Three decisions, each the opposite of how Steward treats a selection document — and deliberately, because **a selection is input being validated before it changes what runs, while a journal is history being read**:

- **An unrecognised event type reads as a generic event.** A workspace outlives the Steward that wrote it, so refusing a file because a later version put something new in it would be the wrong trade. Selection documents forbid extras for exactly the opposite reason.
- **Damage costs one line, never the file.** `read_journal` returns what it parsed *and* what it could not, with line numbers; a missing journal is an empty history rather than damage. Nothing raises, and nothing is swallowed — where a complaint goes is `steward.log`, step 21.
- **Only `initialized` is typed.** The other eight types in workflow §5.6 arrive with the steps that write them; in particular the five anomaly types stay with step 22, which keeps the three-level model as one piece rather than transcribing a table ahead of the code that gives it meaning.

One thing measured rather than assumed, because the first version of the test could not have failed: **splitting a record across two writes** (payload, then newline) loses about a quarter of the events under four concurrent writers. Size is not the hazard and neither is the platform — a buffered whole-line append is safe on a local filesystem. So the guard is one `os.write` of a pre-built line, and the test's docstring records what it does and does not catch.

### Step 4 — Observed state, and the fixtures that prove it 🔧 ✅ **done**

**Delivered** the read half of convergence — `observe_logs` turns a log directory into attempts grouped by identifier, `observe_tasks` reads those against a manifest — and `tests/_logs.py`, which synthesizes such a directory without running anything. `tests/evalset/test_observe.py`, 23 cases, 1.2s, **zero process launches**.

Four decisions:

- **The split is at the filesystem boundary, not at the manifest.** `observe_logs` does the I/O and knows nothing about what was supposed to run, which is what lets it serve `logs-archive/` and the flow store — neither of which has a manifest to compare against. Completeness is a second, pure function, so step 5's inputs stay pure.
- **Four states, one carrying a reason.** `complete`, `incomplete`, `missing`, `orphaned` — deliberately the domain of the action vocabulary rather than a taxonomy of log conditions. Every incomplete task takes the same action, so *why* (`started`, `short`, `invalidated`, `error`, `cancelled`, `no_results`) is reporting material. Complete-clean and complete-with-errors are one state and a count, for the same reason: both mean *do not spawn*, and the errored samples are step 22's queue.
- **Attempts order by `eval.created`, and the latest *successful* one is current.** Both halves diverge from upstream, which sorts by mtime and takes the newest whatever its status. Mtime is not intrinsic — restoring a log from the archive rewrites it, and the archive is a cache the design intends to hit — while `created` survives even the mid-run header fallback. And a deliberate re-run that errored must not displace a good result (exec §5.8).
- **An unreadable log costs one log, never the directory** — step 3's rule, applied to the other thing Steward reads on a schedule. Not hypothetical: a worker's zip has no readable header for the moment between creation and its first journal entry, and a tend that raised on that is a tend that never ran.

Two things measured or read rather than assumed. **Header reads are concurrent**, because an `.eval` header read genuinely awaits on I/O where a `json` one is synchronous inside its `async def` — so the fixtures can prove the reader right and can say nothing about its speed, and a local benchmark over them would argue for exactly the wrong thing. And **the mid-run `.eval` case is layer 1 after all**: a zip with one member, `_journal/start.json`, reproduces it in ten lines.

Two findings, both recorded upstream (exec §12, items 6 and 11): `read_eval_log_headers_async` raises on any single unreadable file, so a scheduled reader cannot use it; and the capture manifest discards the epochs *reducer*, so a reducer-only change reads as complete where `eval_set()` would re-score. A third went to workflow §2.1 — **renaming the definition file orphans the entire run**, since `task_file` is in the identifier, and the workspace's fixed definition name is what makes Steward immune.

**Generator and reader were one step because they are mutually defining.** Neither is testable alone. What made the generator trustworthy is that one `_eval_spec()` builds the `EvalSpec` both the manifest row and every log derive from, so they agree on the identifier by construction rather than by a literal repeated twice.

### Step 5 — `reconcile` ✅ **done**

**Delivered** the decision function — `(manifest, inflight, observed, pool, paused) -> (actions, queued, summary)`, pure. `tests/schedule/`, 29 cases, 1.4s, no process ever started.

Four decisions:

- **The state enum *is* the action vocabulary.** Step 4's four states map one to one onto what to do — `complete` leave it, `incomplete` resume it, `missing` spawn it, `orphaned` report it — so `reconcile` has no classification logic of its own. Every incomplete task resumes whatever went wrong; there is deliberately no branch on the reason, because resume reuses exactly the samples worth keeping.
- **A manifest from a different inspect raises rather than reports.** Unmatchable identifiers make every task read *missing* and every log read *orphaned*, so a finished sweep would re-run from scratch — and a summary carrying that looks entirely normal. A returned flag asks every future consumer to remember to check it; an exception cannot be forgotten. Step 12's `tend` catches it in one place and says `steward launch`.
- **`archive` is not in the vocabulary yet.** Orphans are named in the summary and nothing acts on them. An action nobody can execute is a stub, and a tend computing twelve archive actions every ten minutes and running none of them is noise. It arrives with the launch gate (step 14) and signoff's sweep (step 25).
- **The queue holds `SpawnWorker`s, not identifiers** — the same decision deferred, so *approved re-runs go first* (step 24) becomes a sort rather than a second code path.

The crash-recovery case is the one worth naming: **a live worker and one that died mid-run leave exactly the same thing in the log directory** — a `started` log with no results. Only the in-flight record separates them, which is why `reconcile` takes it, and getting it wrong means either double-spawning a live task or never recovering a dead one. It has its own test.

Two things changed in the design while building it.

**`max_samples` was not in the capture manifest**, so scheduling.md's *yield to whatever the definition set* silently could not happen — a definition asking for 60 got Steward's 40 and nobody was told, and the log records only the effective value so there was no read-side workaround. Fixed upstream: `options` now carries `max_samples`, no schema bump needed since `options` is a free-form dict.

Reading it exposed a second thing, which is why `resolve_max_samples` has **three** sources rather than two: Steward's default and an operator's explicit instruction are not the same claim. Collapsing them means either the fallback outranks a definition or a typed `--max-samples` loses to one, and neither shows up in the resulting number. So `Pool.max_samples` is `int | None`, where `None` means *no preference*, and the order is operator → definition → 40.

**The ceiling stopped being derived from cores.** §2.2's argument — *past core count another process buys no parallelism* — misreads the workload: a worker is on the CPU in bursts and waiting on a model API in between, so ten workers on four cores is ordinary. One process per task buys isolation of those bursts, not core saturation, which makes the ceiling a resource guard rather than a parallelism budget. It is now a flat **10**, matching where `eval_set()`'s own `max_tasks` starts, and expected to be raised. That deleted `available_cores()` and its cgroup reading — the cgroup lie is still real, but it now surfaces only inside Docker's `default_concurrency()` at step 26, where §3.6 records it. Recoverable from `f1e1822` if that step wants it. The formula also needed the running-workers term: `min(ceiling − running, pending)`, since eight running and five pending under a ceiling of ten is two spawns, not five.

Sandbox division is *not* here; it is step 26, blocked upstream. `reconcile` divides one budget at this step and grows a second later.

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

One finding for later: **a definition declaring a scanner cannot be run by Steward today** — worker mode rejects scanners outright (exec §4.2), so the worker exits with an upstream `PrerequisiteError`. The capture manifest records `scanners` in `options`, so `launch` can refuse early with a better message. Step 14, and answered properly at step 27.

### Step 7 — Once-per-run pre-boundary work ✅ **done**

**Delivered** a flow fan-out that keeps its once-per-run work out of the run's log directory, and an accurate account of what Hawk's fan-out actually costs. `tests/worker/test_flow_worker.py`, one case, three process launches.

**The skip mechanism the step was scoped around was the wrong thing to reach for**, because a frontend change is not available: Steward runs `inspect_flow` and `hawk` as released. What *is* available is aiming the work somewhere harmless with controls both already ship — and for Flow that reaches two thirds of it, using nothing but `flow run` options.

**Every flow worker gets a scratch directory of its own** (`.steward/workers/<stem>/`) through the frontend `--log-dir` channel, while the selection carries the run's. All three pre-boundary items key off `spec.log_dir`, so `flow.yaml` and the requirements snapshot stop being N concurrent writes to two shared paths, and `find_existing_logs` scans an empty directory instead of one that grows all run. Safe because the scan's result never reaches `eval_set()` — `_runner/run.py` uses it only for the display and the store. **The two channels were never meant to carry the same value**, which is a correction to exec §4 and config §8.2, and reads had already been doing the right thing.

Measured, one flow worker against a 3.0s plain-`eval_set()` baseline: **4.36s → 4.14s** on an empty log directory, **4.79s → 4.21s** with 150 logs in it. The growing term is gone (it was ~2.4ms per log, so ~12s per worker at five thousand), and the residual ~1.1s is **entirely the requirements freeze** — two `uv` shell-outs that cannot be skipped from outside, only redirected. The original attribution spread that 1.1s across resolution, `flow.yaml`, and the scan; measurement says otherwise, which sharpens what is still worth asking Flow for.

**Two other things came with it.** `--no-store-read --no-store-write` makes exec §5.4's "workers run with `store=none`" true, having been unimplemented — which leaves the store inert until step 32 hands both halves to Steward, and that is the right way round: what workers were writing were unattested claims. And `--set execution_type=inproc` fixes a **live bug**: a spec declaring `execution_type: venv` sent `flow run` to `venv_launch`, so every worker built a virtualenv and ran the eval in a grandchild — the pid Steward recorded, its discovery entry, and every liveness check keyed on them all naming the wrong process. That is the same instruction as hawk's `--direct`, so it is now stated once in config §8 as a rule for every adapter rather than twice as a detail.

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
- **Only two functions, because only two have callers.** `list_tasks(pids)` and `task_config(...)`. `pause` was in the scope line and is **not** here: exec §6.4 declines automatic model pause, workflow §3.1 puts `steward pause` on the scheduling side with no channel involved, and its one real consumer is step 24's composition — which is not obviously served by a single-target wrapper. Writing it now would be exactly the speculative surface the doctrine argues against.
- **Steward models the envelopes, because upstream does not.** Checked first: every producer returns `dict[str, Any]`, the routes are `-> Any`, and `_cli/ctl` holds no `BaseModel` or `TypedDict` either — the contract is held by upstream's tests, not by a type Steward could import. (The three `TypedDict`s in `_control/requeue.py` are the exception, and step 24 should reuse them.) So Steward validates the fields it depends on at the boundary, `extra="allow"`. That earned its place within the hour: `persisted` is **per-knob** (`{"max_samples": true}`), not the bool the read's `null` suggested, and the boundary is where that surfaced instead of a `KeyError` somewhere downstream.

**Two corrections to execution.md, and one to scheduling.md.** §8.2's *writes go through the supervisor* rested on the supervisor holding the accounting; `--author` / `--reason` put every applied change in the eval log, so it is in one place both parties write to, and the rule that survives is narrower — *Steward never undoes a latch it did not set*. §8.5 said `POST /config`; it is `PATCH` at two scopes, and `max_samples` is on the **task** one, which makes the fleet listing a precondition for retuning rather than an independent call (sched §3.2 now says so).

**And §8.5's open hazard is closed by measurement.** `time_limit`, `token_limit`, and `message_limit` are patchable *and* are inputs to `task_identifier`, so a patched value written back into `eval.config` would leave a finished task reading as an orphan. It does not happen: patching a `token_limit` on a live worker and recomputing the identifier from the log it went on to land yields the identifier the manifest scheduled. Pinned as a test, because it is a correlation property that would fail silently.

One thing worth carrying forward for the budget: **an `inspect ctl` invocation costs ~1.3s** — cheaper than a launch, dearer than anything else — which is why the live test folds polling and pid-filtering into one call, and why the gated fixture's sleep is now settable.

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

### Step 15 — `_steward.yaml`

**Delivers** a declarative home for the settings that are a standing property of a workspace rather than an argument to one launch.

- **Scope.** The one rule that makes the file safe (below) and how it is enforced. The precedence chain against the CLI and against a definition. What `init` writes, if anything. Which of today's flags move, and which stay per-run. The underscore, which sorts it to the top of a directory listing beside `AGENTS.md`.
- **Refs.** workflow §5.9; config §2, §10; sched §2.2, §3.1.
- **Done when** a workspace expresses its fleet shape and tend interval once, and a file naming anything the definition owns is refused by name at read time.

**This reopens workflow.md §5.9, deliberately, and the reason it can be reopened is that §5.9's own table shows the seam.** That section refused a config file on two grounds: every candidate belonged somewhere else, and a second file beside the definition is where a contradicting `log_dir` or `model` ends up. The first ground has one weak row — *tend interval → "the runbook, and the agent's scheduling"* — because the runbook is prose and both cron and the ticker fallback need a number, which today can only arrive as a `launch` flag. A standing property of a workspace showing up as a per-launch argument is the thing §5.9 concedes config files are *for*. And the section never considered fleet shape at all, because until step 16 there was none to express.

**The drift objection is answered structurally rather than by discipline.** The rule is that the file may express only what the definition *cannot*: things that affect Steward, never things that affect Inspect. `log_dir`, `model`, task selection, and eval configuration belong to `evalset.py` / `flow.yaml` / `hawk.yaml` and are **refused by name with a message that says where they go**, not silently ignored — the failure mode §5.9 feared is a key someone added in good faith, so it has to fail loudly the first time. Unknown keys are rejected outright, which is the same posture the selection document takes and for the same reason: this is input, not history.

`Pool` already has the shape this slots into. Its `max_samples: int | None` distinguishes *no operator preference* from *an operator's instruction*, resolving operator → definition → default. The config file is a second operator-level source, below the CLI and above the definition, and that ordering is the whole precedence story.

### Step 16 — Fan-out width

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

**Placed before worker startup at scale because it is the unblocked half of the same problem**, and because the escape hatch is worth having *before* M2 rather than after it discovers the need. It is placed after `_steward.yaml` because the width is the first setting with nowhere else to live.

### Step 17 — Worker startup at scale ⚠ upstream 5

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

### Step 18 — The tend summary and the queue

**Delivers** the most-executed interface in the system.

- **Scope.** The summary schema; the queue semantics — at-least-once, acknowledgment as a position rather than per item; what an arriving agent reads as its delta; context cost per tend.
- **Refs.** agent §4, §2.2, §2.3, §8, §5; workflow §5.6.
- **Done when** an agent that missed six tends reads exactly what happened across them, once.

### Step 19 — Human interaction in a detached worker ⚠ upstream 12 + 13

**Delivers** the one thing a detached worker cannot do for itself: reach a person.

- **Scope.** Reading the pending interaction out of the control channel's sample row; the parked worker as an in-flight condition `reconcile` neither reaps nor replaces; the blocked section of the summary and `status.md`, with the `inspect acp` command built from the per-pid ACP discovery file; the latched `stopped` notification, which is the first one Steward sends itself; how much of the request to render.
- **Refs.** exec §7.4, §12 items 12–13, §13 q13; workflow §8, §11.1, §12; agent §2, §2.2, §6; sched §2.2.
- **Done when** a worker parked on an approval appears in `status.md` as blocked with a command that attaches to it, reconcile leaves it alone, and answering it through that command lets the run continue.

**A park is a state, not an anomaly, which is why this is here and not in Judgement.** With the agent barred from answering, there is nothing to rule on and nothing to group — the work is detection, surfacing, and one notification. Placed after step 18 because the summary is what it feeds, and because the notification half rides step 23.

**Both upstream items are outstanding, and the gate does not wait.** Until item 12 lands, worker behaviour is unchanged: `approver: human` and `ask_user` fail as errored samples, loudly enough to notice and rare enough in an unattended sweep to live with. Item 13 must not lag item 12, though — a park with no signal is worse than the errored sample it replaces, because a pending `ToolEvent` makes it look exactly like a slow tool call.

### Step 20 — The tuning loop

**Delivers** concurrency that adapts over the night rather than being fixed at launch.

- **Scope.** The growth signal — rate limits, not saturation — carried in the summary; the envelope as policy; the asymmetric ratchet; retuning through step 9; recording tuning precedent.
- **Refs.** sched §3.2, §3.4, §3.5; workflow §10.5–10.7, §10.10, §10.11, §10.13.
- **Done when** an envelope and a synthesized signal produce the right retune, and the ratchet's asymmetry is a test rather than a comment.

Note the honest limit up front: `mockllm` never returns a 429, so the *end-to-end* growth path stays untested until something emits rate limits on a schedule (testing §6).

### Step 21 — `steward.log` and the sync

**Delivers** durability of the workspace outward, and the record of whether Steward itself worked.

- **Scope.** `steward.log` as the machinery record, separate from the journal's record of decisions, and the rule that says which goes where; the exclusionary sync policy; what leaves and what must not; the rule that the sync never raises.
- **Refs.** workflow §5.7, §9, §9.1–9.4; exec §9.
- **Done when** an unwritable destination degrades a run instead of stopping it.

## 5. Judgement

### Step 22 — Anomalies, proposals, and precedent

**Delivers** errors as structured state with a lifecycle, rather than counts.

- **Scope.** The three levels — instance, class computed from exception type plus raising frame, and proposal grouped by the agent across classes. The state machine and the fold. The window closing on a ruling rather than on a clock. Per-class ruling records, so a bad grouping is recoverable. Precedent lookup and how it travels. Ruling versus policy.
- **Refs.** workflow §12, §12.1, §12.2, §12.8, §6.1; sched §5.1, §5.3; exec §6.7.
- **Done when** synthesized error populations produce stable classes across tends, a ruling on a proposal produces correct per-class records and closes exactly the right window, and precedent surfaces on a recurrence.

**The three levels are one data model, so they are one step.** The seam between them is real — instance and class are computed, proposal is the agent's judgement — but it runs *through* the model rather than between two of them, and building the halves separately would mean designing the same thing twice and getting the second attempt subtly out of line with the first.

### Step 23 — Notification ⚠ upstream 7

**Delivers** the channel that reaches an absent human.

- **Scope.** The four kinds and which two are Steward's alone; Apprise wiring; what a failed notification does.
- **Refs.** workflow §11, §11.1–11.4; agent §7.
- **Workaround.** Steward carries Apprise itself, duplicating `build_apprise` / `init_apprise`. Real, and small.
- **Done when** each kind fires from a synthesized condition.

Before adjudication rather than after, because anomalies need somewhere to escalate and the channel's shape constrains the lifecycle. Building the lifecycle first risks discovering that late.

### Step 24 — Adjudication actions

**Delivers** doing something about a ruling.

- **Scope.** `invalidate_samples` plus respawn with `resume`; approved re-runs scheduled ahead of pending fresh tasks; the task-level attempt ceiling; the conversation's rules.
- **Refs.** exec §6.5, §6.6; sched §5.5; workflow §15; agent §6.
- **Done when** an invalidate-and-resume cycle reuses completed samples and re-runs only the invalidated ones.

### Step 25 — Signoff ⚠ upstream 6

**Delivers** the attestation, and the end of the run.

- **Scope.** The completion criterion and the gate latch; `anomalies.md`; approval terminations; curation into `logs-archive/`; what a stopped run leaves behind. **Who commits the journal** (workflow open question 4) — `init` prepares the repository and nothing commits, so the durability-through-git story does not happen by itself; signoff is the natural owner as the terminal act, but the question is assigned here rather than answered, and a runbook instruction at step 33 is the live alternative.
- **Refs.** workflow §13, §13.1, §14, §14.1, §2.2–2.4.
- **Workaround.** Reimplement the supersession predicate (`latest_completed_task_eval_logs` is private and exported nowhere), accepting that it can drift from `eval_set()`'s definition.
- **Done when** signoff refuses while anomalies are open, and curation moves rather than deletes.

> ### ▸ Gate M3 — walk away
>
> An overnight run tends itself, notices what matters, escalates it, and ends in an attestation. This is the product. ([roadmap.md](roadmap.md) §3.2)
>
> One caveat stated plainly: only the runbook's *bounds* exist at this gate — its operational half is step 33 — so a human is in the loop each session. That is deliberate; see §7.

## 6. Completeness and trust

### Step 26 — Sandbox division ⚠ upstream 9 + 10

**Delivers** a fleet that does not ask a Docker host for `workers × 2 × cores` containers.

- **Scope.** Sandbox type from the manifest; elastic versus host-bound; the division and its floor; redistribution when a worker exits.
- **Refs.** sched §3.6, §3.7; exec §12 items 9, 10.
- **No workaround.** The override does not exist and patching after spawn is too late — the containers are already open.
- **Done when** the arithmetic is unit-tested and the override is exercised against a real Docker sweep.

Positioned by an external dependency rather than by design. It is an M2-on-Docker concern: the arithmetic belongs in step 5 and the override in step 6. **k8s and unsandboxed evals are unaffected** — `k8s_sandbox` does not override `default_concurrency`, so the base `None` applies and its sandboxes are elastic.

### Step 27 — The scan boundary mode ⚠ upstream

**Delivers** a third mode at the `eval_set()` boundary, with Steward as the single writer of scan results.

- **Scope.** How the mode is signalled and what it hands back; taking the scan over with the definition's own `scanner` in hand; what enforces single-writer against a directory that other processes are still landing logs into.
- **Refs.** exec §4.2, §4.3, §5.7.
- **Done when** a scan runs over a synthesized log directory and writes exactly one result set.

Protocol work, and the reason the scanning trio is split: this step is a boundary contract, step 28 is scheduling, step 29 is reporting. They have different dependencies and different failure modes, and one step covering all three would be the largest in the plan by a wide margin.

### Step 28 — Scan passes as scheduled work

**Delivers** scans as detached children a tend spawns and reaps.

- **Scope.** Spawned immediately rather than queued behind a core, because a scan is not competing for one; one log at a time, and where that has to bend; eager drain; a crashed pass as an anomaly rather than a retry; how a scan appears in the in-flight accounting.
- **Refs.** sched §4, §4.1–4.3; exec §4.4.
- **Done when** a sweep's logs drain through scanning across successive tends, and a killed pass surfaces as an anomaly under step 11's harness.

### Step 29 — Scan results as leads

**Delivers** scan output the agent can act on.

- **Scope.** Reporting distributions rather than verdicts; a scan result as a measurement only the agent can read; findings as anomalies that arrive last; collection versus investigation.
- **Refs.** workflow §12.6, §12.3, §12.5; agent §1.
- **Done when** a flat distribution and an outlier distribution over the same scanner produce visibly different leads in the tend summary.

One frontend caveat covering all three: **Hawk rejects `scan:` locally**, so none of this has a Hawk path until §8.

### Step 30 — `scanning.md` and `analysis.md`

**Delivers** what investigation produces, per task, mirrored where the data lives.

- **Scope.** Skeleton rendering; the unprobed count; adjudicating as you go; mirroring into `log_dir` including on S3.
- **Refs.** workflow §12.7, §12.4.
- **Done when** both files exist per task and reach the log directory.

### Step 31 — Smoke gate ⚠ upstream 8

**Delivers** the rehearsal before the sweep.

- **Scope.** The dataset `limit` override; the Steward-side wall-clock cap (*not* a passed-through `time_limit`, which is in the identifier); what a smoke failure blocks.
- **Refs.** workflow §7.1; exec §12 item 8.
- **No workaround.** Without `limit`, a rehearsal runs the whole dataset.
- **Done when** a smoke run truncates, caps, and gates the real launch.

### Step 32 — Store read and publish

**Delivers** reuse across runs, and publication as an act of signoff.

- **Scope.** The read half (a cache); publication gated on the attestation, not on landing; configuration.
- **Refs.** exec §5.3–5.6; workflow §13.2.
- **Done when** publication happens at signoff and never before.

**The workers' side of this landed in step 7**, which turned both store halves off in the flow adapter. So the store is inert for Steward runs from step 7 until here — nothing indexes, nothing is reused — where before it was flow workers appending rows on flow's `write=True` default. That is the right way round: those rows were claims that a log is a valid result, made at the moment a log landed, which is before any scan, adjudication, or signoff. This step is what makes the store non-empty again, on Steward's terms and for every definition type rather than only for flow.

> ### ▸ Gate M4 — close the loop
>
> The result is trustworthy: scanned, smoke-gated, reusable. ([roadmap.md](roadmap.md) §3.3)

## 7. The agent surface

### Step 33 — Filling the runbook, and cold pickup

**Delivers** the prompt artifact that determines most of what a user experiences.

The command and `AGENTS.md` already exist (step 2), carrying the bounds that were settled in advance. What is left is the half that had to be learned: the sections the skeleton marks *not yet written*.

- **Scope.** Cadence and how it is armed; cold pickup as a specified, testable procedure; tuning inside the envelope; when to notify; the hard stops. The launch-time pre-authorization exchange. The agent scenarios of testing §5.
- **Refs.** agent §3, §5, §6, §9; testing §5, §7 q2; workflow §10.7.
- **Done when** the three bound scenarios pass — refusing signoff, raising a definition change as a question, notifying with kind `stopped` rather than only speaking into the conversation.

**Deliberately last before ship, and after the M3 gate it appears to belong to.** A runbook is a set of rules for operating machinery, and rules written against machinery nobody has operated are guesses. Steps 18 through 23 each surface rules as a side effect of being built — what the summary makes obvious, what the anomaly lifecycle actually asks of a reader, which escalations turn out to matter — and those accumulate as notes rather than as a document. This step is where they become one.

The split step 2 made keeps the cost of that honest. The **bounds** did not need discovering — they follow from decisions taken in other documents, so they ship from the start and an agent is never unbounded. What waits is the **operational** half, and until it lands, running overnight means a human in the session each time. That is a slower path to the same place, and it is also how the rules get discovered rather than invented.

## 8. Hawk in the pod — after ship

[hawk.md](hawk.md) §11 stages Hawk in three, and only the third is here.

**Stage 0** — read and run a Hawk config — is done. **Stage 1** — Hawk on an ordinary machine, with the full workspace, tend loop, anomalies and signoff — is not separate work: a Hawk config is a definition type, so it falls out of steps 1–32. Its one Hawk-specific obligation is **step 7**, pulled to the front of execution for exactly that reason, plus two local caveats with no design content: `isolation: strict` hard-fails without `HAWK_RUNNER_PATCH_SANDBOX`, which only the Helm template sets, and `scan:` is rejected locally, so steps 24–26 have no Hawk path until this group.

**Stage 2 — Steward inside the pod — lands after ship.** It is architecture rather than configuration, it is the one stage needing a change on someone else's roadmap, and the three stages before it de-risk it. Nothing above waits on it.

### Step 34 — Blocking launch and exit codes

**Delivers** `steward launch --wait-signoff`: a process that holds the pod open for the whole lifecycle Steward defines.

- **Scope.** Workers as ordinary children rather than detached, since the runner is PID 1 and detaching buys nothing; the in-pod timer as a second driver of the same `reconcile`, sharing the run claim with an external tend; the exit-code mapping.
- **Refs.** hawk §7, §7.1, §8; exec §11.3.
- **Done when** the mapping is exercised end to end, including the non-obvious row: **terminal without signoff exits 0**, because a non-zero exit trips `backoffLimit` and the restarted runner resurrects the eval.
- **Also settles** hawk §12 q2 — how long a parked run waits before its deadline fires, and what the timeout writes.

### Step 35 — The relay surface

**Delivers** driving a pod-resident Steward from outside.

- **Scope.** A loopback TCP server inside the pod — Inspect's control channel cannot be borrowed, since the bind is hardcoded `AF_UNIX` with a PID-derived path and a `SO_PEERCRED` check, and `acp_server`'s `int → TCP 127.0.0.1:<port>` path is the precedent. A `steward --remote` client shaped by the relay's limits: **one connection per command**, because a five-session-per-principal cap and a 900-second idle timeout that keepalives do not reset both punish a pooled client. Recording a relay signoff as claimed-but-unverified, and declining to add a shared-token scheme below a real gate.
- **Refs.** hawk §9, §9.1, §9.2.
- **Done when** a full tend cycle runs over `hawk attach` without approaching either limit.

### Step 36 — The Hawk call site ⚠ Hawk-side

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

**Concurrency tuning split across the M2 gate (9 and 20).** The mechanism — the control channel client — is a wire protocol testable against one live worker, and `pause` and adjudication need it anyway, so it lands early in execution. The policy — signal, envelope, ratchet — needs the tend summary to carry the signal, so it lands immediately after step 18. Nothing breaks without the policy; the run is only slower, which is why the M2 gate does not wait for it.

**Notification before adjudication (23 before 24).** Anomalies need somewhere to escalate, notification is independently testable, and the channel's shape constrains the lifecycle.

**The runbook last before ship (33).** Argued in §7 above. It is the one step placed by an argument about *how design happens* rather than by a dependency.

Three steps are placed by external dependency rather than by design, and each is called out where it appears: **sandbox division (26)** would sit across steps 5 and 6 if upstream items 9 and 10 existed, **smoke (31)** would sit beside launch if item 8 did, and **worker startup at scale (17)** waits on item 5 — though half of it, the memory guard, is placed by a real dependency on launch.

## 10. The test budget

Thirty-six steps each adding "just a few end-to-end tests" is how a suite reaches twenty minutes, and by then no one runs it before pushing. Step 1 measured what the cost actually is, so the rest of the plan can be held to a number instead of an intention.

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

**Which steps genuinely need real workers**: 1 (done), 6–9, 11, 16, 19, and parts of 12, 14, 24, and 27–29 — call it twelve. Steps 2–5, 10, 13, 15, 17–18, 20–23, 25–26, and 30–32 are layer 1 or near it: synthesized state, pure functions, no eval runs at all. **Budget ~12 launches for a layer-2 step**, which is roughly 35s serial and under 10s with `-n auto`. Ten such steps lands the whole suite near five minutes serial and one to two minutes on CI. That is the line; a step that wants more should say why in its design pass.

**Running total after step 9**: 205 offline tests, 36s with `-n auto`. Step 9 added sixteen tests, one launch, and about ten seconds — nearly all of it in `inspect ctl` invocations rather than in evals, which is a cost category the budget below did not have. Step 6 was the first layer-2 step since step 1 and came in at 12 launches, exactly the budget — but eight of those are *relocated* rather than new, because the two step-1 selection tests that ran the production shape moved onto the real spawn instead of being duplicated beside it. Step 7 added one test and three launches, step 8 eighteen tests and three. Wall time has moved by about three seconds across all three, which is rule 1 working.

Step 8 is the clearest case yet for rule 3: the scan is a parameter, so twelve of its eighteen cases are a table over a stubbed one, and the three launches are spent only where the process boundary genuinely is the subject — a worker held in the window before its eval, one killed inside it, and one that finishes. Its one deliberate cost is a five-second sleep in the gated fixture, bought because a socket that exists for less time than it takes to look for it cannot be asserted on.

It also found a fourth category the rules did not name: **a real process that is not an eval**. Proving that a worker's children are not the worker needs actual parent-and-child processes and no eval whatsoever, and `python -c "time.sleep(120)"` costs about 50ms. Four of them appear here. The rule is worth stating — *when the process boundary is the subject but the eval is not, do not launch an eval* — because the instinct is to reach for the fixture that already exists.

Step 9 added a fifth, which sits between the two: **an `inspect ctl` invocation costs ~1.3s**, all of it importing inspect_ai, so it is a tenth of a launch and two orders of magnitude more than a function call. Two consequences fell out of that number rather than out of taste. A live test must not spend invocations casually — the one here folds polling and pid-filtering into a single call — and **the eval has to outlast them**, which is why the gated fixture's sleep became settable. The other half of the step is table-driven over a `_decode` split out from the subprocess, which costs nothing and covers the outcomes no test can manufacture: a wedged event loop, a malformed body, an error kind this version has never heard of.

One thing the budget did not anticipate: **the two measurements step 7 took are not tests and should not become them.** Timing a flow worker against a baseline, and racing two `uv pip install` to see whether uv locks, each answered a question the design had been guessing at — and each would be a slow, flaky, third-party-dependent test if pinned. Measure, write the number in the design with its date and method, move on.

**Levers held in reserve**, in the order they become worth their complexity: cache captures across tests in a session (deterministic by the contract in configuration.md §4, so it is safe — but xdist gives each worker its own cache, so it pays off only once a single xdist worker runs many tests); share one worker run across several assertions; and, last, split the suite so layer 2 runs on a different cadence than layer 1.

## 11. What this plan does not decide

- **The internals of any step.** Each gets a design pass, starting from its **Refs** line.
- **Sizing.** [roadmap.md](roadmap.md) §3 declines to attempt dates and this document does too.
- **Where ship falls** between the M3 gate and step 33. Only that it falls before §8.
- **The open questions.** Twenty-odd remain across the docs, distributed over the steps that own them. None blocks step 1.
- **The one real gap.** Nothing surfaces an agent's mistake ([roadmap.md](roadmap.md) §7). It belongs to step 22 and is not yet solved there.
