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

### Locating the definition

Every verb that takes a definition — `steward run`, `steward tasks` — accepts an explicit path. With no path, it discovers one **in the current directory** by convention:

```
run.py    evalset.py    flow.py    flow.yaml
```

The common case is then `steward run` in a project directory, with the filename as an escape hatch for anyone using a different convention.

Four rules keep this from becoming a source of surprise:

- **Current directory only.** No recursion, no walking upward. A definition is found because the user is standing in its project, not because Steward went looking.
- **Multiple matches are an error**, naming the candidates, rather than a precedence order that silently picks. A repository with both `flow.yaml` and `run.py` is genuinely ambiguous — which is authoritative is unknowable — and quietly running the wrong eval set is expensive and easy not to notice. The fix is one word on the command line. This matches how ambiguity is already handled a layer down: `detect_definition_type` refuses a `.py` file that imports both `inspect_flow` and `eval_set` rather than guessing.
- **The name is a discovery hint, not a type declaration.** Type detection stays structural and independent, so a file called `flow.py` that culminates in `eval_set()` is an `evalset` definition and is treated as one.
- **`_flow.py` is deliberately not a candidate.** It is Flow's implicit-inheritance fragment — a piece of shared configuration, not a program that culminates in an `eval_set()` call — so matching it would be a category error.

One hazard worth recording: Flow writes a *resolved* `flow.yaml` into its log directory as an output artifact, and that file is itself a valid spec. Discovery run from inside a log directory would therefore find it and re-run the eval set from its resolved config. Steward should recognize a log directory (`.eval-set-id` or `eval-set.json` present) and refuse rather than treat its contents as a definition.

### Protocol

The interception protocol is environment-based so that any conforming program works unmodified under any process manager (names provisional):

| Variable | Meaning |
|---|---|
| `INSPECT_EVAL_SET_CAPTURE` | Path to write the manifest. `eval_set()` resolves tasks, writes the manifest, and exits the process. |
| `INSPECT_EVAL_SET_SELECTION` | Path to a selection document (`{version, eval_set_id, tasks: [{identifier, resume?}]}` — `inspect_ai._eval.eval_set_selection`). `eval_set()` runs only matching tasks, through `eval()`, and performs no eval-set orchestration. |
| *(operational overrides)* | Carried in the selection document rather than a channel of their own: `log_dir` and `max_samples` let Steward control worker runtime behavior without editing definitions. Neither changes what an eval means, and neither participates in task identity. The one option Steward overrides semantically (`fail_on_error`) is applied by selection mode itself. |

The protocol lives in **inspect_ai** — `eval_set()` honors these variables natively. Flow and Hawk then conform with zero code changes, `python evalset.py` works directly under Steward, and any future frontend gets Steward support for free.

Steward exposes the enumerate side programmatically as `read_eval_set()`:

