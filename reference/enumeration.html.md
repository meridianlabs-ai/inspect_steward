# Enumeration – Inspect Steward

## Reading

### read_eval_set

Read the static definition of an eval set.

Executes the definition in a subprocess with eval-set capture enabled: the definition runs normally (including any side effects) up to its [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call, which resolves all tasks, writes a manifest, and exits without running anything.

[Source](https://github.com/meridianlabs-ai/inspect_steward/blob/83559a02c673f2482e51d17b0e53d5aca0cf0a61/src/inspect_steward/_evalset/read.py#L49)

``` python
def read_eval_set(
    definition: str | Path,
    args: dict[str, Any] | None = None,
    *,
    type: DefinitionType | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Manifest
```

`definition` str \| Path  
Path to the definition file (an [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) script, an Inspect Flow spec, or a Hawk eval set config).

`args` dict\[str, Any\] \| None  
Arguments for the definition (flow spec function args only).

`type` DefinitionType \| None  
Explicit definition type (auto-detected by default).

`cwd` str \| Path \| None  
Working directory for executing the definition (defaults to the current working directory, matching how the definition would run by hand).

`env` dict\[str, str\] \| None  
Additional environment variables for the definition process.

`timeout` float \| None  
Timeout in seconds for executing the definition.

### ReadEvalSetError

An eval set definition could not be read.

[Source](https://github.com/meridianlabs-ai/inspect_steward/blob/83559a02c673f2482e51d17b0e53d5aca0cf0a61/src/inspect_steward/_evalset/read.py#L27)

``` python
class ReadEvalSetError(Exception)
```

## Manifests

### Manifest

Static enumeration of an eval set read from a definition.

[Source](https://github.com/meridianlabs-ai/inspect_steward/blob/83559a02c673f2482e51d17b0e53d5aca0cf0a61/src/inspect_steward/_evalset/manifest.py#L71)

``` python
class Manifest(BaseModel)
```

#### Attributes

`version` int  
Manifest schema version.

`identifier_version` int  
Version of the `task_identifier` computation that produced `tasks[].identifier`. A manifest outlives the inspect_ai it was read with, and an identifier computed under a different version cannot be matched against a log — so this records which computation the identifiers came from rather than leaving a silent mismatch to read as “nothing has run yet”.

`eval_set_id` str \| None  
Eval set id as passed to [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) (if any).

`source` [ManifestSource](../reference/enumeration.html.md#manifestsource)  
The definition this manifest was read from.

`options` dict\[str, Any\]  
Informational [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) options as the *definition* passed them (e.g. `log_dir`, `retry_attempts`, `limit`).

`log_dir` str \| None  
The run’s log directory as the launch resolved it, or `None` on a manifest committed before this field existed.

**Resolved once and recorded, for the reason `overrides` is.** The resolution has three rungs — the definition’s own `log_dir`, then a machine’s `log_root`, then the workspace’s `logs/` — and the middle one arrives in the environment. A scheduled tend inherits almost no environment (`_timer.env`, *AMBIENT*: “every variable Steward reads is resolved at launch and recorded in the committed manifest”), so re-deriving this per turn would have the 02:00 tend read `logs/` while the fleet wrote to the root: every task lands and then reads as never started, all night, with nothing saying why.

Recording it also makes a root change *visible*. A launch compares this against the directory it has just resolved, so moving the root strands the previous results exactly the way editing the definition’s `log_dir` does — and is gated by the same predicate. Recomputing both sides from the current root would have compared a value against itself.

`MANIFEST_VERSION` deliberately did not move, on the `capture_rss` reasoning rather than the sharper half of the `overrides` one: absence here means *resolve it the way it was resolved before this field*, which `resolve_log_dir` still does exactly, so an old manifest and a new reader agree.

`overrides` EvalSetOverrides \| None  
Inspect’s words as this run said them, or `None` where the run is the definition’s own.

**The durable copy, and the only one.** A run’s overrides are resolved once, at launch, from flags and the environment — and neither survives to the 02:00 tend that spawns the next worker. They cannot live in `.steward/` either, which this design tells people they may delete. So they are captured *with* the manifest, by the same subprocess that honoured them, and every later tend reads them back out of the committed file: the enumeration and the fleet cannot disagree, because the fleet’s copy is the one the enumeration was made under.

`MANIFEST_VERSION` deliberately did not move for this, and the `capture_rss` reasoning only half covers it. That argument is about a *new* reader meeting an old manifest, where an absent field means *not measured* and nothing is lost. The other direction is not so comfortable: an **older** reader meeting this field drops it silently, accepts the manifest as version 1, and tends the run on the definition’s own values — a different eval than the one that was captured, with nothing said. What makes that acceptable is only that no such reader exists in the wild; the package is unreleased. The moment one does, this field is the reason to bump.

`scan` ManifestScan \| None  
The run’s scanning material, or `None` where nothing scans — no scanner in the definition and nothing injected at launch.

Committed by the launch rather than the capture alone, because injection is the launch’s word: capture contributes `spec` and `scans` (`read_eval_set`), and the launch adds `injected` once the merge is settled. `MANIFEST_VERSION` deliberately did not move, on the `capture_rss` reasoning: absence means *this run does not scan*, which is exactly what an older manifest’s absence should mean to a newer tend — nothing to inject, fold, or finalize.

`tasks` list\[[ManifestTask](../reference/enumeration.html.md#manifesttask)\]  
Resolved tasks in the eval set.

### ManifestSource

The source a manifest was read from: an eval set definition and the arguments it was invoked with.

[Source](https://github.com/meridianlabs-ai/inspect_steward/blob/83559a02c673f2482e51d17b0e53d5aca0cf0a61/src/inspect_steward/_evalset/manifest.py#L27)

``` python
class ManifestSource(BaseModel)
```

#### Attributes

`type` DefinitionType  
Definition type: an `evalset` definition is a Python file culminating in a call to [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set); a `flow` definition is an Inspect Flow spec (Python or YAML); a `hawk` definition is a Hawk eval set config (YAML).

`path` str  
Definition file path (as provided by the caller).

`content_hash` str  
Hash of the definition file contents (`sha256:<hex>`), for staleness detection. Covers only the top-level file (not includes or imports).

`args` dict\[str, Any\]  
Arguments passed to the definition (flow spec function args; empty otherwise).

`capture_rss` int \| None  
Peak resident memory of the capture process tree, in bytes, or `None` where nothing measured it.

A fact about *reading* this definition rather than about the eval set, which is why it lives here beside the hash and the path. It is carried because it also bounds running one: capture constructs every task in the set, where a worker constructs only its own, so this is the most a worker’s startup can cost and the fleet’s is it times the width (`_evalset/cost.py`).

`MANIFEST_VERSION` deliberately did not move for this. The version gate refuses a manifest whose schema the reader would have to guess at; a field added with a default whose absence means *not measured* is not one, and bumping would have made every committed manifest unreadable to say so.

### ManifestTask

A resolved task in an eval set manifest.

[Source](https://github.com/meridianlabs-ai/inspect_steward/blob/83559a02c673f2482e51d17b0e53d5aca0cf0a61/src/inspect_steward/_evalset/manifest.py#L51)

``` python
class ManifestTask(EvalSetCaptureTask)
```

#### Attributes

`key` str  
Human-facing display key (`task[solver]@model`, disambiguated when tasks collide). Unique within a manifest, but not stable across definition edits — use `identifier` for stable matching.
