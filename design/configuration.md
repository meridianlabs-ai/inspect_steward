# Configuration

**Status: draft for discussion**

How users define what an evaluation run contains, and how Steward turns that definition into work it can manage.

## Requirements

Steward is an autonomous evaluation runner: it launches, monitors, scales, diagnoses, and remedies on behalf of a human. That role imposes three requirements on configuration:

1. **Static enumeration.** Steward must be able to produce the full list of tasks (resolved task × model × solver combinations) *before* running anything, so it can schedule them, run them in their own processes, track completion, and estimate progress and cost.

2. **Meet users where they are.** There are three ways people already define eval sets, and Steward must support all of them without forcing migration:
   - A plain Python file that ends in a call to `eval_set()` — the code users naturally write today.
   - An [Inspect Flow](https://github.com/meridianlabs-ai/inspect_flow) spec (`FlowSpec`, as Python or YAML).
   - A [Hawk](https://github.com/METR/hawk) eval set config (`EvalSetConfig` YAML).

   Steward introduces **no fourth format**. The raw `eval_set()` file is the native mode.

3. **Side effects run everywhere.** Definition files do real work when executed: `set_model_info()`, dynamically constructed `Model` objects, environment mutation, dataset preparation. These effects cannot be captured in a static artifact, so *every* process that runs evaluation work must execute the definition itself — enumeration produces an index into the definition, never a replacement for it.

A fourth, structural decision frames everything below: **Steward owns all orchestration.** It never calls `flow run` and never relies on Hawk's single `eval_set()` invocation. Flow and Hawk contribute *definition formats* (and loading libraries); scheduling, retries, scaling, and supervision belong to Steward.

We maintain inspect_ai, so the mechanisms below that belong in inspect_ai are designed as inspect_ai features from the start — no interim patching or workarounds.

## The definition contract

The central abstraction: **a Steward definition is any program that culminates in a single `eval_set()` call.**

All three formats already satisfy this. A user's `evalset.py` *is* such a program. Flow's runner loads a `FlowSpec` and ends in one `eval_set()` call. Hawk's runner (`run_eval_set.py`) loads its config, crosses the grid, and ends in one `eval_set()` call. Because everything lowers to the same boundary, Steward needs exactly two capabilities, both implemented as interception at that boundary:

```
                        ┌─────────────────────────┐
                        │   execute the program   │
   definition   ──────► │  (all side effects run) │
                        └───────────┬─────────────┘
                                    │  eval_set() boundary
                    ┌───────────────┴───────────────┐
             enumerate mode                  execute mode (selection)
                    │                                │
                    ▼                                ▼
        resolve tasks × models,          filter resolved tasks to the
        write manifest, exit             selection, run only those
        the process
```

**Enumerate**: execute the definition; at the boundary, resolve all tasks (via `eval_resolve_tasks()` + `task_identifier()`), write the manifest, and **exit the process immediately** — code after the `eval_set()` call never runs, so result-processing side effects cannot fire against empty results. Steward's controller does this once per run, in a dedicated subprocess.

**Execute(selection)**: execute the definition; at the boundary, keep only the tasks named in a selection document, then run them. Steward's workers do this — typically with a single-task selection, one worker process per task. Because the worker ran the whole program, every side effect (`set_model_info()`, dynamic `Model` objects, env setup) is in place, exactly as if the user had run the file by hand.

A second `eval_set()` call in the same process under capture or selection is an error — the contract is one call per definition.

Dynamic task producers (`TaskSource`) are fundamentally incompatible with static enumeration and are **out of scope**: capture mode fails with a clear error when the definition passes a `TaskSource`.

### Protocol

The interception protocol is environment-based so that any conforming program works unmodified under any process manager (names provisional):

| Variable | Meaning |
|---|---|
| `INSPECT_EVAL_SET_CAPTURE` | Path to write the manifest. `eval_set()` resolves tasks, writes the manifest, and exits the process. |
| `INSPECT_EVAL_SET_SELECTION` | Path to (or inline JSON of) a selection document. `eval_set()` runs only matching tasks. |
| `INSPECT_EVAL_SET_OVERRIDES` | Whitelisted `eval_set()` kwarg overrides (`log_dir`, `display`, `log_level`, `retry_attempts`, `ctl_server`, ...) so Steward can control the runtime behavior of workers without editing definitions. |

The protocol lives in **inspect_ai** — `eval_set()` honors these variables natively. Flow and Hawk then conform with zero code changes, `python evalset.py` works directly under Steward, and any future frontend gets Steward support for free.

Steward exposes the enumerate side programmatically as `read_eval_set()`:

```python
def read_eval_set(definition: str, args: dict[str, Any] | None = None) -> EvalSetManifest:
    """Execute a definition in a subprocess with capture enabled and return its manifest."""
```

> **Naming note**: inspect_ai has an internal `read_eval_set_info(log_dir)` that reads the post-hoc `eval-set.json` manifest from a log directory — a different artifact (what ran) from ours (what is defined to run). Since the capture API lands in inspect_ai we should either unify the two or choose a name like `resolve_eval_set()` to avoid confusion.

## Tasks and identity

The unit of work is a **task**: a resolved task × model × solver combination — inspect_ai's `ResolvedTask`, corresponding 1:1 with an eval log. This follows existing precedent: inspect_ai's `eval-set.json` calls these entries `tasks` (`EvalSetTask`, each carrying its model) and Flow's crossed unit is `FlowTask`. Where prose needs to distinguish the definition (`@task my_task`) from the combination, we say "task definition" vs "resolved task".

Two identity layers:

- **`task_identifier`** (authoritative). inspect_ai's stable identity string — `task_file@task_name#args_hash/model/config_hash` — already used by `eval_set` to match logs to tasks for retry/resume, and by Flow for log reuse. It hashes task args, resolved plan/solver, generate config, limits, and version, so it distinguishes tasks that differ *only* in configuration (e.g. a temperature sweep over the same task definition and model). Steward uses it for exact matching and log correlation.

- **Display key** (human-facing). `task[solver]@model` — e.g. `mbpp[react]@openai/gpt-5` — used in the CLI, TUI, and logs. The solver segment is always present, showing the resolved primary solver/agent name (the task's default plan when no override is crossed in). Remaining collisions (args/config sweeps) are disambiguated with a short args/config summary or a sequence index (e.g. `mbpp[react]@openai/gpt-5 (temperature=0.7)`). Steward's CLI accepts patterns against display keys and reports the candidate list on ambiguity.

Full identifiers are only computable *after* task construction (they hash the resolved plan and config). This asymmetry — cheap partial facets early, exact identity late — drives the filtering design below.

## The manifest

The manifest is an **index into the definition, not a reconstruction of it**. The definition file remains the single source of truth; the manifest exists so Steward can schedule, display, and track without re-executing the definition.

```jsonc
{
  "version": 1,
  "eval_set_id": "swe-sweep-2026-08",
  "definition": {
    "type": "evalset",            // evalset | flow | hawk
    "path": "evalset.py",
    "content_hash": "sha256:…",   // staleness detection
    "args": {}                    // e.g. flow --arg values
  },
  "options": {                    // serializable eval_set kwargs, informational
    "log_dir": "s3://…/logs",
    "epochs": 3,
    "retry_attempts": 10
  },
  "tasks": [
    {
      "key": "mbpp[react]@openai/gpt-5",
      "name": "mbpp",
      "file": "evalset.py",
      "args": { "difficulty": "easy" },
      "args_hash": "9f3a…",
      "solver": "react",
      "model": "openai/gpt-5",
      "model_roles": null,
      "sequence": 4,
      "identifier": "<full task_identifier>",
      "samples": 500,             // dataset size, known at enumeration
      "epochs": 3
    }
  ]
}
```

Because enumeration fully resolves tasks, the manifest carries per-task sample counts and epochs — the raw material for Steward's scheduling, progress estimation, and cost projection.

**Determinism requirement.** The contract assumes the definition produces the same task list on every execution: enumerate on Monday, execute task 7 on Tuesday, and the selection must still match. `task_identifier` is deterministic given the same tasks/args/models, so the requirement reduces to "the definition is deterministic with respect to its task list" (no time- or randomness-dependent task construction). Drift is detected, never papered over: a selected task that resolution fails to produce is a hard error naming the missing task, and the manifest's `content_hash` lets Steward warn when the definition file changed after enumeration.

## Selection and filtering

A selection is a structured document, not a bare list of identifier strings — early filtering layers need partial facets that opaque identifiers can't provide:

```jsonc
{
  "tasks": [
    {
      "key": "mbpp[react]@openai/gpt-5",
      "name": "mbpp",
      "args_hash": "9f3a…",
      "model": "openai/gpt-5",
      "sequence": 4,
      "identifier": "<full task_identifier>"
      // reserved: "samples": [...] — future within-task sharding
    }
  ]
}
```

Filtering happens at two internal layers. **No user-facing filtering API is required** — users write exactly the code they write today.

### Layer 1: authoritative boundary filter

At the `eval_set()` boundary, after resolution: exact matching of resolved tasks against selection `identifier`s. Only this layer decides what runs. It also enforces drift detection (selected-but-unresolved task → hard error) and requires every selected task to match exactly one resolved task.

### Layer 2: automatic early pruning

The expensive part of running a filtered worker is constructing task definitions the worker will never run — datasets load at `Task` construction. Pruning happens automatically at two points, both inside inspect_ai, both invisible to users:

1. **Resolver-level.** For task specs that are not yet constructed — registry names, `file.py@task` strings, `@task` callables — the resolver knows the task name before construction and skips creating (or even importing) tasks that no selected task references.

2. **Construction-level.** The `@task` registry wrapper intercepts every task creation call and sees exactly the identity facets that matter: the registry name and the actual passed args, *before* the function body runs. When a selection is active and no selected task matches `(task_name, args_hash)` — args hashed identically to `task_identifier`'s scheme; model/solver/config facets deliberately ignored at this layer — the wrapper returns a lightweight **placeholder task** without invoking the function. No dataset loads. The placeholder carries the name, args, and a reference to the factory, and is dropped at the boundary before identifier computation.

Layer 2 covers all three frontends with no cooperation: users' `@task` calls in `evalset.py`, Flow's `instantiate_tasks` (which goes through `registry_create`), and Hawk's task loading (likewise) all pass through the same interception point.

**Safety property.** Early pruning is purely an optimization and can never change results. Pruning fires only on a *definite* `(name, args_hash)` mismatch, so a placeholder can never correspond to a selected task; and since placeholders retain their factory, a pruning bug materializes the task late rather than failing the run. Under-pruning costs time only. Correctness rests entirely on Layer 1.

**Edge case.** Task definitions built without `@task` — e.g. inline `Task(dataset=csv_dataset(...))`, where the dataset loads in the argument expression — cannot be intercepted. They still filter correctly at the boundary; they simply don't get the cost savings. Essentially all registry and real-world tasks are `@task` functions, so this is documented and accepted. A public query API (e.g. `eval_set_selection()`) may be offered later as an escape hatch for expensive side effects outside task construction; it is an optimization tool, never part of the contract. The systemic fix — lazy datasets — is a deep inspect_ai change and out of scope.

## Execution model

Full runner design belongs to a future document; the parts that constrain configuration:

- **One worker process per task.** The worker executes the definition under `INSPECT_EVAL_SET_SELECTION` with a single-task selection. The boundary call becomes a single-task filtered `eval_set()`.

- **Per-task log directories.** The definition's `log_dir` is the root; Steward assigns each task its own directory beneath it (derived from the task's key, exact layout TBD) via the `log_dir` override. Per-task directories eliminate log-dir contention and dirty-dir validation concerns entirely, and sample-level resume still works: respawning a failed task reuses its own directory, where `eval_set`'s log-matching idempotency picks up completed samples from the prior attempt. Steward owns the aggregate view across directories (see open questions). Note this layout is a Steward runtime choice — running `python evalset.py` by hand still uses the single `log_dir` as normal.

- **Retries** are split across two failure domains, and the division of responsibility is an open question (see below): sample/eval-level errors, which `eval_set`'s in-process retry machinery (`retry_immediate`, sample reuse) already handles well, vs process/infra-level failures (crash, OOM, hang), which only Steward can see and act on.

- **Supervision channel.** Workers run with inspect's control server enabled (`ctl_server`), giving Steward a live channel to query state and adjust runtime behavior of a running eval — which is Steward's whole purpose. Details in the runner design.

## Frontend adapters

Each frontend needs a thin adapter that turns a definition reference into a conforming program. The interception protocol is common; only loading differs.

### Raw `eval_set()` scripts

The user's file is the program. Steward executes it as `__main__` (via `runpy` or a subprocess `python evalset.py`) with the protocol environment set. Nothing about the file is Steward-specific — it runs identically by hand:

```python
# evalset.py — exactly what users write today
from inspect_ai import eval_set
from my_evals import my_task

set_model_info(...)                     # side effects: run in every process

tasks = [my_task(difficulty=d) for d in ["easy", "hard"]]

eval_set(
    tasks=tasks,
    model=["openai/gpt-5", "anthropic/claude-opus-5"],
    log_dir="logs/sweep",
)
```

Under a single-task selection, the non-matching `my_task` calls return placeholders (no dataset load), and the boundary runs exactly one task.

### Inspect Flow specs

Steward uses `inspect_flow` **as a library, never as a runner**: `load_spec()`/`expand_spec()` provide includes, implicit `_flow.py` inheritance, defaults merging, `NotGiven` semantics, and `@after_load` hooks; task instantiation goes through the registry (so Layer 2 pruning applies); the adapter maps `FlowOptions` to `eval_set()` kwargs and makes the boundary call itself. Loading a Python spec is `exec` with real side effects (sys.path mutation, dotenv, `_flow.py` files) — by design, since every worker re-executes it. Flow's store, bundling, and steps are out of scope for Steward's execution path.

Flow specs may contain live `Task`/`Model` objects (which Flow itself rejects in venv mode); the always-re-execute model supports them naturally.

### Hawk eval set configs

Steward reads Hawk configs with **Hawk's own parser**: the `hawk` package is assumed installed (a Steward optional extra, e.g. `inspect_steward[hawk]`), and configs are validated via `EvalSetConfig.model_validate()` so Steward never chases Hawk's schema. The adapter crosses the grid (tasks × solvers/agents × models, as Hawk's runner does today) and loads via the registry, so Layer 2 pruning applies.

Deployment-wise, Hawk keeps its platform — CLI, API server, Helm release, runner-pod venv construction, secrets, sandboxes — and embeds **Steward inside the release**, delegating at the point where `run_eval_set.py` currently makes its single `eval_set()` call. Hawk's existing runner is already a conforming program (config in, one `eval_set()` out), so the integration is minimal: the pod invokes Steward, and Steward drives Hawk's entrypoint under the protocol. Hawk continues to own environments (the per-job uv venv, package installation from `PackageConfig` pip specs); Steward owns execution within the environment Hawk built. Steward does **not** take on dependency/venv management — running a Hawk config outside a Hawk deployment requires the referenced packages to already be installed.

## Changes required in inspect_ai

1. Capture mode: `INSPECT_EVAL_SET_CAPTURE` honored by `eval_set()` — resolve, write manifest, exit the process.
2. Selection mode: `INSPECT_EVAL_SET_SELECTION` honored at the boundary (Layer 1), plus drift errors and the single-call constraint.
3. Automatic pruning in the resolver and the `@task` registry wrapper (Layer 2), including the placeholder task mechanism.
4. Overrides channel: `INSPECT_EVAL_SET_OVERRIDES` for the whitelisted supervision kwargs (including `log_dir`).
5. Public (or at least stable) exposure of `task_identifier` and the manifest models, which today live in `inspect_ai._eval.evalset`.

## Open questions

1. **Retry responsibility.** How to divide retries between `eval_set`'s in-process machinery and Steward's process-level supervision. Options: (a) Steward is the sole retry authority — workers run with `retry_attempts=0` and a respawn *is* the retry (per-task log dirs make this clean, since sample resume is per-directory); (b) workers keep modest in-process retries for cheap transient failures (model API errors, sample errors — where `retry_immediate` + sample reuse is battle-tested), while Steward supervises attempt budgets at the process level and handles the failures `eval_set` can't see (crash, OOM, hang). Current lean: (b) — don't reimplement working sample-level retry. Belongs to the runner design.

2. **Aggregate view across per-task log dirs.** Each task's directory gets its own `eval-set.json` and logs; Steward's manifest is the top-level index. How `inspect view`, bundling, and log listing work across the tree (and whether Steward writes a root-level manifest/redirect inspect tools understand).

3. **Overrides whitelist.** Exactly which `eval_set()` kwargs Steward may override in workers (`log_dir`, `display`, `log_level`, `retry_attempts`, `ctl_server`, `max_tasks`, ...) and what happens on conflict with definition-specified values.

4. **Display-key format details.** Exact rendering of the solver segment (resolved plan name for default solvers), and the disambiguation format for args/config sweeps.

5. **Hawk `extra="allow"` passthrough.** Hawk forwards unknown YAML keys verbatim to `eval_set()`. Direct (non-embedded) Hawk config support in Steward should honor the same passthrough or declare a whitelist.

6. **Selection schema evolution.** The reserved per-task `samples` field for within-task sharding (splitting one large task's samples across workers, via `eval_set`'s existing `sample_id` support).