```python
def read_eval_set(definition: str, args: dict[str, Any] | None = None) -> Manifest:
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
    "type": "evalset",            // evalset | flow
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

A selection is a structured document, not a bare list of identifier strings — the per-task entry is an object so early filtering layers can carry partial facets that opaque identifiers can't provide. Version 1, as implemented, carries only what the authoritative filter and resume need:

```jsonc
{
  "version": 1,
  "eval_set_id": "swe-sweep-2026-08",
  "tasks": [
    {
      "identifier": "<full task_identifier>",
      "resume": "logs/…_mbpp_abc.eval"   // optional prior log to reuse samples from
      // added with Layer 2: "name", "args_hash", "model", "sequence"
      // reserved: "samples": [...] — future within-task sharding
    }
  ]
}
```

The Layer 2 facets are additive optional fields, so adding them needs no version bump: Steward writes the file and inspect_ai reads it, and an older inspect_ai ignores fields it doesn't know.

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

Full runner design is in [execution.md](execution.md); the parts that constrain configuration:

- **One worker process per task.** The worker executes the definition under `INSPECT_EVAL_SET_SELECTION` with a single-task selection. At the boundary, `eval_set()` resolves normally, filters to the selection, and runs the task through the ordinary `eval()` path — performing none of its own orchestration (no directory scan, no `.eval-set-id` / `eval-set.json` / `logs.json`, no log pruning).

- **One flat log directory.** Every worker writes into the definition's own `log_dir`, and each writes exactly one `.eval` file, which inspect writes atomically. Because worker mode removes the competing orchestrator, this is safe by construction, and `inspect view` and `samples_df` work against the directory live and unmodified. Steward is the single writer of the shared eval-set metadata. (An earlier draft specified per-task subdirectories; that avoided contention only by moving it, since each worker still ran a full orchestrator over its own directory — see execution.md.)

- **Recovery** runs in three tiers rather than as a single retry setting. Selection mode hard-codes `fail_on_error=False` (and task-retry-off), so a task runs its whole dataset and finishes `success` carrying whatever errored samples remain; `retry_on_error` stays the definition author's to set. Those samples become an explicit adjudication queue: Steward can requeue them in flight over the control channel when the cause looks transient, or re-run them after completion by respawning with `resume` (errored samples re-run automatically; `invalidate_samples` forces a re-run of completed-but-suspect ones). Whole-task retry is Steward's alone and narrows to process death and errors outside sample scope; workers run with in-process task retry disabled so budgets cannot multiply. The consequence for configuration: **a worker exiting 0 with a `success` log says nothing about whether the work is good** — the log is the only ground truth.

- **Supervision channel.** Workers run with inspect's control server enabled (`ctl_server`), giving Steward a live channel to query state and adjust runtime behavior of a running eval — which is Steward's whole purpose. Details in [execution.md](execution.md).

## Frontend adapters

Each frontend needs a thin adapter that turns a definition reference into a conforming program. The interception protocol is common; only loading differs.

### Raw `eval_set()` scripts

The user's file is the program. Steward executes it as `__main__` (via `runpy` or a subprocess `python evalset.py`) with the protocol environment set. Nothing about the file is Steward-specific — it runs identically by hand:

```python
# evalset.py — exactly what users write today
from inspect_ai import eval_set
from my_evals import my_task

set_model_info(...)  # side effects: run in every process

tasks = [my_task(difficulty=d) for d in ["easy", "hard"]]

