# Configuration

**Status: draft for discussion**

How users define what an evaluation run contains, and how Steward turns that definition into work it can manage.

## 1. Requirements

Steward is an autonomous evaluation runner: it launches, monitors, scales, diagnoses, and remedies on behalf of a human. That role imposes three requirements on configuration:

1. **Static enumeration.** Steward must be able to produce the full list of tasks (resolved task × model × solver combinations) *before* running anything, so it can schedule them, run them in their own processes, track completion, and estimate progress.

2. **Meet users where they are.** There are three ways people already define eval sets, and Steward must support all of them without forcing migration:
   - A plain Python file that ends in a call to `eval_set()` — the code users naturally write today.
   - An [Inspect Flow](https://github.com/meridianlabs-ai/inspect_flow) spec (`FlowSpec`, as Python or YAML).
   - A [Hawk](https://github.com/METR/hawk) eval set config (`EvalSetConfig` YAML).

   Steward introduces **no fourth format**. The raw `eval_set()` file is the native mode.

3. **Side effects run everywhere.** Definition files do real work when executed: `set_model_info()`, dynamically constructed `Model` objects, environment mutation, dataset preparation. These effects cannot be captured in a static artifact, so *every* process that runs evaluation work must execute the definition itself — enumeration produces an index into the definition, never a replacement for it.

A fourth, structural decision frames everything below: **Steward owns all orchestration.** It does run Flow's and Hawk's own entrypoints — that is what keeps their formats theirs — but it intercepts at the `eval_set()` boundary, so neither ever orchestrates. Flow and Hawk contribute *definition formats* and the code that lowers them; scheduling, retries, scaling, and supervision belong to Steward.

We maintain inspect_ai, so the mechanisms below that belong in inspect_ai are designed as inspect_ai features from the start — no interim patching or workarounds.

## 2. The definition contract

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

### 2.1 Locating the definition

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

### 2.2 Protocol

The interception protocol is environment-based so that any conforming program works unmodified under any process manager (names provisional):

| Variable | Meaning |
|---|---|
| `INSPECT_EVAL_SET_CAPTURE` | Path to write the manifest. `eval_set()` resolves tasks, writes the manifest, and exits the process. |
| `INSPECT_EVAL_SET_SELECTION` | Path to a selection document (`{version, eval_set_id, tasks: [{identifier, resume?}]}` — `inspect_ai._eval.eval_set_selection`). `eval_set()` runs only matching tasks, through `eval()`, and performs no eval-set orchestration. |
| *(operational overrides)* | Carried in the selection document rather than a channel of their own: `log_dir` and `max_samples` let Steward control worker runtime behavior without editing definitions. Neither changes what an eval means, and neither participates in task identity. A dataset `limit` override belongs here on the same grounds and has not landed ([execution.md](execution.md), item 8). The one option Steward overrides semantically (`fail_on_error`) is applied by selection mode itself. |

The protocol lives in **inspect_ai** — `eval_set()` honors these variables natively. Flow and Hawk then conform with zero code changes, `python evalset.py` works directly under Steward, and any future frontend gets Steward support for free.

Steward exposes the enumerate side programmatically as `read_eval_set()`:

```python
def read_eval_set(definition: str, args: dict[str, Any] | None = None) -> Manifest:
    """Execute a definition in a subprocess with capture enabled and return its manifest."""
```

> **Naming note**: inspect_ai has an internal `read_eval_set_info(log_dir)` that reads the post-hoc `eval-set.json` manifest from a log directory — a different artifact (what ran) from ours (what is defined to run). Since the capture API lands in inspect_ai we should either unify the two or choose a name like `resolve_eval_set()` to avoid confusion.

## 3. Tasks and identity

The unit of work is a **task**: a resolved task × model × solver combination — inspect_ai's `ResolvedTask`, corresponding 1:1 with an eval log. This follows existing precedent: inspect_ai's `eval-set.json` calls these entries `tasks` (`EvalSetTask`, each carrying its model) and Flow's crossed unit is `FlowTask`. Where prose needs to distinguish the definition (`@task my_task`) from the combination, we say "task definition" vs "resolved task".

Two identity layers:

- **`task_identifier`** (authoritative). inspect_ai's stable identity string — `task_file@task_name#args_hash/model/config_hash` — already used by `eval_set` to match logs to tasks for retry/resume, and by Flow for log reuse. It hashes task args, resolved plan/solver, generate config, limits, and version, so it distinguishes tasks that differ *only* in configuration (e.g. a temperature sweep over the same task definition and model). Steward uses it for exact matching and log correlation.

- **Display key** (human-facing). `task[solver]@model` — e.g. `mbpp[react]@openai/gpt-5` — used in the CLI, TUI, and logs. The solver segment is always present, showing the resolved primary solver/agent name (the task's default plan when no override is crossed in). Remaining collisions (args/config sweeps) are disambiguated with a short args/config summary or a sequence index (e.g. `mbpp[react]@openai/gpt-5 (temperature=0.7)`). Steward's CLI accepts patterns against display keys and reports the candidate list on ambiguity.

Full identifiers are only computable *after* task construction (they hash the resolved plan and config). This asymmetry — cheap partial facets early, exact identity late — drives the filtering design below.

## 4. The manifest

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

Because enumeration fully resolves tasks, the manifest carries per-task sample counts and epochs — the raw material for Steward's scheduling and progress estimation.

**Determinism requirement.** The contract assumes the definition produces the same task list on every execution: enumerate on Monday, execute task 7 on Tuesday, and the selection must still match. `task_identifier` is deterministic given the same tasks/args/models, so the requirement reduces to "the definition is deterministic with respect to its task list" (no time- or randomness-dependent task construction). Drift is detected, never papered over: a selected task that resolution fails to produce is a hard error naming the missing task, and the manifest's `content_hash` lets Steward warn when the definition file changed after enumeration. What that hash is and is not for is the subject of the next section.

## 5. Reproducibility is the author's concern

`task_identifier` hashes a task's **interface** — name, args, model, resolved plan, generate config, limits — and not its **implementation**. Change a scorer's logic, a prompt body, or the contents behind a dataset location, and the identifier is byte-identical. Inspect's answer is `task.version`, which participates in the identifier and is the author's to bump.

That is the whole of the mechanism, and deliberately so. Steward does not verify it.

The temptation runs the other way, because the ingredients for a checker are lying around: every log already records `revision` (git origin, commit, dirty), `packages` (inspect_ai's version, plus the task's distribution version when it came from one), and the dataset's name, location, and sample count — and a task installed from a git URL gets an exact commit from PEP 610 `direct_url.json` with no working tree involved at all. Steward reads log headers on every tend anyway, so comparing those across a directory would cost nothing.

It is still the wrong thing to build. Reproducibility discipline is a property of the eval, and the person who wrote the definition is the person who knows whether an upgraded package matters — the same reasoning that leaves `retry_on_error` with the author rather than the runner. A check Steward cannot ground in intent becomes a warning that fires during an ordinary week of `pip install -U`, and a warning that fires when nothing is wrong is one people learn to route around. The provenance is recorded in every log; querying it is a user's prerogative, not Steward's obligation.

**So `content_hash` stays narrow on purpose.** It covers the top-level definition file and nothing else, and its job is an affordance rather than a guarantee: *notice that the file in front of you changed, and offer to re-read it* ([workflow.md](workflow.md), *One trigger, and one gate on it*). Widening it was considered and dropped — a Python import graph's transitive closure cannot be resolved statically, and resolving it by observation would mean a capture-schema addition and a version bump in service of a check nobody asked for. Where the hash misses an edit — a changed module, an included fragment — the consequence is only that Steward does not volunteer the prompt; the next `launch` re-captures regardless, and any identifier that moved shows up in the delta. **The delta report is the safety net; the hash is only the nudge.**

One caveat worth recording so nobody reaches for it: `revision.dirty` is computed from `git status --porcelain` over the whole tree, and a Steward workspace writes `journal.jsonl`, `status.md`, and `anomalies.md` into that tree as it runs. In a workspace that `init` made its own repository, every log is therefore marked dirty by Steward's own operation. The flag means "a run touched this tree", not "the source was edited", and it should not be built on.

## 6. Selection and filtering

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
      // the reserved "samples" field is dropped — see open question 6
    }
  ]
}
```

The Layer 2 facets are additive optional fields, but that does not make them free: the selection models are `extra="forbid"`, so an older inspect_ai *refuses* a field it doesn't know rather than ignoring it. Each added field therefore takes a version bump and a `_FIELD_MIN_VERSION` entry — deliberately, since silently dropping an unrecognized key is how a misspelled `resume` becomes no resume at all. See [execution.md](execution.md), *Changes required in inspect_ai*.

Filtering happens at two internal layers. **No user-facing filtering API is required** — users write exactly the code they write today.

### 6.1 Layer 1: authoritative boundary filter

At the `eval_set()` boundary, after resolution: exact matching of resolved tasks against selection `identifier`s. Only this layer decides what runs. It also enforces drift detection (selected-but-unresolved task → hard error) and requires every selected task to match exactly one resolved task.

### 6.2 Layer 2: automatic early pruning

The expensive part of running a filtered worker is constructing task definitions the worker will never run — datasets load at `Task` construction. Pruning happens automatically at two points, both inside inspect_ai, both invisible to users:

1. **Resolver-level.** For task specs that are not yet constructed — registry names, `file.py@task` strings, `@task` callables — the resolver knows the task name before construction and skips creating (or even importing) tasks that no selected task references.

2. **Construction-level.** The `@task` registry wrapper intercepts every task creation call and sees exactly the identity facets that matter: the registry name and the actual passed args, *before* the function body runs. When a selection is active and no selected task matches `(task_name, args_hash)` — args hashed identically to `task_identifier`'s scheme; model/solver/config facets deliberately ignored at this layer — the wrapper returns a lightweight **placeholder task** without invoking the function. No dataset loads. The placeholder carries the name, args, and a reference to the factory, and is dropped at the boundary before identifier computation.

Layer 2 covers all three frontends with no cooperation: users' `@task` calls in `evalset.py`, Flow's `instantiate_tasks` (which goes through `registry_create`), and Hawk's task loading (likewise) all pass through the same interception point.

**Safety property.** Early pruning is purely an optimization and can never change results. Pruning fires only on a *definite* `(name, args_hash)` mismatch, so a placeholder can never correspond to a selected task; and since placeholders retain their factory, a pruning bug materializes the task late rather than failing the run. Under-pruning costs time only. Correctness rests entirely on Layer 1.

**Edge case.** Task definitions built without `@task` — e.g. inline `Task(dataset=csv_dataset(...))`, where the dataset loads in the argument expression — cannot be intercepted. They still filter correctly at the boundary; they simply don't get the cost savings. Essentially all registry and real-world tasks are `@task` functions, so this is documented and accepted. A public query API (e.g. `eval_set_selection()`) may be offered later as an escape hatch for expensive side effects outside task construction; it is an optimization tool, never part of the contract. The systemic fix — lazy datasets — is a deep inspect_ai change and out of scope.

## 7. Execution model

Full runner design is in [execution.md](execution.md); the parts that constrain configuration:

- **One worker process per task.** The worker executes the definition under `INSPECT_EVAL_SET_SELECTION` with a single-task selection. At the boundary, `eval_set()` resolves normally, filters to the selection, and runs the task through the ordinary `eval()` path — performing none of its own orchestration (no directory scan, no `.eval-set-id` / `eval-set.json` / `logs.json`, no log pruning).

- **One flat log directory.** Every worker writes into the definition's own `log_dir`, and each writes exactly one `.eval` file, which inspect writes atomically. Because worker mode removes the competing orchestrator, this is safe by construction, and `inspect view` and `samples_df` work against the directory live and unmodified. Steward is the single writer of the shared eval-set metadata. (An earlier draft specified per-task subdirectories; that avoided contention only by moving it, since each worker still ran a full orchestrator over its own directory — see execution.md.)

- **Recovery** runs in two tiers rather than as a single retry setting. Selection mode hard-codes `fail_on_error=False` (and task-retry-off), so a task runs its whole dataset and finishes `success` carrying whatever errored samples remain; `retry_on_error` stays the definition author's to set. Those samples become an explicit adjudication queue, and everything past tier 1 is adjudicated rather than automatic: a ruling authorizes either an in-flight requeue over the control channel or a post-completion re-run by respawning with `resume` (errored samples re-run automatically on resume; `invalidate_samples` forces a re-run of completed-but-suspect ones), the choice being only whether the task is still running. Whole-task retry is Steward's alone and narrows to process death and errors outside sample scope; workers run with in-process task retry disabled so budgets cannot multiply. The consequence for configuration: **a worker exiting 0 with a `success` log says nothing about whether the work is good** — the log is the only ground truth.

- **Supervision channel.** Workers run with inspect's control server enabled (`ctl_server`), giving Steward a live channel to query state and adjust runtime behavior of a running eval — which is Steward's whole purpose. Details in [execution.md](execution.md).

## 8. Frontend adapters

Each frontend needs a thin adapter that turns a definition reference into a conforming program. The interception protocol is common; only loading differs.

### 8.1 Raw `eval_set()` scripts

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

### 8.2 Inspect Flow specs

**`flow run` is itself a conforming program**: it culminates in the `eval_set()` call the spec describes, so Steward drives flow's own CLI under the protocol (`python -m inspect_flow._cli.main run <spec>`) rather than reaching into flow's internals. Flow keeps ownership of everything before the boundary — includes, implicit `_flow.py` inheritance, defaults merging, `NotGiven` semantics, `@after_load`/`@after_instantiate` hooks, and its `FlowOptions` → `eval_set()` mapping — and Steward owns execution from the boundary onward. Loading a Python spec is `exec` with real side effects (sys.path mutation, dotenv, `_flow.py` files) — by design, since every worker re-executes it.

Flow writes `flow.yaml` and a requirements snapshot into the log directory *before* the boundary, so those side effects cannot be intercepted by capture. Reads therefore pass a scratch `--log-dir`, leaving the definition's real log directory untouched; execution passes the real one, where those files are wanted. Flow's bundling and steps are out of scope for Steward's execution path.

**Flow's store is not out of scope, though workers still run with `--store none`** — for a different reason than the original one. The store indexes logs by `task_identifier` so an identical task never runs twice, and nothing about it is Flow-specific: the key is inspect_ai's and `store_factory` takes a bare path. So rather than being disabled, both halves move to Steward, which can then offer the same caching to `evalset` and `hawk` definitions — neither of which has any Flow code in the worker to do it for them. See [execution.md](execution.md), *Flow's store, and who is allowed to read it*.

Flow conforms to both halves of the protocol as-is — verified end to end, including four concurrent flow workers writing into one flat log directory. Flow also passes `ctl_server` through to `eval_set()`, so flow-launched workers get the control endpoint Steward supervises them with, and it already defaults `retry_on_error` to 3. Flow specs can carry `scanner:`, which selection mode rejects; `options["scanners"]` in the manifest surfaces that at enumeration time. The one rough edge is that each worker repeats Flow's pre-boundary work — see open question 1 in [execution.md](execution.md).

Flow specs may contain live `Task`/`Model` objects (which Flow itself rejects in venv mode); the always-re-execute model supports them naturally.

### 8.3 Hawk eval set configs

**`hawk local eval-set` is itself a conforming program**: it culminates in the `eval_set()` call the config describes, so Steward drives Hawk's own CLI under the protocol (`python -m hawk local eval-set <config> --direct`) rather than reaching into Hawk's internals. Hawk keeps ownership of everything before the boundary — the tasks × solvers/agents × models crossing, secrets resolution, `runner.environment`, provider environment for middleman routing, and rejecting configs it does not support — and Steward owns execution from the boundary onward. `--direct` is not optional: without it Hawk builds a fresh venv per worker.

`--direct` does not mean *skip installing*, though — Hawk still runs `uv pip install` into the current environment on every invocation (`run_in_venv.install_into_current`). It is a no-op in a consistent environment, since Hawk pins the versions already installed, but two consequences follow that Flow has no equivalent of: a config declaring `packages:` installs them into the caller's environment, and N workers starting together run N concurrent installs into it. Two concurrent workers are verified; higher fan-out is not, and this is the first thing to suspect if it misbehaves. Driving `python -m hawk.runner.run_eval_set` instead would avoid the install entirely — that entry point skips Hawk's entrypoint module — at the cost of the pre-boundary work above.

An earlier implementation instead parsed configs with Hawk's `EvalSetConfig` and reimplemented the lowering. That fork diverged from a real Hawk run in ways a manifest cannot show — `EvalSetInfraConfig` was invisible to it, `runner.environment` was ignored, and secrets resolution was absent — and all three dissolve here, because all three happen in Hawk's runner *before* the boundary.

Hawk has no log-directory option to override: a local run synthesizes an infra config whose `log_dir` is a fresh `logs/<random job id>/` relative to the working directory. Reads need no scratch directory anyway, because capture exits before that path is used (nothing creates it), and workers override both `log_dir` and `eval_set_id` through the selection document. Verified end to end, including two concurrent Hawk workers writing into one flat log directory, both logs stamped with the eval-set id Steward assigned rather than Hawk's synthetic one.

Detection validates a YAML definition against both `FlowSpec` and `EvalSetConfig` and requires exactly one match. Any definition that names tasks discriminates itself: a flow spec's `{name, model}` task entries cannot satisfy Hawk's `PackageConfig` (which requires `package` and `items`), and Hawk's entries match no member of flow's `str | FlowTask | Task` union. A definition that names *no* tasks satisfies both — `tasks: []` is a valid empty sequence for flow and a present-but-empty required field for Hawk — so that one case is reported as ambiguous and asks for an explicit `--type` rather than being resolved by declaring one format the winner.

Detection is relative to what is installed: each format is validated only when its package is present, so "exactly one match" means one among those that could be checked. With only one of the two installed, an otherwise-ambiguous document resolves to that one. A document that is genuinely the *other* format still fails, and says which package is missing — the ambiguity is confined to definitions that declare no tasks, which no frontend can run anyway.

Hawk requires Python 3.13; inspect_steward does not. The extra is marker-gated, so the floor binds only those who ask for Hawk, and on 3.12 the extra resolves to nothing.

That install also needs a `uv` binary, which Hawk shells out to but does not declare, so the `[hawk]` extra carries it. Declaring it is necessary but not sufficient: pip installs it beside the interpreter, and Hawk resolves a bare `uv` through `PATH`, which contains that directory only when the venv happens to be activated — so an unactivated `.venv/bin/steward` would still fail. Steward prepends the interpreter's directory to the Hawk child's `PATH`, which is Steward's to do because Steward chose the interpreter. Both are ours to carry until Hawk declares the dependency.

Deployment-wise Hawk keeps its platform — CLI, API server, Helm release, runner-pod venv construction, secrets, sandboxes — and would embed Steward inside the release, delegating where `run_eval_set.py` makes its single `eval_set()` call; Hawk continues to own environments, Steward owns execution within the environment Hawk built. Steward does not take on dependency/venv management. That half is unbuilt — see [hawk.md](hawk.md) for the infra config's per-process budgets, the blocking `launch`, and the relay surface an external agent drives it through.

## 9. Changes required in inspect_ai

1. Capture mode: `INSPECT_EVAL_SET_CAPTURE` honored by `eval_set()` — resolve, write manifest, exit the process. *Landed.*
2. Selection mode: `INSPECT_EVAL_SET_SELECTION` honored at the boundary (Layer 1), plus drift errors. *Landed.*
3. Automatic pruning in the resolver and the `@task` registry wrapper (Layer 2), including the placeholder task mechanism.
4. Operational overrides: carried by the selection document (`log_dir`, `max_samples`) rather than a separate channel. An environment variable could not serve — `INSPECT_LOG_DIR` and its siblings are *defaults*, and `eval_set()` declares `log_dir` with no default, so a definition's explicit argument always wins. The error-handling options this once had to carry (`fail_on_error`, `continue_on_fail`) are hard-coded by selection mode instead, and `retry_on_error` stays with the definition. *Landed* — except for the dataset `limit` a smoke run needs, which is a third override on the same grounds and is tracked as [execution.md](execution.md) item 8. `max_samples` is concurrency, not a slice, and does not substitute for it.
5. Public (or at least stable) exposure of `task_identifier` and the manifest models, which today live in `inspect_ai._eval.evalset`. *Partly landed:* `task_identifier` is public; the capture and selection models stay private as versioned wire formats.
6. The single-`eval_set()`-call constraint (a second call under capture or selection is an error) is not yet enforced.

## 10. Open questions

1. **Retry responsibility.** *Resolved, and reframed:* the question was the wrong shape. Rather than dividing one retry mechanism, Steward runs `fail_on_error=False` so sample failures never fail a task, which turns recovery into two tiers by *authority*: in-eval `retry_on_error` runs automatically, and past it both mechanisms — in-flight requeue and post-completion invalidate-plus-resume — need a ruling. Only `retry_on_error` runs unsupervised; past it, further attempts are a decision, which is why Steward carries no requeue budget. Whole-task retry is Steward's alone and shrinks to process death and errors outside sample scope; worker mode forces in-process task retry off so budgets cannot multiply. See [execution.md](execution.md), *The authority line is not where the tiers divide*.

2. **Aggregate view across per-task log dirs.** *Moot:* there are no per-task directories. One flat directory means `inspect view`, `samples_df`, bundling, and log listing work unmodified, and Steward is the single writer of `.eval-set-id`, `eval-set.json`, and `logs.json`.

3. **Overrides whitelist.** Exactly which `eval_set()` kwargs Steward may override in workers (`log_dir`, `display`, `log_level`, `retry_attempts`, `ctl_server`, `max_tasks`, ...) and what happens on conflict with definition-specified values. *Partly settled:* the whitelist stays purely operational — nothing on it may change what an eval means. The one semantic override (`fail_on_error=False`) is applied by selection mode itself, which keeps this channel from needing a second, riskier tier.

4. **Display-key format details.** *Resolved in implementation:* the solver segment always renders (the resolved plan name, or the literal `default` when unregistered); collisions disambiguate by differing args, then differing model args, then ordinal (`#n`) for config-only sweeps.

5. **Hawk `extra="allow"` passthrough.** *Moot:* Steward drives Hawk's entrypoint rather than lowering configs itself, so Hawk's own passthrough handling applies unchanged — including its guarantee that a forwarded extra cannot shadow an infra-config kwarg. Hawk's remaining open questions are in [hawk.md](hawk.md).

6. **Selection schema evolution.** *Resolved: won't do.* The reserved per-task `samples` field was for within-task sharding, and [scheduling.md](scheduling.md) rules that out — a task always runs all of its samples in one process, controlling memory through `max_samples` from the inside. A single very large task is therefore bounded by one core, which is accepted rather than solved: the alternative is a second granularity mechanism with multiple logs per task, a completeness predicate that reassembles shards, and adjudication that has to know which shard a sample came from. The field can be dropped rather than designed. With batching also ruled out, the selection document's `tasks` list is permanently length 1 — unused generality upstream, harmless, and not worth a schema change to remove.
