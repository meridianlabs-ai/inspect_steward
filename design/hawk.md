# Hawk

[Hawk](https://github.com/METR/hawk) is a platform: a CLI, an API server, a Helm release, a runner pod, secrets, sandboxes, and an ingestion pipeline. Steward is a runner. This document works out where the two meet.

[configuration.md](configuration.md) deferred Hawk support and named the reason it would come back:

> The right shape is the one used for Flow: **Hawk's runner is itself a conforming program** (config in, one `eval_set()` call out), so Steward should drive Hawk's entrypoint under the protocol rather than re-deriving it.

That is still right, and two things since have made it cheap. Selection mode landed, including the schema-v2 operational overrides (`log_dir`, `max_samples`) — which turn out to be exactly the two levers this integration needs. And reading Hawk's runner closely confirms the conformance claim precisely enough to build on: one `eval_set()` call, at one line, reached by a documented command.

So this document is not a reversal. It is the part configuration.md left as a sentence.

## 1. Hawk's runner is a conforming definition

`hawk/hawk/runner/run_eval_set.py` makes a single `inspect_ai.eval_set()` call, inside `eval_set_from_config()`. It is the synchronous facade, called from a plain `def main()`; in the Kubernetes path no event loop is running at all, so `eval_set()` owns the main thread and makes its own. Everything above it is config loading and lowering. Everything below it is Inspect.

(Line numbers are deliberately absent for Hawk. It is an external dependency on its own release cadence — every citation taken against `main` while writing this had drifted by roughly eighty lines against the released 2.4.0 that `hawk[cli,runner]>=2.4.0` actually installs. Symbols survive that; line numbers do not. Inspect_ai citations keep their line numbers, because we develop it.)

The runner learns its config from two positional argv paths — never environment variables:

```
python -m hawk.runner.run_eval_set /etc/hawk/user-config.json /etc/hawk/infra-config.json
```

which is exactly what the Helm job template passes (`helm_chart/templates/job.yaml`, sourced from a ConfigMap mounted read-only at `/etc/hawk`).

That command *is* the definition. Steward's `DefinitionCommand` already exists to hold precisely this — a mode-agnostic argv that runs the definition normally, enumerates it under `INSPECT_EVAL_SET_CAPTURE`, and runs a subset under `INSPECT_EVAL_SET_SELECTION`. `_evalset/command.py` carries the comment for the flow case:

> flow's own CLI is a conforming program: it culminates in the `eval_set()` call this definition describes

Hawk is the same sentence with a different module. `DefinitionType` gains `"hawk"`; `definition_command` gains one branch. There is no second architecture here — **Hawk support is a definition type**.

### 1.1 Capture gets Hawk's own lowering for free

The lowering configuration.md warned about re-implementing is `_load_tasks_and_models` in `run_eval_set.py`: tasks × solvers/agents × models, crossed into one flat list, with each combination becoming a `Task` through `task_with(task, dataset=…, model=…, solver=…)` in `_load_task`. Note that the model is baked into each `Task`, so `eval_set()` is never called with a `model=` kwarg at all.

Capture mode intercepts *after* `eval_resolve_tasks`, so the manifest sees Hawk's fully-lowered tasks — the real cross product, with the real models, computed by the real code. Steward reimplements none of it. The three divergences configuration.md listed (invisible infra config, ignored `runner.environment`, absent secrets resolution) all dissolve, because all three happen in the runner before the boundary.

Capture then ends the process with `raise SystemExit(0)` (`inspect_ai/_eval/evalset.py:585`). `SystemExit` derives from `BaseException`, so Hawk's `except Exception` in `lifecycle.execute_runner_main` does not catch it: the `finally` blocks run, telemetry flushes, and the process exits 0. Enumeration through Hawk's entrypoint needs no cooperation from Hawk.

### 1.2 Detection

A Hawk config is a YAML (or JSON) document that validates as `EvalSetConfig` (`hawk/hawk/core/types/evals.py`). Its one required field is `tasks`; distinctive keys are `runner`, `packages`, `secrets`, `isolation`, `acp_server`, `model_cost_config`, `human_eval`, and `approval_timeout_minutes`.

Today `_detect_yaml` validates against `FlowSpec` only, and a Hawk config fails as an invalid flow spec — the outcome configuration.md wanted to avoid. With both formats in play, detection tries each and reports which matched; a document that validates as both is ambiguous and should say so rather than silently pick, since `--type` exists for exactly that. Both models are permissive in ways that make overlap plausible (`EvalSetConfig` is `extra="allow"`), so the ordering is a real decision and not an implementation detail — see open question 1.

## 2. Two configs, and no loader to borrow

Hawk splits configuration in two, and the split is meaningful: **the user config says what to evaluate, the infra config says where and how hard**.

`EvalSetConfig` is user-authored YAML. `EvalSetInfraConfig` (`evals.py`) is platform-supplied — built by the API at `api/eval_set_server.py`, serialized into Helm values, and rendered into the ConfigMap. It carries `job_id`, `log_dir`, the retry family, the concurrency ceilings, logging options, and the sandbox-patching fields.

There is no public loader. Every call site re-implements *read text → `ruamel.yaml` safe load → `model_validate`* (the CLI at `cli/cli.py`, `cli/local.py`, the entrypoint at `entrypoint.py`, the runner in `main`). What *is* stable: the models are exported from `hawk.core.types`, and there is a published `hawk/hawk/api/EvalSetConfig.schema.json`, regenerated by `python -m hawk.core.types`.

Steward should depend on as little of that as possible. Detection needs the schema or the model; execution needs neither, because execution runs Hawk's own command. Two validation layers exist that Steward deliberately will **not** reproduce: the CLI's unknown-key warning pass (`cli/cli.py`) and the API's semantic checks (no local package paths, secret validation, model permissions). Those belong to the frontends that own them.

### 2.1 Steward does not supply an infra config, and does not need to

An earlier draft of this document assumed Steward would hand Hawk its own infra config, on the reasoning that the file is Hawk's platform-half lever and the API server writes one. Implementation ruled that out: `hawk local eval-set` takes no infra config, so driving Hawk's CLI means Hawk synthesizes `_default_local_infra_config` for itself. Steward could only write one by driving `python -m hawk.runner.run_eval_set <user> <infra>` instead, which costs the pre-boundary work the CLI exists to provide.

It turns out not to matter, for two reasons worth recording because they were the worry:

- **The `log_prior_attempt` scan is cheap locally.** `main` calls it before `eval_set_from_config`, and it reads up to 5,000 log headers 32-wide. That is expensive against the pod's S3 `log_dir` — but a local run's synthesized `log_dir` is a relative `logs/<random job id>/` that does not exist, so the scan finds nothing. Under capture it is waste measured in milliseconds, not an S3 round trip. (Per *worker* it is still the growing cost Flow has; see *Pre-boundary work that must not be per-worker*.)
- **Capture never touches the directory at all.** It exits at `evalset.py:585` before `log_dir` is used, verified by the absence of any `logs/` directory after a read.

The levers that do the work are the selection document's `log_dir` and `max_samples` overrides, which apply uniformly to every definition type and are how a smoke run reaches a temp directory whatever the frontend. In the pod the question disappears from a different direction: there the infra config is the platform's, handed to the runner, and Steward is a consumer of it rather than its author — which is what *What Steward must honour* is about.

## 3. What Steward must honour

Three things in the infra config are not preferences:

- **`job_id` → `eval_set_id`.** Hawk passes `eval_set_id=infra_config.job_id`. The Helm release, the pod labels, the S3 prefix, the ingestion pipeline, and `hawk eval-set --eval-set-id <id>` all key off it. Steward stamps the same value or the platform loses the run.
- **`log_dir`.** `f"{settings.evals_s3_uri}/{eval_set_id}"`. Log ingestion into Postgres is event-driven off S3 object-created notifications — nothing tells the platform where else to look.
- **Model-group annotations and labels.** `lifecycle.build_annotations_and_labels` stamps these onto sandbox pods, and they are what the relay's authorization later reads. This one Steward gets for free: workers re-execute the runner, so the runner does it.

That last point generalizes. Because a worker is the whole runner again, everything Hawk does before the boundary happens in every worker — secrets resolution, sandbox patching, annotations, `runner.environment`. Steward does not need to learn any of it.

## 4. The budget hazard: per-process ceilings meet multiplied processes

This is the one place where "run the runner N times" is actively wrong rather than merely wasteful — but not for the reason it first appears, and the difference decides what has to be fixed.

Every ceiling in the infra config is written as a budget for *one process*, because Hawk has always had exactly one. What matters is which of them were already being multiplied by something before worker mode arrived. [scheduling.md](scheduling.md) works this out in general; applied to Hawk's numbers:

| field | default | scope | under N workers |
|---|---|---|---|
| `max_samples` | **1000** | per *task* | 1000 × N — but N replaces `max_tasks`, so this **improves** |
| `max_tasks` | **1000** | per process | moot — a worker runs one task |
| `max_connections` | 100 hint | per process, adaptive | N independent controllers — **newly multiplied** |
| `max_sandboxes` | computed | per process | 500 × N — **newly multiplied** |

**`max_samples` is the false alarm.** It bounds concurrency *within a task*, not within a process, so single-process Hawk was already multiplying it by however many tasks ran at once — up to `max_tasks`, which is also 1000. Worker mode swaps that multiplier for the worker count, which Steward caps at ten by default ([scheduling.md](scheduling.md), *Launch everything, up to a ceiling*). Fifty concurrent tasks in one process is 50,000 potential samples; ten workers is 10,000 — and Steward's ramp floor of 40 makes the launch figure 400, climbing only as clean windows buy it and never past the machine's sandbox budget ([scheduling.md](scheduling.md) §3.5–3.6). The knob that looked like the hazard is the one worker mode makes *smaller*.

**`max_connections` is the one the table used to omit.** It is process-global and adaptive, so one process means one AIMD controller converging on what the provider will bear, and N processes mean N controllers each ramping independently toward the ceiling hint. This needs no fix: [scheduling.md](scheduling.md) argues that rate limits are the coordination mechanism — a shared 429 is felt by every controller at once, and they all cut together. Independent controllers on a shared bucket converge on a shared answer.

**`max_sandboxes` is the real one**, and it is not a static default but a runtime computation. `_apply_config_defaults` (`run_eval_set.py`) mutates the infra config in place, setting `max_sandboxes = min(total_max_connections * 2, 500)` — where `total_max_connections` is derived from the distinct provider keys across the loaded models and the adaptive-connections ceiling hint of 100. Each worker computes it independently, from its own single task, and each gets a number sized for the whole eval set. There is no backpressure signal to discover the mistake with; the pod simply dies.

Hawk lands cleanly on scheduling.md's rule, because computing the value explicitly is exactly the case that rule handles best: **an explicitly configured `max_sandboxes` is a machine budget, and Steward enforces it as a cap on the fleet-wide sum of sample setpoints** ([scheduling.md](scheduling.md) §3.6 — the earlier divide-by-workers rule is superseded). No provider sniffing is needed — the infra config already committed to a number, and setting it means Docker's own `default_concurrency()` (whose `os.cpu_count()` misreads a pod's cgroup quota) is never consulted, which retires the cgroup wrinkle here entirely.

The narrower claim replaces the old blanket one: it is not that every knob must be bounded by Steward, but that **the ones with no feedback signal must be**. Connections coordinate through rate limits; samples were never the multiplier they appeared to be; sandboxes have nothing, so Steward caps their sum. Within that bound the infra config's values stay an *envelope* rather than a quota to split evenly — different tasks with different models want different `max_samples`, so the tuning loop discovers each one's level inside it ([scheduling.md](scheduling.md) §3.5) and the agent holds the climb when the right answer is unclear. What neither may do is exceed it, because unlike wall clock, exceeding this one kills the pod.

## 5. Hook scope decides what survives worker mode

`lifecycle.install_runner_hooks(infra_config)` installs Hawk's Inspect hooks, and they do not all fare the same way under worker mode. The dividing line is not Hawk's — it is the scope each handler is registered at.

**Run- and sample-scoped handlers fire in every worker, which is exactly right.** A worker is a real eval in a real process, so:

- **`refresh_token`** rewrites the model API key through `override_api_key` — a per-request override, not a lifecycle event. It is load-bearing on long runs, and every worker refreshing its own key is what is needed. A design that held the eval in one process and put workers elsewhere would have broken it.
- **`stop_monitor`** (`on_run_start`/`on_run_end`, plus sample-attempt handlers) polls the S3 stop marker. Every worker polls the same marker and cancels itself, so **`hawk stop` keeps working unchanged**, with no cooperation from Steward at all.
- **`otel_tracing`** and **`observability_headers`** are run-, sample-, and model-scoped throughout.

**Eval-set-scoped handlers fire nowhere.** Selection mode returns at `inspect_ai/_eval/evalset.py:648`; `emit_eval_set_start` and `emit_eval_set_end` are at `:899` and `:937`. A worker never reaches them, and it should not — a worker is not running an eval set. Steward is. So these handlers are silently dropped:

| hook | lost handlers | consequence |
|---|---|---|
| `datadog_metrics` | `on_eval_set_start`, `on_eval_set_end` | the `inspect.eval_set.active` gauge is never set or cleared (its `on_model_usage` / `on_sample_event` handlers still fire) |
| `cloudwatch_metrics` | `on_eval_set_end` | no eval-set completion metric |
| `stuck_eval_monitor` | `on_eval_set_start`, `on_eval_set_end` | **the watchdog is never armed** — its sample handlers still fire, but nothing brackets them |

This is not a Hawk bug and not a selection-mode bug. It is the exact, intended consequence of removing the competing orchestrator: **eval-set-scoped hooks become the responsibility of whoever is running the eval set**, which under Steward is Steward. It generalizes past Hawk — any platform with eval-set-scoped hooks loses them the same way — so the question of whether Steward fires them, or exposes its own equivalent, belongs in [execution.md](execution.md) rather than here.

The `stuck_eval_monitor` row is the one that matters most, because it is a safety mechanism rather than a metric, and losing a watchdog silently is worse than losing a gauge. Steward's tend loop covers much of the same ground — a worker that stops making progress is exactly what reconciliation notices — but "covered by something else" is a claim to verify, not assume. It is at least now a claim with something to check it against: [execution.md](execution.md)'s *The stuck sample* designs the sample-level half explicitly, off the control channel rather than from inside the worker. What remains to compare is the part a design cannot answer from here — Hawk's thresholds, and whether its watchdog *acts* where Steward reports and escalates within policy.

One more duplication, unrelated to hooks: **`memory_monitor`** starts a daemon thread polling the *cgroup*, launched from `execute_runner_main` rather than registered as a hook. One cgroup, N pollers, N identical gauge streams. See open question 5.

## 6. Pre-boundary work that must not be per-worker

A worker is the whole runner again — that is what makes Hawk support a definition type rather than an architecture. The cost is that everything Hawk does *before* the `eval_set()` boundary happens N times. Sorting that work by whether repeating it is right, merely wasteful, or actively unsafe is the main thing the runner has to get right, and it is not the same list as the hooks above.

**Correct per worker.** Environment application, the run- and sample-scoped hooks, `ptrace.allow_any_tracer()`, config parsing. These establish process state; a worker that skipped them would be wrong, and the fact that they repeat is the reason worker mode preserves token refresh and `hawk stop` for free.

**Wasteful.** `prior_attempt.log_prior_attempt` reads up to 5,000 log headers from `log_dir` before *every* worker starts, the direct analogue of Flow's pre-boundary directory scan ([execution.md](execution.md) open question 1) — but **measurably cheaper than Flow's, which is the opposite of what this section used to assume**: 300 logs in the directory added 0.04s to a worker, against Flow's ~2.9ms per log, because hawk reads headers 32-wide through a thread pool where Flow's scan is effectively serial. On local disk it extrapolates to under a second at the 5,000 cap. The width exists "for S3 round-trip latency", though, and that is where it still bites: 5,000 headers at 32-way concurrency is ~160 sequential round trips per worker. `_load_tasks_and_models` builds the entire cross product in every worker, which is general rather than Hawk-specific (execution.md open question 3). Double config parsing, annotations, and labels are cheap enough to ignore.

**Unsafe.** One item, and it is not the one this section originally led with:

- **`_resolve_aws_sourced_secrets` and `_setup_provider_env_vars`** make remote calls — Secrets Manager, and the gateway for middleman routing. N workers means N× those calls at startup, all at once. Throttling is the plausible failure, and it would present as a confusing burst of worker startup failures. Nothing Steward can do about it from outside, because what these produce is process state rather than files: there is no directory to redirect and no result to hand on unless Hawk reports it.

**Reclassified: `install_into_current` is wasteful, not unsafe.** `--direct` does not mean *skip installing*; it means *install into the current interpreter instead of a new venv*, and `entrypoint._run_module` calls it unconditionally, so N workers do run N `uv pip install` commands against one shared environment. Three things make that redundant rather than dangerous, and the first two were verified rather than reasoned:

- **uv takes an exclusive lock on the target environment.** Two concurrent `uv pip install` into one venv serialize: the second logs `Waiting to acquire exclusive lock for '.venv' at '.venv/.lock'` and both succeed. Concurrent installs queue; they do not interleave.
- **Capture has already installed, before any worker exists.** `read_eval_set` runs this same command under `INSPECT_EVAL_SET_CAPTURE` in Steward's own interpreter, so by the time `launch` spawns anything the packages are present and every worker's install is a satisfied no-op.
- **The downgrade hazard is real but is not about fan-out.** Against a PyPI-versioned inspect-ai, Hawk emits `inspect-ai==<exact>` and installs it into the running interpreter — *the environment the runner itself is executing in*. That happens at N=1, during `steward tasks`. Fan-out neither causes it nor worsens it, so it belongs with the ambient hazards of driving Hawk in-process rather than with the fan-out ones.

  A smaller edge of the same fact: Hawk invokes a bare `uv`, resolved through `PATH`, and does not declare it as a dependency. Steward's `[hawk]` extra supplies it and the Hawk branch of `definition_command` puts the interpreter's directory on the child's `PATH`, since pip installs `uv` beside the interpreter and that directory is on `PATH` only when the venv is activated. Both disappear if Hawk declares `uv`; neither is load-bearing for the design.

**So Steward would still like to own the once-per-run half**, but it no longer *has* to before Hawk fan-out is usable. The candidates:

1. **A Hawk-side skip** — a flag or environment variable meaning "the caller owns dependency installation", paired with a way to **report** the environment it resolved so the caller can hand that to each worker. Small, and the right shape: it is the same ask Flow needs for its pre-boundary artifacts, so the general form is *a way for an external runner to say it owns the once-per-run work*. The reporting half is what makes Hawk's case harder than Flow's — secrets and provider variables are process state, not files, so there is nothing to redirect. Preferred, and still outstanding.
2. **Split entry points** — one setup pass through `hawk local eval-set --direct`, then workers through `python -m hawk.runner.run_eval_set`, which bypasses `entrypoint` entirely and so never installs. Attractive because it needs nothing from Hawk, and `DefinitionCommand.env` already exists to carry a resolved environment to workers. Fatally flawed as stated, though: `run_eval_set.main` does no secrets resolution either, so Steward would have to re-derive from the config the very thing driving Hawk's CLI exists to avoid. It only works given candidate 1's reporting half.
3. ~~**Serialize worker startup** behind a lock.~~ **Declined, not deferred.** It was the answer to concurrent installs corrupting a shared environment, and that is not what happens: uv serializes them itself, and capture has already done the install before the first worker starts. Buying that with minutes of serial worker startup — Hawk's pre-boundary window is the longest of any frontend — would be paying a real cost for an averted hazard.

**None of this blocks Hawk fan-out**, which is a change from how this section used to read. What it costs today is N× startup remote calls and N× redundant installs: waste and a throttling risk, not corruption. It remains Stage 1's to improve.

## 7. Why `launch` must block inside the pod

Everywhere else, `steward launch` returns immediately and a coding agent tends the run. Inside a Hawk pod that model is unavailable, and the reason is worth stating precisely, because it is a platform contract rather than a preference.

**Process exit is run completion, for the entire platform.** `restartPolicy: Never`, `backoffLimit: 3`, `ttlSecondsAfterFinished: 3600`. A Complete or Failed Job condition drives `fetch_job_terminal_state`; the monitoring path collapses status to `complete`/`failed` once no pod is in an active phase; the janitor CronJob `helm uninstall`s the release an hour later.

**Detaching buys nothing.** There is no reaping code anywhere in `hawk/hawk/runner/` — no `killpg`, `setsid`, `waitpid`, or `atexit`; `process_tree.py` only reads `/proc` for OOM diagnostics. But the runner is PID 1 of the container, so when it exits the kubelet tears down the PID namespace and takes every child with it. Nothing survives the parent regardless of how it was spawned. Worse, the runner's own `finally` (`run_eval_set.py`) calls `common.cleanup_s3_sessions_blocking()`, which closes cached `s3fs` sessions and clears the instance cache — so any surviving process would be writing logs through a torn-down client cache.

**Hawk already accepts a parked runner.** `lifecycle.stay_alive_if_cleanup_disabled` (`lifecycle.py`) holds the process open past `eval_set()`, polling an S3 marker every 30 seconds, and deliberately returns **exit 0** — because a non-zero exit would trip `backoffLimit` and the restarted runner would resurrect the eval. And `approval_timeout_minutes` defaults to `DEFAULT_APPROVAL_TIMEOUT_MINUTES`, written in the source as `7 * 24 * 60` — Hawk's tolerance for a runner blocked on a human is already measured in days, deliberately.

So the shape is `steward launch --wait-signoff`:

1. capture the manifest through Hawk's entrypoint;
2. spawn workers as ordinary children — not detached, since detaching is meaningless here and being a child makes reaping easy;
3. bind the RPC surface on a loopback port;
4. run the mechanical tend on a timer;
5. return when the campaign is signed off.

The process holds the pod open for the whole lifecycle Steward defines — tasks complete, scans drain, anomalies settle, a human signs off — and only then lets Kubernetes call the Job complete. That is a *better* fit for the platform's semantics than what happens today, where Job-complete means "the eval loop returned" and the question of whether the data is usable has no representation at all.

### 7.1 Exit codes

The mapping is load-bearing, because it decides whether the pod restarts:

| outcome | exit | effect |
|---|---|---|
| signed off | 0 | Job Complete |
| terminal without signoff (deadline, cancellation) | **0** | Job Complete, unsigned state recorded in the journal |
| `PrerequisiteError` / `TaskLoadError` | 78 | `FailJob` via `podFailurePolicy` — no retry |
| any other exception | 1 | retryable; `backoffLimit: 3` restarts the pod |

The second row is the non-obvious one, and it follows `stay_alive_if_cleanup_disabled`'s precedent exactly. A run nobody signed off is not a *failed* run — the eval may be perfectly complete — and exiting non-zero would restart the pod and re-run the work. The absence of signoff is a fact for the journal, not a process failure. Which means a parked run needs a deadline: see open question 2.

## 8. Two drivers, one function

If the pod's process must live for the whole run, something inside it must decide when to spawn and reap. That looks like the daemon [execution.md](execution.md) rejected, and it is worth being clear that it is not.

The daemon was rejected because its headline justification — low-latency requeue of transient failures — required classifying a failure as transient, which is judgement, and the mechanical layer explicitly does not do judgement. That boundary is unchanged here. The in-pod loop runs the *mechanical* tend: reconcile the manifest against the log directory, spawn, reap, sync the workspace out. When it meets something requiring judgement it opens an anomaly, notifies, and waits. It never adjudicates and it never signs off.

The external coding agent supplies the judgement, over the relay. So there are two drivers of one function, and the function is the same pure `reconcile(manifest, inflight, log_dir) -> (actions, summary)` in both. Concurrency between the internal timer and an external `steward tend` needs no new machinery: the run claim is short-lived by design — seconds, not hours — and this is the case it was designed for.

The alternative, an external agent as sole driver, is rejected. A pod is expensive and an agent can stop being called; a run that stalls because nobody woke up leaves a live pod burning money with no worker running. The timer is the floor, not the ceiling.

This split is not peculiar to Hawk, and [execution.md](execution.md), *Driving and judging are separate roles that usually coincide*, now states it generally: driving is mechanical so a clock suffices, judging is not so only an agent will do. The pod is simply the deployment where the two roles land in different places.

## 9. The relay surface

`hawk attach --port N` opens a loopback TCP listener on the operator's machine and pumps its bytes to the runner pod over an authenticated WebSocket. Hawk's own description of it (`hawk/cli/acp.py`):

> The relay is a dumb byte pipe; this bridge never parses payloads.

Everything follows from that. It works for **any** port 1–65535, with no allow-list and no `containerPort` declaration required — the runner container's `job.yaml` has no `ports:` block at all, yet the ACP path works through this same code. Transport is the Kubernetes `pods/portforward` subresource, so the only requirement on the target is that something is listening on the pod's loopback interface. Authorization is per-*run* — write access to the run's model groups — and the port never reaches the authz path at all.

Steward must host its own TCP surface, because Inspect's control channel cannot be borrowed: `CtlServerConfig` has no host or port field, the bind is hardcoded `AF_UNIX`, the socket path is derived from the PID, the client is hardwired to `httpx(uds=...)`, and there is a `SO_PEERCRED` peer-UID check on top. Only `acp_server` has the `int → TCP 127.0.0.1:<port>` path, and that is the precedent to copy: a pod-internal loopback port, declared in config, bridged out on demand.

### 9.1 The limits shape the client

| limit | value |
|---|---|
| idle timeout | 900 s — keepalives deliberately do **not** reset it |
| max session lifetime | 4 h |
| buffered bytes per direction | 4 MiB |
| concurrent sessions | 40 global, **5 per principal** |
| WebSocket sessions | **one per accepted local TCP connection** |

Two of these decide the client design. A fresh relay session per TCP connection plus a five-session cap means a pooled HTTP client is wrong — six sockets and the sixth is refused. And the 900-second idle timeout is uncomfortably close to a ten-minute tend cadence: a held-open connection would sit five minutes from the cliff, on a clock that keepalives do not touch.

Both problems have the same answer: **one connection per command**. The `hawk attach` bridge stays up; the sockets through it do not. An idle bridge holds no WebSocket, so there is no clock to run out, and a CLI making one request at a time never approaches the session cap. This is also the natural shape for `steward --remote http://127.0.0.1:<local> tend` — a command, a connection, a response, a close.

### 9.2 Who signed off

The relay forwards no identity. It is a byte pipe, and the loopback port inside the pod is unauthenticated for the session's lifetime — Hawk's docs say so explicitly, comparing it to `kubectl port-forward`. So a signoff arriving over the relay cannot be authenticated by Steward.

The honest position: the journal records that a signoff arrived over the relay and the identity the caller *claimed*, marked as unverified. The authenticated record is the relay's own access log plus the Kubernetes audit trail, where the principal that opened the session is known. Steward's journal is the record of what was decided; it is not, and should not pretend to be, the record of who was authorized to decide it.

What Steward should **not** do is add a shared-token scheme on the loopback port. The token would have to live in the ConfigMap next to everything else, which makes it a formality rather than a control, and the real gate already exists one layer up: attaching at all requires write access to the run's model groups. A second, weaker gate below a real one buys nothing but the appearance of rigour. (One sharp edge in the real gate is worth knowing about: a run whose pods carry no model-access annotation yields an empty required-groups set, and the authorization check passes any authenticated principal.)

## 10. What each side gains, and pays

Hawk gains three things it does not have:

- **Failure granularity.** Today the recovery unit is the whole pod: `restartPolicy: Never` plus `backoffLimit: 3` means one OOM restarts the world, and Inspect resumes from the shared log directory with in-flight evals cancelled. Per-task workers make the blast radius one task.
- **A structural record of re-run cost.** `prior_attempt.py` exists solely to *log* this — its docstring reports a production run where three OOM kills turned 4,588 logical samples into 6,013 attempts, "and nothing said so" (METR/hawk#936). It reads up to 5,000 log headers to print a warning, writes nothing, and changes no behaviour. Steward's journal is that record, kept as it happens rather than reconstructed after.
- **Adjudication and signoff**, which have no equivalent at all. Job-complete currently means the eval loop returned. It says nothing about whether the data is usable, and `anomalies.md` is the artifact that answers that question.

It pays: the dropped eval-set hooks and duplicated cgroup polling above, and the per-worker pre-boundary work — `install_into_current` and the N× secrets and provider-env calls, plus `log_prior_attempt`, which is the same cost recorded for Flow in [execution.md](execution.md) except that Flow's could be redirected to scratch and Hawk's cannot. All of it points at one upstream ask, which is also the cheapest thing Hawk could do for this integration: **a way for an external runner to declare that it owns the once-per-run work**, so a worker skips installation and the prior-attempt scan — paired with a way for Hawk to *report* the environment it resolved, since secrets and provider variables are process state a runner cannot obtain any other way. A small change on Hawk's side, and what it buys is startup latency and throttling headroom rather than safety.

## 11. Staging

Nothing here needs to land at once, and the stages are separated by who has to change.

**Stage 0 — read a Hawk config. *Done.*** Detection, the `definition_command` branch, capture through Hawk's entrypoint. `read_eval_set()` enumerates a Hawk config into a manifest reflecting Hawk's real lowering (`steward tasks` shows it, as the enumeration diagnostic it is — see [workflow.md](workflow.md)). Verified end to end: two concurrent workers into one flat log directory, both logs carrying Steward's eval-set id, no eval-set metadata written, and `list_eval_logs`/`samples_df` reading the directory unmodified. Required nothing from Hawk. Its Python 3.13 floor is carried by the extra alone — marker-gated, so inspect_steward still supports 3.12, where the extra resolves to nothing and Hawk support is simply unavailable. The extra also has to supply `uv`, which Hawk shells out to on every invocation without declaring.

**Stage 1 — run a Hawk config outside the pod.** Steward as launcher on an ordinary machine; the full workspace, tend loop, anomalies, and signoff. Blocked on Steward's runner rather than on Hawk. **The first Hawk-specific problem it meets is the one above**, and it is smaller than this paragraph used to claim: the second worker's `install_into_current` queues behind the first on uv's environment lock rather than racing it, and capture has already installed by then, so what fan-out actually costs is startup latency and N× remote calls. *Pre-boundary work that must not be per-worker* is Stage 1 work to improve, not to unblock. Two further local caveats: `isolation: strict` hard-fails without `HAWK_RUNNER_PATCH_SANDBOX` (which only the Helm template sets), and `scan:` is rejected locally.

**Stage 2 — Steward inside the pod.** Blocking launch, the relay RPC surface, the in-pod tend timer. **This is the only stage that needs a change on Hawk's side** — a runner type, or a flag, that calls Steward where `run_eval_set.py` currently calls `eval_set_from_config`. That is the delegation point configuration.md already anticipated, and it is one call site.

The ordering means the interesting design risk (stage 2) is de-risked by the two stages before it, and neither of those requires anyone else's roadmap.

## 12. Open questions

1. **Detection ordering between `FlowSpec` and `EvalSetConfig`.** Both models are permissive; `EvalSetConfig` is `extra="allow"`. Which is tried first, whether a document validating as both is an error or a precedence rule, and whether detection should use the published JSON schema rather than importing Hawk.

2. **How long a parked run waits.** A pod blocked on a signoff that never comes is a real cost. Hawk's own `approval_timeout_minutes` defaults to seven days, which is precedent for a long bound rather than no bound — but the deadline's default, whether it is configurable per run, and what a timeout writes to the journal are unsettled.

3. **Whether Steward divides infra budgets automatically or requires them restated.** *Resolved — automatically, and only where it means something.* [scheduling.md](scheduling.md) establishes that the three knobs are not one budget: `max_samples` is per-task and never was a fleet share, `max_connections` coordinates itself through rate limits across any number of processes, and only `max_sandboxes` is a genuine allocation. So the answer to the hard case is that Hawk computing it at runtime is exactly what makes it *safe* to divide — an explicitly set `max_sandboxes` is by construction a statement about the host, which is the case the general rule handles best, and no restatement is asked of anyone. The intent-guessing worry applied to a division of everything; it does not survive dividing only the one knob that is a division.

4. **`job_id` reuse on resume.** `hawk eval-set --eval-set-id <id>` relaunches into the same log directory with the same `job_id`. Steward's run claim is keyed to a run; whether a resumed Hawk job is the same Steward run (resuming its journal) or a new one (with a second journal over one log directory) is undecided, and the answer determines whether the journal survives a pod restart at all.

   Its sibling: **a local run drops a declared `eval_set_id`.** Hawk's cloud path honours `EvalSetConfig.eval_set_id` (`api/eval_set_server.py`), but `_default_local_infra_config` ignores it and synthesizes `local-eval-set-<uuid>` fresh per invocation — even though that same function deliberately mirrors `acp_server` and `approval_timeout_minutes` from the user config, which reads like an oversight rather than a decision. So `Manifest.eval_set_id` records a throwaway id where the user may have declared a real one, and it differs on every read of the same config.

   This is inert today: nothing reads the field, and workers take their id from the selection document, which is why the Stage 0 verification stamps Steward's own id into every log. It stops being inert when Steward has to choose the id it writes into `.eval-set-id` — adopting a fresh random id per read would give a resumed run a different eval-set id than the original, which is exactly the failure this question is about. Mirroring the field in `_default_local_infra_config` is a one-line upstream change; whether Steward should instead read the declared value itself is the part to settle alongside the runner.

5. **Who fires eval-set-scoped hooks, and whose problem is `memory_monitor`.** The dropped `on_eval_set_start`/`on_eval_set_end` handlers are a general worker-mode consequence, so the general answer — Steward emits them, or Inspect grows a way for a runner to — belongs in [execution.md](execution.md) (open question 11). What is Hawk-specific is whether `stuck_eval_monitor`'s loss is genuinely covered by Steward's tend loop — narrower now that the sample-level condition is designed rather than assumed ([execution.md](execution.md), *The stuck sample*), and reduced to comparing thresholds and whether Hawk's watchdog acts where Steward escalates — and whether `memory_monitor`'s N-way cgroup duplication is suppressed by Steward (easy, loses data) or fixed by Hawk (correct, not Steward's code).

6. **How Steward takes ownership of the once-per-run work.** The candidates are in *Pre-boundary work that must not be per-worker*, and the choice turns on a question only Hawk can answer: is a skip flag welcome, and can Hawk report the environment it resolved? The second half is the one that matters — without it, a runner that skips Hawk's setup has no way to obtain the secrets and provider variables that setup produced. The concurrency ceiling this question used to be framed around turned out not to exist: uv holds an exclusive lock on the target environment, so concurrent installs queue. What is unmeasured now is the throttling ceiling on N simultaneous Secrets Manager and gateway calls, which is a number only a real deployment can supply.

7. **Whether the in-pod tend cadence should match the agent's.** Ten minutes is a cadence chosen for a coding agent's attention, not for a timer. A timer could tend far more often, since a mechanical tend is cheap and `.eval` immutability makes scans incremental. Whether it should is a question about escalation noise, not about cost.