eval_set(
    tasks=tasks,
    model=["openai/gpt-5", "anthropic/claude-opus-5"],
    log_dir="logs/sweep",
)
```

Under a single-task selection, the non-matching `my_task` calls return placeholders (no dataset load), and the boundary runs exactly one task.

### Inspect Flow specs

**`flow run` is itself a conforming program**: it culminates in the `eval_set()` call the spec describes, so Steward drives flow's own CLI under the protocol (`python -m inspect_flow._cli.main run <spec>`) rather than reaching into flow's internals. Flow keeps ownership of everything before the boundary — includes, implicit `_flow.py` inheritance, defaults merging, `NotGiven` semantics, `@after_load`/`@after_instantiate` hooks, and its `FlowOptions` → `eval_set()` mapping — and Steward owns execution from the boundary onward. Loading a Python spec is `exec` with real side effects (sys.path mutation, dotenv, `_flow.py` files) — by design, since every worker re-executes it.

Flow writes `flow.yaml` and a requirements snapshot into the log directory *before* the boundary, so those side effects cannot be intercepted by capture. Reads therefore pass a scratch `--log-dir`, leaving the definition's real log directory untouched; execution passes the real one, where those files are wanted. Flow's store, bundling, and steps are out of scope for Steward's execution path (workers run with `--store none`).

Flow conforms to both halves of the protocol as-is — verified end to end, including four concurrent flow workers writing into one flat log directory. Flow also passes `ctl_server` through to `eval_set()`, so flow-launched workers get the control endpoint Steward supervises them with, and it already defaults `retry_on_error` to 3. Flow specs can carry `scanner:`, which selection mode rejects; `options["scanners"]` in the manifest surfaces that at enumeration time. The one rough edge is that each worker repeats Flow's pre-boundary work — see open question 1 in [execution.md](execution.md).

Flow specs may contain live `Task`/`Model` objects (which Flow itself rejects in venv mode); the always-re-execute model supports them naturally.

### Hawk eval set configs

**Not currently supported** — deferred until the integration can go through Hawk's own runner. Steward recognizes a Hawk config structurally and reports that it is unsupported, rather than failing as an invalid flow spec.

A first implementation parsed configs with Hawk's `EvalSetConfig` and then reimplemented the lowering (the tasks × solvers/agents × models crossing from `run_eval_set.py`). That fork diverged from a real Hawk run in ways a manifest cannot show:

- **`EvalSetInfraConfig` is invisible to it.** Hawk merges a second, platform-supplied config into the `eval_set()` call — `log_dir`, the retry family, `tags`/`metadata` (unioned with the user's), `max_samples`/`max_tasks` defaults, `log_shared`, `acp_server`, and a runtime-computed `max_sandboxes`. Even `hawk local` synthesizes a default one. Task identity is unaffected (none of those fields feed `task_identifier`), but eval-set options are not what Hawk would use.
- **`runner.environment` was ignored**, though it is a user-config field Hawk applies to the environment before running — so dataset loading could differ or fail.
- Secrets resolution (files, AWS Secrets Manager, provider routing) and dependency installation were absent.

The right shape is the one used for Flow: **Hawk's runner is itself a conforming program** (config in, one `eval_set()` call out), so Steward should drive Hawk's entrypoint under the protocol rather than re-deriving it. Deployment-wise Hawk keeps its platform — CLI, API server, Helm release, runner-pod venv construction, secrets, sandboxes — and embeds Steward inside the release, delegating where `run_eval_set.py` makes its single `eval_set()` call; Hawk continues to own environments, Steward owns execution within the environment Hawk built. Steward does not take on dependency/venv management.

## Changes required in inspect_ai

1. Capture mode: `INSPECT_EVAL_SET_CAPTURE` honored by `eval_set()` — resolve, write manifest, exit the process. *Landed.*
2. Selection mode: `INSPECT_EVAL_SET_SELECTION` honored at the boundary (Layer 1), plus drift errors. *Landed.*
3. Automatic pruning in the resolver and the `@task` registry wrapper (Layer 2), including the placeholder task mechanism.
4. Operational overrides: carried by the selection document (`log_dir`, `max_samples`) rather than a separate channel. An environment variable could not serve — `INSPECT_LOG_DIR` and its siblings are *defaults*, and `eval_set()` declares `log_dir` with no default, so a definition's explicit argument always wins. The error-handling options this once had to carry (`fail_on_error`, `continue_on_fail`) are hard-coded by selection mode instead, and `retry_on_error` stays with the definition. *Landed.*
5. Public (or at least stable) exposure of `task_identifier` and the manifest models, which today live in `inspect_ai._eval.evalset`. *Partly landed:* `task_identifier` is public; the capture and selection models stay private as versioned wire formats.
6. The single-`eval_set()`-call constraint (a second call under capture or selection is an error) is not yet enforced.

## Open questions

1. **Retry responsibility.** *Resolved, and reframed:* the question was the wrong shape. Rather than dividing one retry mechanism, Steward runs `fail_on_error=False` so sample failures never fail a task, which turns recovery into three tiers — in-eval `retry_on_error`, in-flight requeue over the control channel, and post-completion adjudication (invalidate + resume). Whole-task retry is Steward's alone and shrinks to process death and errors outside sample scope; worker mode forces in-process task retry off so budgets cannot multiply. What remains open is the classification and escalation policy the tiers depend on. See [execution.md](execution.md).

2. **Aggregate view across per-task log dirs.** *Moot:* there are no per-task directories. One flat directory means `inspect view`, `samples_df`, bundling, and log listing work unmodified, and Steward is the single writer of `.eval-set-id`, `eval-set.json`, and `logs.json`.

3. **Overrides whitelist.** Exactly which `eval_set()` kwargs Steward may override in workers (`log_dir`, `display`, `log_level`, `retry_attempts`, `ctl_server`, `max_tasks`, ...) and what happens on conflict with definition-specified values. *Partly settled:* the whitelist stays purely operational — nothing on it may change what an eval means. The one semantic override (`fail_on_error=False`) is applied by selection mode itself, which keeps this channel from needing a second, riskier tier.

4. **Display-key format details.** *Resolved in implementation:* the solver segment always renders (the resolved plan name, or the literal `default` when unregistered); collisions disambiguate by differing args, then differing model args, then ordinal (`#n`) for config-only sweeps.

5. **Hawk `extra="allow"` passthrough.** *Moot for now:* Hawk support is deferred (see above). If Steward drives Hawk's entrypoint rather than lowering configs itself, Hawk's own passthrough handling applies unchanged.

6. **Selection schema evolution.** The reserved per-task `samples` field for within-task sharding (splitting one large task's samples across workers, via `eval_set`'s existing `sample_id` support).
