# Execution

**Status: draft for discussion**

How Steward runs the tasks that [configuration.md](configuration.md) enumerates. That document ends at the manifest — the static list of resolved tasks in an eval set. This one starts there: how those tasks become processes, where their logs go, who retries what, and how Steward keeps track of work it did not stay attached to.

## 1. Requirements

1. **Steward owns orchestration.** Scheduling, retries, scaling, and supervision are Steward's, not `eval_set()`'s. A definition's `eval_set()` call is a *boundary*, not a runner.

2. **Standard Inspect tooling must work, live.** `inspect view`, `samples_df`, `evals_df`, and `inspect log` must work against a running eval set with no Steward-specific adaptation and no post-processing step. A user watching a run in the viewer should see what they would see if they had run `eval_set()` by hand.

3. **Workers execute the definition.** Every process that runs evaluation work executes the whole definition program, so `set_model_info()`, dynamically constructed `Model` objects, and environment setup are in place — see requirement 3 in [configuration.md](configuration.md).

4. **Survive Steward restarting.** A run outlives the process that started it. Steward must be able to exit, be killed, or be upgraded, and on return reconstruct what is in flight without killing or double-running anything.

## 2. The problem with running `eval_set()` per worker

The obvious implementation — spawn N workers, each running the definition with a single-task selection, all pointed at one log directory — does not work, and the reason shapes everything below.

`eval_set()` is *itself* an orchestrator. On every pass it scans the whole log directory, decides which logs to reuse, writes `.eval-set-id` / `eval-set.json` / `logs.json`, and prunes "older" logs. Running N of them over one directory means N orchestrators over shared state, and there is no locking, no atomic manifest write, and no claim protocol anywhere in that machinery. Concretely:

- Worker B lists the directory, sees worker A's in-flight log with `status == "started"`, classifies it as incomplete, and re-runs A's task.
- `cleanup_older_eval_logs` groups logs by `task_id` and deletes the losers by mtime — so one worker deletes another's good log.
- `eval-set.json` and `logs.json` are truncate-in-place writes that can tear, and the viewer's reader for `eval-set.json` validates it with no error handling.
- Each worker does roughly 2N log-header reads per pass, so directory scanning costs O(workers × logs).

Per-task log directories were the first answer (and are what configuration.md originally specified). They avoid the contention, but only by moving it: each worker still runs a full orchestrator, just over its own directory, and Steward inherits the job of stitching a tree of `eval-set.json` files into one view. Inspect's discovery already recurses by default, so nesting is *mostly* invisible in the viewer's default task listing — but drilling into a log loses its eval-set context, because `eval-set.json` is only read from the log's own directory.

**The resolution is to remove the competing orchestrator, not to give each one its own sandbox.** Steward *is* the eval-set runner. A worker should run a single `eval()`, which is the part of Inspect that actually runs a task, and none of the part that decides *which* tasks to run.

## 3. Worker model

A worker is one process running one task:

```
steward
   │  reads the definition once (capture) ──► Manifest
   │
   ├─► worker  ── definition ──► eval_set() ──► eval(task₁)  ──► task₁.eval
   ├─► worker  ── definition ──► eval_set() ──► eval(task₂)  ──► task₂.eval
   └─► worker  ── definition ──► eval_set() ──► eval(task₃)  ──► task₃.eval
                                    │
                            selection boundary:
                            resolve normally, run only
                            the selected task, skip all
                            eval-set bookkeeping
```

The definition still calls `eval_set()` — that is the contract, and it is what preserves the definition's side effects in every worker. What changes is what `eval_set()` *does* when a selection is active: it resolves tasks exactly as it normally would, filters to the selected identifiers, and hands them to the ordinary `eval()` path. It writes no eval-set metadata, scans no directories, and prunes nothing.

Each worker therefore writes exactly one `.eval` file, and Inspect writes those atomically (temp file + `os.replace`). That is the whole safety argument for a shared directory: the only thing a worker touches is a file no other worker knows about.

**What a process costs, measured** — spawn to landed log, `mockllm`, warm environment, per definition type:

| | one worker | four concurrent | over baseline |
|---|---|---|---|
| `evalset` script | 3.0s | 3.3s | — |
| Flow spec | 4.2s | 4.7s | **+1.2s** |
| Hawk config | 3.5s | 4.0s | **+0.5s** |

Two things to read out of it. **A frontend costs about a second, not the minutes this document elsewhere estimates** — and Hawk, the one described as the expensive case, is *cheaper* than Flow once its environment is warm, because Flow pays a `uv pip freeze` and `uv pip compile` on every worker while Hawk's install is a satisfied no-op. **And four concurrent workers cost roughly what one does** — half a second more, not four times — which is the empirical form of the argument in [scheduling.md](scheduling.md) for a flat worker ceiling.

The caveats are where the minutes actually live, and none of them is exercised above: a **cold** environment pays Hawk's real install once (4.4s vs 2.7s here, and unbounded for a config declaring `packages:`); a config with `secrets:` adds a Secrets Manager round trip **per worker**; and both frontends scan the log directory at startup, which over S3 is latency-bound rather than free. The numbers are a floor, not a promise.

## 4. The selection protocol

Selection is the execution counterpart to capture. Both are environment-variable interceptions at the `eval_set()` boundary, and they are mutually exclusive.

| Variable | Meaning |
|---|---|
| `INSPECT_EVAL_SET_CAPTURE` | Path to write a manifest. `eval_set()` resolves tasks, writes the manifest, and exits the process without running anything. |
| `INSPECT_EVAL_SET_SELECTION` | Path to a selection document. `eval_set()` resolves tasks, runs only the selected ones through `eval()`, and skips all eval-set orchestration. |

The selection document (`inspect_ai._eval.eval_set_selection`, schema version 3):

```jsonc
{
  "version": 3,
  "eval_set_id": "swe-sweep-2026-08",   // Steward-assigned; stamped into every log
  "tasks": [
    {
      "identifier": "<task_identifier from the manifest>",
      "resume": "logs/2026-08-19T…_mbpp_abc.eval"   // optional prior log to resume
    }
  ],
  "overrides": {                        // all optional; omitted keeps the definition's
    "log_dir": "s3://…/logs",
    "max_samples": 12,
    "limit": 5,
    "max_sandboxes": 8
  }
}
```

**Three fields are the protocol; the rest are knobs, and they live in a container.** `version`, `eval_set_id`, and `tasks` are what a selection *is*. Everything else adjusts how one worker is operated, and at version 3 there are four of them — enough that leaving them beside the protocol fields reads as one flat bag of unrelated things. `overrides` is a `extra="forbid"` sub-model like its parent, so a misspelled key is still refused rather than dropped.

The container does not save a schema version per field, and the design should not pretend otherwise: `extra="forbid"` already refuses an unknown key, and the version bump exists to turn that refusal into *"upgrade inspect-ai"* rather than *"unknown field"* — which is worth having either way. What it fixes is a category error, not a cost.

**The rule for what may go in:** an override may change **how a worker is operated** — where its output goes, how fast it runs, how much of its dataset it runs, what surfaces it exposes — but never *what is evaluated*. The operative test is mechanical: nothing in the container participates in `task_identifier()`, so overriding it cannot desynchronize a worker from the capture manifest. That is why `limit` qualifies (the identifier hashes a task's execution limits, not its dataset slice) and `time_limit` does not.

**The `log_dir` override reaches the boundary, and not one step earlier.** Anything a frontend writes on its way to `eval_set()` lands wherever the *definition* said — verified: a flow worker given only the override drops `flow.yaml` and `flow-requirements.txt` into the definition's log directory before the selection is ever read. So a worker needs both channels, exactly as a read does: the frontend's own `--log-dir` for the pre-boundary half and the selection override for the eval itself. This is the same pre-boundary seam as open question 1 and Hawk's installation problem, in its smallest form.

**And the two channels deliberately carry different directories.** An earlier draft had the frontend channel carry the run's log directory too, on the grounds that a frontend's artifacts are wanted there. They are not: the work behind them is once-per-*run* and every worker repeats it, so pointing N workers at one directory means N concurrent writes to the same two paths and N scans of a directory that grows all run. Each worker therefore gets a scratch directory of its own (`.steward/workers/<stem>/`), and the run's log directory is reached only through the selection. Reads already did this; workers now do the same thing for the same reason.

`tasks` is a list rather than a single entry so a worker could host several when that is cheaper than several processes. Steward never does: [scheduling.md](scheduling.md) settles on exactly one task per process, so the list is permanently length 1 and the generality goes unused.

An identifier that matches no resolved task is a hard error naming the likely cause — the definition changed since it was enumerated. An identifier that matches *more* than one resolved task is also an error: outside selection mode, `validate_eval_set_prerequisites` enforces identifier uniqueness across the eval set, and worker mode skips that check, so it makes the same guarantee locally for the tasks it is asked to run.

### 4.1 What worker mode deliberately skips

Everything in `eval_set()` below the selection branch is orchestration, and all of it is Steward's:

| Skipped | Who does it instead |
|---|---|
| `eval_set_id_for_log_dir` (reads/writes `.eval-set-id`) | Steward, once, at run start |
| `write_eval_set_info` (`eval-set.json`) | Steward, rewritten as logs land |
| `write_log_dir_manifest` (`logs.json`) | Steward, periodically and at the end |
| `list_all_eval_logs` + pending/failed partitioning | Steward's scheduler |
| `validate_eval_set_prerequisites` | Steward, at enumeration time |
| `cleanup_older_eval_logs` | Steward, at the end of the run |
| `embed_log_dir` / `bundle_log_dir` | Steward, at the end of the run |
| `scan_context` / `scan_finalize` | Steward, over the log directory (see Scanning) |
| `emit_eval_set_start` / `emit_eval_set_end` | **Nobody, today** — see open question 11 |

Two things are *not* skipped. The worker still creates `log_dir` (`mkdir(exist_ok=True)` is idempotent and concurrency-safe), and it still runs the full `eval()` path, so every eval-set-level kwarg the definition set — epochs, limits, solver, generate config, tags, metadata, `retry_on_error` — applies exactly as it would have.

**Three of those kwargs are exceptions, forced by worker mode rather than honoured.** `fail_on_error` is `False` and task-level retry is off, because both are completion decisions belonging to the runner; `acp_server` is on, because a detached worker has no other way to reach a human (*The parked worker*). All three are properties of what worker mode *means* rather than tunable policy, which is why none of them is an override — see items 3 and 12 in *Changes required in inspect_ai*. The values the definition asked for are recorded in the capture manifest's `options`, so a runner can see what it is overriding.

### 4.2 Scanning

Scanners are **rejected** in worker mode rather than silently dropped, and the reason is worth stating precisely: a scan directory reproduces every hazard that made a shared eval-set log directory unsafe, one level down.

One scan directory (`<log_dir>/scans/scan_id=<scan_id>`) serves a whole eval set, and its bookkeeping assumes a single writer:

- **Create-or-attach is a TOCTOU.** `scan_init` does `if exists(scan_dir): attach() else: init()`, and `init()` resets. N workers starting together all see it absent, and the last one to `init` wipes the others.
- **Finalize prunes another worker's rows.** `_cleanup_orphan_scan_rows` reads sample uuids from *all* logs in the log directory and filters each scanner's parquet down to just those uuids. A worker finalizing while a sibling's log is still `started` computes a `live_tids` set that omits the sibling's samples — and rewrites the shared parquet without the sibling's rows. This is `cleanup_older_eval_logs` again, wearing a different hat.
- **`complete` is eval-set-wide but computed per worker.** `scan_finalize` derives `complete` from a directory-wide status and, when true, cleans up the buffer dir. The first worker to finish marks the whole scan clean, and `scan_already_clean` then makes later passes skip transcripts nobody ever scanned.
- **`_summary.json` is read-modify-write**, so concurrent workers clobber each other's counters.

Worker mode fixed these for logs by *removing* the orchestrator, not by adding locks. Scanning still has its orchestrator inside each worker, so the same fix has to be applied at the same level: **the runner scans, over the log directory, as the single writer.** That does not have to mean waiting for the run to finish — Steward can scan completed logs incrementally as they land, the same way it periodically rewrites `logs.json`, which recovers most of the liveness of in-worker scanning with one writer instead of N.

Because rejection happens at execution, capture records `options["scanners"]` so a runner learns at *enumeration* time that a definition scans, rather than discovering it when every worker fails.

### 4.3 How Steward would take the scan over

Steward cannot simply read `scanners=` out of the manifest and run it itself. Scanners are **live objects the definition constructs** — `Scanner` callables, a `ScanJob`, scanners backed by models built at import time — so they face exactly the constraint that shapes the rest of this design: they exist only in a process that executed the definition. A scan pass therefore has to *be* the definition, executed. That makes it a third mode at the `eval_set()` boundary rather than a Steward-side library call.

What that mode does is small, because Inspect already has every piece:

```python
# at the eval_set() boundary, with the definition's own `scanner` in hand
with scan_context(scanner, scan_id=eval_set_id, log_dir=log_dir):
    for location in scan.logs:
        eval_log = read_eval_log(location)
        scanned = scanned_transcripts_for_resume(scanner, eval_set_id, location)
        for sample in eval_log.samples or []:
            await resume_scan_previous_sample(
                sample,
                scanner,
                scanned,
                sample_semaphore,
                scan_id=eval_set_id,
                eval_id=eval_log.eval.eval_id,
                log_location=location,
                model=eval_log.eval.model,
                eval_spec=eval_log.eval,
            )
```

`scan_context` is self-contained (`scan_init` on enter, `scan_finalize` on exit), and `resume_scan_previous_sample` already encapsulates the skip-if-already-recorded check that makes re-scanning cheap. No task resolution, no dataset matching, no eval loop.

The protocol would mirror the other two: `INSPECT_EVAL_SET_SCAN` naming `{version, scan_id, logs: [...]}`.

**Why not reuse selection mode with `resume` instead.** It is tempting, because `eval_set()` already scans landed logs this way — `_resume_scan_tasks` builds `PreviousTask`s for *successful* logs purely so the sample-reuse path dispatches scans for transcripts whose row never landed. But that route drags in task resolution and dataset matching, and carries a failure mode a scan pass must not have: a sample the reuse path *cannot* match gets executed, silently turning a scan into an eval run with model calls. Scanning a log should never run a sample. A dedicated mode over log locations avoids the question entirely.

**Properties that follow.** Exactly one scan process runs at a time — not by convention but because spawning is serialized by the run claim and recorded, so a tend checks for a live scan before starting one and a restarted Steward adopts an in-flight scan rather than starting a second (see *What enforces single-writer*). All four hazards above are then gone by construction. Passes are incremental: `scan_init` attaches to an existing scan dir and `_invalidate_finalized_flag` flips `complete` back to `False` — the same path `eval_set()` resume uses. Passes are idempotent: a transcript with a row for every scanner is skipped.

### 4.4 A scan is a detached process, not part of a tend

A scan reads whole transcripts and, for model-graded scanners, makes model calls — so a pass over a large eval set can run for hours. That makes it the same kind of thing as a task worker, and it must be spawned the same way: **detached, recorded, and reaped by a later tend.** A tend that ran a scan inline would hold the run claim for the scan's whole duration, which would destroy the property the driver model is built on — that a claim older than a generous tend timeout is unambiguously stale, so no heartbeat protocol is needed. Blocking on long work inside a tend brings the wedged-supervisor problem straight back.

This generalizes past scanning, and scanning is just the case that forces it to be said: **a tend spawns and reaps; it never does long work itself.** Task workers, scan passes, adjudication re-runs, and end-of-run bundling all obey it. Anything whose duration is unbounded is a detached child whose completion some later tend observes.

Two consequences specific to scans:

- **The claim protects the decision, not the duration.** A scan process outlives the tend that spawned it, so the claim is not what keeps the scan directory single-writer — the in-flight record plus a liveness check at spawn time is, exactly as it is for not double-spawning a task. The claim's job is only to serialize the *decision*.
- **Scanning is a drain, not a pass.** Because a scan can outlast the tend interval, Steward cannot start one per tend. The model is a queue of unscanned logs drained by one process at a time. [scheduling.md](scheduling.md) settles its cadence — spawn immediately rather than at a threshold, since a scan is I/O-bound and takes no worker slot; one log per pass, degrading to whatever is unscanned at spawn when scans run slower than logs land. That self-regulates: slow scanning batches more logs, and coverage lags the run rather than stalling it.

It also means a run is not finished when its tasks are. The final scan is a long job that begins after cleanup and adjudication settle (see the ordering constraint above), so `status` needs *complete, scanning* as a distinct state, and end-of-run finalization is itself a multi-tend affair rather than a blocking step.

**The one real ordering constraint.** `scan_finalize` runs `_cleanup_orphan_scan_rows`, which prunes rows whose uuid appears in no current log. So Steward's *final* pass must run **after** log cleanup and after adjudication re-runs have settled — otherwise rows belonging to superseded attempts survive, and rows for re-run samples are keyed to uuids that no longer exist. Mid-run passes are unaffected: in-flight samples have no rows yet, so there is nothing for the pruner to get wrong.

## 5. Log directory

**One flat directory, shared by every worker**, at the definition's own `log_dir`. This is the hard requirement behind the whole design: it is what makes `inspect view <log_dir>` and `samples_df(<log_dir>)` work live, unmodified, with clean task names and no folder column.

Who writes what:

| Path | Writer | When |
|---|---|---|
| `<task>_<id>.eval` | worker | during its own run (atomic replace) |
| `.eval-set-id` | Steward | run start |
| `eval-set.json` | Steward | **rewritten as logs land** — see below |
| `logs.json` | Steward | periodically during the run, and at the end |
| `listing.json` | Steward | only when producing an embedded/bundled view |

Steward is the **single writer** of every shared file, which is what makes the absence of locking in Inspect's manifest writers acceptable. Workers never read the directory to make decisions; their decisions arrive in the selection.

The standard by which this list is judged is **conformance**, not "something reads it". A Steward directory should contain exactly what an `eval_set()` directory contains, because the promise is that nothing downstream can tell the difference. That matters most for the file with no in-repo reader: `logs.json` (`write_log_dir_manifest`) is exported public API, so external consumers may well depend on it even though nothing inside Inspect does. Steward writes it because `eval_set()` writes it.

Worth distinguishing from `listing.json` (`write_log_listing`), which is a different file and the one the **static/bundled viewer** actually fetches. `eval_set()` writes it only via `_embed_viewer` / `bundle_log_dir`, so Steward writes it only when doing the equivalent.

### 5.1 `eval-set.json` must be written incrementally

This is the one entry that cannot be written once at run start, and the reason is worth recording because the obvious implementation is silently wrong.

`eval-set.json` earns its place: the viewer reads it (`read_eval_set_info_async`) to render **pending tasks** — entries in the eval set that have no log yet — via `appendPendingItems` in `LogsPanel` and `SamplesPanel`. That is exactly the live progress view Steward wants, and without the file the viewer shows only landed logs with no sense of the whole set. Absence degrades gracefully (the reader returns `None`), but the feature is lost.

The trap is `EvalSetTask.task_id`. `to_eval_set_task` resolves it as `existing_task_id or task.id or eval_set_identifier`, and `task.id` is a fresh `uuid()` assigned at resolution (`loader.py:112`). In an ordinary `eval_set()` run the manifest's task_ids match the logs' exactly — because one process both resolved and ran. **Steward's workers each resolve independently**, so a worker's log task_id is unpredictable to Steward. Writing `eval-set.json` up front from the manifest would give every task an id matching no log, and the viewer would render each one as *both* landed and pending.

The fix needs no protocol change, just the same algorithm driven from a different source: rewrite `eval-set.json` as logs land, taking `task_id` from the log for tasks that have one and falling back to the task's `identifier` as a placeholder for those still pending. That is `existing_task_id or … or eval_set_identifier` computed from the log directory rather than from `ResolvedTask`s, and Steward is already rewriting directory metadata on the same trigger.

### 5.2 Sharing the directory operations with `eval_set()`

Steward reproducing Inspect's directory bookkeeping by hand is how it would drift. That is the same failure the Hawk integration had — a re-implemented lowering that silently diverged from the real thing — so the operations should be **shared functions both `eval_set()` and Steward call**, not two implementations of one protocol.

Most of this is available already. Grouped by what it would take:

**Shareable as-is.** `cleanup_older_eval_logs(log_dir, task_ids)` takes nothing but a directory and a set of task ids; `latest_completed_task_eval_logs`, `list_all_eval_logs`, `write_log_dir_manifest`, `write_log_listing`, `bundle_log_dir`, and `embed_log_dir` are likewise pure directory operations, several already public. `eval_set_id_for_log_dir` is pure too, and its hard-fail on id mismatch is exactly the "is this directory already owned by a different eval set" check Steward wants. **The cleanup band — the part most likely to be enhanced — needs no refactor at all.**

**Shareable after a narrow refactor**, in each case to drop a `ResolvedTask` dependency that isn't really used:

- `validate_eval_set_prerequisites` touches `resolved_tasks` only to compute identifiers (and to name a task in one error message); every check below that is identifier-level. Split out an identifier-taking core.
- `write_eval_set_info` → `to_eval_set` → `to_eval_set_task` uses only name, id, file, args, model, model_args, model_roles, and sequence — all present in Steward's manifest except `task.id`, which is the per-process uuid Steward must fall back from anyway. Have it take `list[EvalSetTask]` and let the caller build those rows.
- `list_latest_eval_logs` / `log_samples_complete` need dataset size and epochs, which the manifest carries as `samples` and `epochs`.

**Not shareable, and shouldn't be.** Task resolution requires the definition executed. And retry partitioning is genuinely different work: `eval_set()` partitions tasks into pending/failed, whereas Steward adjudicates at sample level. Forcing those together would be worse than two implementations.

**These should be public API, not private-but-shared.** Sharing only protects Steward if the shared thing is *intended* as shared. Importing more from `inspect_ai._eval.evalset` as it stands would couple Steward to more internals that can change without notice — more exposure, not less. A private module maintained "with external callers in mind" is a contradiction that offers no actual guarantee.

This is less of a new commitment than it sounds, because **the surface is already half-public, and inconsistently so**:

| public today | private today |
|---|---|
| `write_log_dir_manifest` (writes `logs.json`) | `write_log_listing` (writes `listing.json`) |
| `bundle_log_dir` | `embed_log_dir` |
| `list_eval_logs`, `task_identifier` | `cleanup_older_eval_logs`, `write_eval_set_info`, `read_eval_set_info`, `eval_set_id_for_log_dir` |

Those splits are historical accident, not design — the file the static viewer actually reads is private while its sibling is public. So the ask is not "expose internals for Steward"; it is "this surface is already partly exposed, here is the coherent version," with Steward as the forcing function. `task_identifier` was made public for exactly this reason during the capture work, so the precedent is already set within this project.

**Two kinds of contract, two mechanisms.** Worth stating explicitly, because the protocol pieces deliberately go the other way:

- **Data crossing a process boundary** — the capture manifest, the selection document — stays private and *versioned* (`EVAL_SET_CAPTURE_VERSION`, `EVAL_SET_SELECTION_VERSION`). A schema change breaks silently, so it needs explicit version negotiation and golden tests.
- **Functions called in-process** — the directory operations — become *public*. A signature or semantic change breaks loudly at call time and in shared tests, so ordinary deprecation policy is sufficient and versioning would be ceremony.

The honest trade is that Steward then receives breaking changes for free along with enhancements. That is still the better side: loud coupling beats silent divergence — which is precisely what happened with Hawk.

**One difference sharing does not erase — and it is now two.** `cleanup_older_eval_logs` keeps the newest log per task id and deletes the rest, but Steward's adjudication model needs failed attempts *kept* until they are resolved. Steward therefore calls it on a different schedule: once at the end, after adjudication settles, rather than on every pass. Same code, different timing; worth recording so nobody moves the call earlier as a tidy-up.

The second difference is behavioural. Steward **never deletes an eval log** ([workflow.md](workflow.md), *Steward never destroys a result, but it does curate the directory*), so where `eval_set()` removes a superseded attempt Steward moves it to the sibling archive. That is a genuine divergence in a design that argues divergence is how Steward drifts — so the resolution is not to fork the function but to widen it: an `archive_dir` parameter that moves rather than deletes, useful to `eval_set()` itself and folded into the public-directory-operations ask. Until it exists, Steward performs the move and calls cleanup with nothing left to remove.

### 5.3 Flow's store, and who is allowed to read it

Flow ships a **store**: a Delta Lake table mapping `log_path → task_identifier`, indexing completed logs so an identical task never runs twice. It holds no log data — only pointers — and is rebuildable with `flow store import`, which is why its own design describes it as a cache whose absence costs time and never correctness.

Two things make it directly relevant rather than a Flow implementation detail. It is keyed on **`task_identifier`** — inspect_ai's identity, the same key Steward keys everything on, and versioned by `TASK_IDENTIFIER_VERSION` in the table's columns. And it works against local disk or S3 with concurrent writers, which is exactly the deployment Steward targets.

**Disabling it wholesale was an error, and Flow had already drawn the right line.** `FlowStoreConfig` carries independent `read` and `write` flags, defaulting to `read=False, write=True` — index everything, match nothing unless asked. `--store none` turned off both to solve a problem with one of them.

| half | what it does | under worker mode |
|---|---|---|
| **write** (`add_run_logs`, default on) | appends `log_path → identifier` rows after a run | harmless — Delta Lake is built for concurrent writers and the operation is idempotent — but **only available to flow definitions**, which is what disqualifies it |
| **read** (`search_for_logs`, default off) | matches wanted identifiers and **copies** the found logs into `log_dir` | **broken, not merely redundant** — see below |

The read half fails for a specific and instructive reason. `find_existing_logs` runs *before* the boundary, so it operates on the whole spec — every task Flow resolved — and knows nothing about the worker's selection. Under N workers that means N identical store queries and N racing `copy_file` calls onto the same destination paths. Worse, a copied log does not stop anything: selection mode deliberately skips `eval_set()`'s reuse logic, so the worker runs its selected task regardless, and the directory ends up with a copied log *and* a fresh log for one identifier. The reuse buys nothing and costs a race.

That is the competing-orchestrator problem again, one level out. **The store read is a scheduling decision, and scheduling belongs to Steward.**

### 5.4 Steward owns both halves, because the store is not really Flow's

The obvious repair is to leave workers writing and move only the read to Steward. That works for Flow definitions and fails the moment you ask the question that matters: **an `evalset` or `hawk` worker contains no Flow code at all**, so there is nobody in it to index anything. A store that only accumulates when the definition happens to be a Flow spec is a store that is empty for two thirds of the projects that would benefit.

Nothing about the store is actually Flow-specific. It is keyed on `task_identifier`, which is inspect_ai's, and `store_factory` accepts a bare path string rather than a `FlowSpec` — so the whole mechanism is already definition-type agnostic. Only its *configuration surface* belongs to Flow.

So Steward takes both halves:

- **Workers run with `store=none`** — which is where this started, but now for the opposite reason. The function moved to the single writer rather than being discarded. *Implemented*: the flow worker command passes `--no-store-read --no-store-write`.
- **Steward reads once, at launch.** It holds precisely the input `search_for_logs(set[str])` wants: the manifest's identifiers. One query, one copy per match, no race, and the copied logs then satisfy the convergence check like any other landed log.
- **Steward writes at signoff, and only then** — see below. Not as logs land.

Three properties follow, and they are the same three the rest of the design keeps arriving at: one writer, uniform behaviour across frontends, and a cache whose loss costs time rather than correctness — a missed row means a re-run, and `flow store import` rebuilds the table from the logs at any point.

### 5.5 Publication is an act of signoff, not a side effect of landing

The tempting implementation indexes each log as it lands: every tend already reads new headers to reconcile, so the row is nearly free. It is also wrong, and the reason is an invariant this document states two sections earlier.

A store row is a **claim** — *this log is a valid result for this identifier* — and a reader acts on it by not running the task. But under `fail_on_error=False` a task that reaches the end of its dataset finishes `status="success"` carrying whatever errored samples remain (*Completion is not success*). At the moment a log lands, anomalies may be open, no scan has run, and nothing has been adjudicated. Indexing then publishes provisional results into a shared cache, where another project inherits them silently and nobody is positioned to notice. It is the failure adjudication exists to prevent, escaping the project that could have caught it.

[workflow.md](workflow.md) already names the moment results stop being provisional: *Steward can compute that no anomaly is open; only a person can say I accept these results.* Signoff is that attestation, and a store row is an assertion that a result may be reused. Same claim, same moment.

So **`steward signoff --publish`** writes the project's signed logs into the store as a batch, and the store's contents strengthen accordingly — from *logs that exist* to *results a human accepted*. That is what makes automatic reads defensible: reuse inherits an attestation rather than trusting that a file is present. A project that is stopped, abandoned, or never signed publishes nothing, which is correct — its results were never accepted.

Three consequences worth recording:

- **A shared store can be mixed.** Flow writes rows itself, on its own default (`write=True`), with no attestation behind them. A team using both tools has a store whose rows carry different warrant, and the schema — `log_path`, `task_identifier`, timestamp — has nowhere to record which is which. Steward cannot fix this from its side; what it can do is not make the problem worse, and say plainly that the guarantee is a property of who wrote the row.
- **Archiving dangles rows.** A store row is a path, and Steward moves logs to the archive. A signed log later superseded by an amendment leaves a row pointing at nothing. The store degrades gracefully by design (unreadable matches are skipped), but Steward owns the tidy-up: archiving a log means removing its row, which `remove_log_prefix` already supports.
- **Accepted exceptions cross the boundary invisibly.** A signed task carrying two samples accepted-as-errored is a legitimate result *with a caveat recorded in this project's `anomalies.md`* — and a project reusing it gets the log without the caveat. Whether such logs should be publishable at all is unresolved, and it is the sharpest form of the question the filter policy below has to answer.

### 5.6 Configuring it

The store is a **machine-level resource**, frequently shared: pointing several machines at one S3 prefix means a colleague's completed task is one your next launch does not have to run. That shape decides where the settings live, and they split along the line this design already draws between mechanics and standards.

| setting | where | why |
|---|---|---|
| **where the store is** | `INSPECT_STEWARD_STORE` (a path, `auto`, or `none`), with `--store` as a launch override | mechanics, and a property of the machine rather than the project — the same reasoning that puts the notification channel in the environment |
| **whether to publish to it** | decided at `steward signoff`, defaulting from `policy.md` | publication is an attestation, so it belongs to the one command a human always runs personally |

**Configuring a store is the opt-in, and it enables reads.** There is no separate switch, because there is nothing to protect against: a match means the identifier is equal, and what it points at was signed off by someone. Reads are still *reported* in the launch delta — visibility, not consent — because a reused log is a result this project did not produce.

Writing has no default beyond what policy says, since it is a decision on a command a human is present for. Someone who never signs off never publishes; someone whose project is exploratory can say so once in `policy.md` and stop being asked.

There is deliberately no `steward.yaml` entry for either ([workflow.md](workflow.md), *There is no `steward.yaml`*): the location is environmental, and the publication rule is prose a human writes.

**This unifies with the archive**, since both are identifier-keyed caches — one project-local, one global. Convergence for a wanted identifier with no log in `logs/` consults them in cost order:

```
1. logs-archive/   project-local — move back (free; a superseded task was restored)
2. flow store      global index  — copy in  (cheap; another project already ran it)
3. otherwise       spawn a worker
```

**Reuse must be visible, because Steward's audit posture is stronger than a cache's.** An identifier match guarantees the task, args, model, solver, resolved plan, generate config, and execution limits are identical — a strong claim, and exactly what identifier equality means. It does *not* guarantee the environment matched: package versions, or a dataset loaded from a mutable source. So a reused log is journaled with its source location and counted in the launch delta ("3 tasks satisfied from the store"), rather than silently becoming a result of this project. Reporting is where it stops: whether an identifier match is good enough to accept is the reader's judgement, not something Steward verifies ([configuration.md](configuration.md), *Reproducibility is the author's concern*).

`FlowStoreConfig.filter` is the lever for the policy that follows — a `LogFilter` restricting what may be matched (only logs that scored, only recent ones). What that policy should be is unresolved; what matters here is that the mechanism exists and belongs to Steward rather than to each worker.

**The one wrinkle is the dependency.** The implementation lives in `inspect_flow._store`, so store support for a Hawk or plain-script project would today require installing the `[flow]` extra — an odd thing to tell someone whose project has nothing to do with Flow. Steward should accept that for now and say so plainly when a store is configured without Flow present, rather than failing obscurely.

**The destination is inspect_ai**, and this is the same move as the public directory operations above: a cache keyed on inspect_ai's own identifier, useful to `eval_set()` directly (cross-directory reuse is not a Steward-specific want), sitting in a private module of an optional third package. If it moves, Steward changes one import — the design above assumes nothing about where the code lives, only that `store_factory` takes a path.

What remains genuinely open is the **filter policy**: `FlowStoreConfig.filter` accepts a `LogFilter` restricting what may be matched — only logs that scored, only recent ones, only from a trusted prefix. That is the concrete form the `policy.md` question takes, and it wants deciding alongside the reuse default rather than separately.

### 5.7 What enforces single-writer

"Steward is the single writer" is a claim about a *process*, and nothing about the architecture so far makes that process unique. Steward detaches, it is restartable, and it is frequently driven by a coding agent — which double-invokes far more readily than a human at a terminal does. So the property has to be enforced, at three levels.

**Within one Steward process** — trivial. Shared-state work (scan passes, `logs.json` rewrites, log cleanup) is serialized in the process that owns the run.

**Across a Steward restart, with detached work still running** — this is what the in-flight record is for. A restarted Steward replays it, finds entries with `launched` and no `exited`, and checks liveness against the control discovery directory using the recorded pid *and process start time*. Work still in flight is adopted, not duplicated. This is why a scan pass must be recorded exactly like a task worker: otherwise a Steward that restarts mid-scan cannot tell that one is already running, and launches a second into the same scan directory.

**Across two concurrent Stewards** — not covered by either of the above, and the actual gap. The mechanism is a **run claim** keyed by the log directory: before performing any shared-state write, a Steward must hold the claim for that directory.

Inspect already has the primitives. `inspect_ai._util.discovery` is written to be shared ("each subsystem only needs to provide its directory and its own schema") and provides pid-keyed JSON with permissions and stale-entry reaping; the control channel and the ACP server are both built on it. A Steward registry at `<inspect_data_dir>/steward/<pid>.json`, recording the log directory a Steward owns, gets the same liveness semantics for free. A mutating command enumerates live entries first and declines to proceed if another live Steward owns the directory.

**With one refinement: pid-liveness is not enough for the claim holder.** It is the right test for a worker, which either runs or does not. A supervisor can *wedge* — deadlocked or blocked on a hung request — and a wedged supervisor keeps its pid, keeps its claim, and blocks the replacement that would have taken over, which is worse than crashing. So the registry entry must carry a **heartbeat** the supervisor refreshes as it makes progress, and "alive" must mean *recently heartbeating*, not *process exists*. A claim whose heartbeat has lapsed is reclaimable, and the reclaiming Steward should terminate the stale holder rather than merely ignoring it, so the two never write concurrently.

Two consequences worth being explicit about:

- **Safety must come from the CLI, not from convention.** Commands split by whether they write: `steward tasks` and `steward status` need no claim; anything that spawns workers, rewrites eval-set metadata, scans, or adjudicates must hold one. A rule that only holds when the caller remembers it is not a rule an autonomous agent will honour.
- **Refusing is the wrong end state.** When a second `steward launch` arrives against a live run, what the caller almost always wants is the *existing* run — so the useful behaviour is to attach and report status rather than error. Refusing with a clear message is acceptable for a first version; attaching is the better destination.

This also settles the shape of the runner: rather than each CLI invocation doing work directly, there is **one long-lived supervisor per eval set** (see *The supervisor*), and the claim is what makes it unique. Everything else — scan passes, metadata writes, cleanup, adjudication — is work that process schedules. "Only one scan at a time" then stops being a rule anyone has to remember and becomes a queue inside the process that owns the run.

Because that process is long-lived and advertises a socket, "is one already running?" also stops being an inference from files on disk and becomes a direct question with a live answer.

**Scope limit:** the discovery directory is machine-local and pid-based, so this covers one host — consistent with the cross-host limitation noted under open questions. A genuinely distributed claim needs a lease *in the log directory* with a fencing token, and that runs into filesystem reality: `log_dir` may be S3, where atomic create-if-absent is not available through the usual filesystem abstraction (S3 conditional writes exist, but support across the stack is uneven). That is the cross-host problem, not a reason to skip the local claim.

The directory is "eval-set conforming" throughout — it has the same files with the same meanings as one produced by `eval_set()` — so nothing downstream can tell the difference.

### 5.8 Multiple logs per task

A task can end up with more than one log: a first attempt that failed, then a resumed attempt. That is the same situation `eval_set()` produces on retry, and Steward resolves it the same way — the latest successful log for an identifier wins, and the final sweep clears superseded failed logs to the archive rather than deleting them. Steward keeps them in place until then, because the attempt history is exactly the diagnostic material it exists to reason about.

## 6. Recovery: one automatic tier, then adjudication

The model rests on **`fail_on_error=False`**: sample errors never mark a task failed, so a task that reaches the end of its dataset finishes `status="success"` whatever residue of errored samples it carries. Because the whole design depends on it, worker mode **hard-codes it** rather than routing it through configuration — a definition asking for fail-fast is asking for a completion decision that belongs to the runner. `continue_on_fail` needs no override at all: it is moot once `fail_on_error` is `False` (`_should_eval_fail` returns `False`, so the mid-run abort it guards can never fire).

Sample-level retry is the opposite case and stays the definition author's to set. `retry_on_error` passes through worker mode untouched, because how many attempts a sample deserves is a property of the eval — a flaky sandboxed task and a pure-inference task want different numbers, and the author knows which they wrote. Steward's assumed default is 3, but it is a default, not a constraint.

The point of forcing `fail_on_error=False` is to convert a binary task outcome into a **sample-level work list**. Under `fail_on_error=True`, three bad samples out of five hundred make the task "failed", and the only lever is a whole-task respawn. Under `False`, the task is done and what remains is precisely "these three samples need resolution" — the granularity adjudication actually operates at.

The definition's own `fail_on_error`, `continue_on_fail`, and `retry_on_error` are recorded in the capture manifest's `options`, so Steward can see what it is honouring and what it is overriding rather than having to guess.

Recovery therefore happens at two tiers — one automatic, one adjudicated with two mechanisms. Whole-task failure is not a fourth: it lands in tier 3 with the unit enlarged, since nothing at that level is retried without a ruling ([scheduling.md](scheduling.md), *Failure is adjudicated, not retried*).

### 6.1 The automatic tier: in-eval sample retry

`retry_on_error` handles transient sample failures inside the worker, with no supervision, at whatever count the definition set. Two properties matter:

- **Retries do not hold a concurrency slot.** Inspect performs the retry recursion deliberately outside the sample semaphore (`_eval/task/run.py`, the `retry_on_error > 0` branch after the sample scope exits), so a retrying sample releases its `max_samples` slot and re-enters at the back of the sample queue. No head-of-line blocking, no deadlock against the cap. The only observable effect is ordering — retries land behind pending samples and so tend to finish late in a task.
- **Exhaustion is terminal but not fatal.** A sample that burns all three attempts is recorded errored, and (with `fail_on_error=False`) the task carries on.

### 6.2 The adjudicated tier, mechanism one: in-flight requeue

Inspect's control channel exposes `POST /evals/{eval_id}/sample/requeue`, alongside `GET /evals/{id}/samples` (listing plus status histogram) and `GET /evals/{id}/sample` (summary **plus error detail**). That is the full loop needed to act on a failure *while the task is still running*: read the error detail and re-open the sample's slot without waiting for the task to end. The sample re-runs inside a task that is already warm, with no respawn and no resume read. Requeue is idempotent — a repeat lands in the already-queued rows and reports `changed: False`.

**Steward never does this on its own.** An earlier draft had requeue acting automatically on a confident transient classification, with a per-sample requeue budget to stop it looping against a provider that stays down. That is now a ruling like any other, for the reason below.

The mechanism matters anyway, because it is what makes an *early* ruling cheap. A human who rules at 2am on a sandbox blip gets those samples re-run in the task still running rather than after it finishes.

### 6.3 Two tiers, not three

An earlier draft numbered three tiers, dividing them by **mechanism** — inside the eval, into a live task, into a finished one. That is one distinction too many, because the line that decides *who may act* falls in a different place and is the one that matters:

> **One tier is automatic; the rest is adjudicated.** A sample gets the attempts its definition asked for — Steward's assumed default is 3 — and when those are gone, further attempts are a conversation between the human and the agent, not a rule Steward executes.

So there are two tiers, and the second has two mechanisms chosen by a detail: whether the task is still running. Requeue and invalidate-plus-resume reach the same samples and answer to the same authority; picking between them is an implementation question, not a decision anyone makes. Neither has a budget, because neither loops — nothing can requeue without a ruling in between, which is the same argument that makes task restarts affordable to ask about ([scheduling.md](scheduling.md), *No automatic restart*).

Three things follow.

**Steward carries no requeue budget.** The guard existed to stop an automatic rule from looping; with no automatic rule there is nothing to bound. Inspect's lack of a per-sample ceiling stops being a hazard.

**Error classification stops being a decision input.** Its remaining job is *grouping* — turning 47 errored samples into one decidable anomaly rather than 47 — which is a presentation problem, not a control problem. That is a large reduction in what a taxonomy has to be right about: a bad grouping produces a confusing anomaly, where a bad automatic classification produced wasted money and a wrong retry. It also removes the redundancy, since Inspect has already asked "is this transient?" twice before Steward sees the error (`ModelAPI.should_retry`, which itself distinguishes `rate_limit` from `transient`, and then `retry_on_error`).

**A pre-authorization is a ruling made earlier, not an exception to this.** `policy.md` may admit a class of re-run — *"sandbox provisioning failures may be re-run without asking"* — and Steward acting on it is executing a decision the human already made, recorded where anyone can read it. That is the same standing-authority move [workflow.md](workflow.md) makes for scaling, and it is what keeps the rule from meaning "wake someone up for every flaky container". Nothing is pre-authorized by default.

### 6.4 Considered and declined: pausing a failing model

An outage looks like the one place a mechanical response might beat adjudication. With `fail_on_error=False`, the default behaviour when a provider dies is not "wait" but **destroy** — every in-flight sample burns its `retry_on_error` attempts against a dead endpoint, errors terminally, and takes its sandbox and accumulated conversation with it. Inspect's hard pause (`pause --now`) would park each sample before its next `generate` with the sandbox intact, and one model-scoped call reaches every task on that model. On a long-episode agentic benchmark that is hours of work saved.

**It does not work, for a timing reason that no amount of policy fixes.** A sample dies within a few minutes of the outage starting: the model API exhausts its own backoff, then `retry_on_error` restarts the sample from the top twice more, each attempt failing at its first call. The tend interval is ten minutes. By the time a tend could observe the error cluster and issue the pause, **the fleet it would have protected is already gone.** Closing that gap means either polling faster than a tend — which is a daemon, and [the driver argument](#8-the-supervisor) rejects one — or putting the response in-process beside `should_retry`, which is Inspect's layer and not Steward's to build.

**The loss it was protecting against also has a better answer already.** Inspect ships checkpointing — `Checkpointer` with time, token, and turn triggers, restic-backed sandbox state capture, and a retry path that scans for the latest committed checkpoint. A checkpointed sample resumes rather than restarting, which beats pausing on every axis: it survives worker death and host loss and not just provider outages, it has no interaction with sample limits, it needs no unpause trigger, and it belongs to the definition author alongside `retry_on_error`. Where hours of agentic work are at stake, that is the mechanism to reach for.

**What declining it avoids is not small.** Hard pause holds a sample while its `time_limit` keeps running — Inspect names this explicitly as "the operator's risk with `pause --now`" (`working_limit` is protected, since held time is credited as waiting time). A sample killed that way records as a limit exhaustion, which [workflow.md](workflow.md) treats as *the measurement working as designed* rather than an anomaly — so an over-long pause would silently launder a Steward-caused failure into an accepted result. Guarding that meant computing each pause's bound from the tightest remaining `time_limit` among the samples it held, journalling every pause window, and reclassifying limit exhaustions falling inside one. All of it in service of a response that arrives too late.

So the rule stays clean, with no exception to reason about: **the automatic tier is `retry_on_error`, everything past it is adjudicated.** A provider outage produces a cluster of errored samples on one model, which is one anomaly with many instances and one ruling — and re-running errored samples on resume is free.

Hard pause remains available as an *action*, since a human or agent may well want it — "the provider is down, hold everything on sonnet" is a reasonable ruling. It carries the `time_limit` caveat above wherever it is used, and note that model gates key on a task's **primary** model, so role and grader models need task-scoped pauses instead (the manifest carries `model_roles`, so Steward knows which tasks those are).

### 6.5 The adjudicated tier, mechanism two: invalidate and resume

When a task finishes, its errored samples are an explicit queue of unresolved work. Steward reviews them and can re-run them by respawning the worker with `resume` pointing at the log. Inspect's resume path already distinguishes the two cases Steward cares about (`eval_log_sample_source` in `_eval/task/run.py`):

| Prior sample state | On resume |
|---|---|
| completed, no error, not invalidated | reused as-is |
| **errored** | re-runs, seeded with the prior attempt's error history |
| **invalidated** | re-runs fresh |
| absent | runs |

So errored samples re-run on resume **for free** — invalidation is not required for them. `invalidate_samples(log, uuids, provenance)` extends the same path to samples that *completed* but that Steward judges bad: a suspicious score, a scorer anomaly, a run that overlapped a known-bad window. It also sets `EvalLog.invalidated = True`, which is the standard marker that a log is not final.

`ProvenanceData` carries a timestamp, author, reason, and metadata — so every adjudication decision leaves an audit record of *why* Steward re-ran something. For an autonomous runner acting on a human's behalf, that record is not a nicety; it is the artifact that makes the autonomy reviewable.

This all works against the resume path as built: worker mode reads the prior log header-only and passes its file info through, and Inspect reads each prior sample lazily from the file, checking `sample.error is None and sample.invalidation is None` per sample. No full-log read is needed to decide reuse.

### 6.6 What is left for task-level recovery

With tiers 1–3 covering sample failures, whole-task recovery narrows to what no sample-scoped mechanism can reach:

- **Errors outside sample scope** — dataset load, task setup, sandbox provisioning, scorers and metrics at the end. These still produce `status="error"` regardless of `fail_on_error`.
- **Hangs.** A sample past its `time_limit` or `working_limit` records a limit event, not an exception, so `retry_on_error` never fires.
- **The process dying** — OOM, host loss, SIGKILL. Not an error from Inspect's point of view at all.

All three are handled the same way, and **not automatically**: a failed task opens an anomaly, and respawn-with-`resume` is the action a ruling authorizes ([scheduling.md](scheduling.md), *Failure is adjudicated, not retried*). That makes this less a separate mechanism than the adjudicated tier arriving from a different direction, where the unit happens to be a task rather than a sample. The reasoning is short: `fail_on_error=False` has already absorbed everything sample-shaped, so what reaches this level is mostly structural and mostly deterministic, and respawning a definition that will not import spends money to arrive where it started. Rarity is what makes asking affordable, and classed anomalies are what keep one reboot from costing forty questions.

And a worker performs **no task-level retry of its own** — worker mode forces `task_retry_attempts=0` regardless of the definition's `retry_attempts`. Honouring the definition's value would multiply Steward's attempt budget by it and leave a failed log per in-process attempt in the shared directory. With three sample attempts already inside, an in-process task loop would be a third multiplier on the same failures.

This reverses the lean recorded in configuration.md's open question 1. Worker mode changed the trade-off: with one `eval()` per process, in-process task retry and a Steward respawn are the same operation at different levels, and `resume` preserves the sample reuse that made the in-process version worth keeping.

### 6.7 Two invariants this model creates

**Completion is not success.** With `fail_on_error=False`, a worker exits 0 and its log says `success` even if every sample errored. Worker exit status therefore carries no information about whether the work is good — Steward must read the log, always. This is the sharpest version of the rule that the log directory is ground truth.

**An eval set is not done while samples are unresolved.** `fail_on_error=False` removes the mechanism that used to make a broken run loud. A task whose model API was down for its entire duration now completes "successfully" with five hundred errored samples and a meaningless score, and nothing in the log's status says otherwise. Adjudication is consequently not optional garnish — it is the only thing standing between a broken run and a plausible-looking result. Steward must treat *any* errored sample as unresolved state, and must refuse to report an eval set complete until every sample is either resolved or explicitly accepted by a human. Metrics computed while the queue is non-empty are provisional and must be reported as such.

**A trap worth naming, because Inspect's own code steps into it.** `eval_set()` decides a log is complete with `log_samples_complete`, which compares `results.total_samples` against the expected count — and `total_samples` is "dataset samples × epochs", errored samples included. `completed_samples` is the field that means "completed without error". Under `fail_on_error=True` this is harmless, because an errored sample already made the log `status="error"`. Under `fail_on_error=False` it is not: 497 good plus 3 errored reads as `status="success"`, `total_samples == 500`, therefore complete. Inspect's own machinery would call that task done and never revisit it — only `invalidated` reopens it (`list_latest_eval_logs` routes invalidated logs to the retry bucket).

Worker mode skips that classification entirely, so nothing is broken today. But it means Steward must not reimplement completeness the obvious way: **completeness is `completed_samples`, never `total_samples`.**

## 7. Detachment and the in-flight record

Steward spawns workers **detached** (`start_new_session`, which is why Steward is POSIX-only — see below) so a run survives Steward exiting — including the supervisor exiting, which is why workers are not its children (see *The supervisor*). Note that Inspect's `--detach` is a *CLI* feature (`inspect eval --detach`), not an `eval_set()` kwarg, and Steward's workers are never the Inspect CLI — so Steward does its own detached spawn rather than passing a flag through.

**Steward is POSIX-only, and this is where that is decided.** `subprocess` reaches `setsid()` and nothing equivalent on Windows: `start_new_session` is silently ignored there, so a Windows worker would stay attached to its console and take a Ctrl-C or a window close with it — the one thing requirement 4 exists to prevent, failing silently rather than loudly. Windows would need `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` and a second story for everything else that leans the same way (AF_UNIX control sockets, `getsid`, process-table identification, the signals fault injection sends). That is a second execution model, not a flag, so it is **declined rather than deferred** — declared in the package metadata, and refused at the spawn itself, before anything is written. A classifier is metadata that `pip` does not enforce, and the one thing worse than an unsupported platform is one that appears to work.

Detachment creates the tracking problem: Steward must be able to answer "what is running right now?" after a restart, without a live parent-child relationship.

### 7.1 Why `task -> pid` is not enough

PIDs are recycled, are meaningful only on one host, and — critically — are *unknown during the window between deciding to spawn and the spawn returning*. A crash in that window leaves a worker whose existence Steward has no record of. So the tracking artifact is an **append-only record** (`.steward/inflight.jsonl`), not a table of current state:

| Record | Written | Carries |
|---|---|---|
| `intent` | **before** the spawn | task identifier, display key, attempt number, selection path, argv, cwd, target log dir |
| `launched` | after the spawn returns | pid, process start time |
| `exited` | when the worker is reaped or observed gone | an exit status only when there is one |

Every record carries the worker's file stem, which is what the three are keyed on, and the host that wrote it. **The control socket is not among them.** An earlier draft had `launched` carry it, which cannot work: the socket is bound when the worker reaches its `eval_set()` boundary, long after the spawn returns. It is discovered rather than recorded (§7.2).

**The exit status is usually not knowable, and nothing should be built on it.** `start_new_session` detaches a worker from the terminal, not from its parent: it stays the spawning process's child until that process exits. But a tend exits in seconds and a worker runs for hours, so by the time anything observes the worker gone it has been reparented and its status reaped by init. A status is therefore available only in the rare case where a worker finishes inside the tend that spawned it — which is why the reap action carries none, and why *observed gone* rather than *exited with* is what the record can promise.

Current state is *derived* by replaying the record, never stored. An `intent` with no `launched` is the ambiguous case, and the process table settles it (§7.3): a worker whose spawn never returned left nothing running, and must not hold its task forever.

Process start time was recorded alongside the pid to defeat PID recycling. **It no longer carries that weight**, because the pid is never asked about alone: the resolver asks whether the recorded pid is among the processes running *this selection document* (§7.3), and a recycled pid inherits none of that. What the start time still buys is a diagnosable record — two entries claiming one pid can be told apart after the fact. It is provenance, not a correctness mechanism.

### 7.2 Control discovery supplies the socket, not the answer

Inspect maintains a discovery directory — `<inspect_data_dir>/control/<pid>.json`, holding `pid`, `socket_path`, `started_at`, `run_id`, and the control API version, with stale-PID reaping in `list_alive_discovery_entries`. An earlier draft read that as *liveness is a solved problem to consume*, which overstates it in the one direction that matters: a discovery entry appears when the worker reaches its `eval_set()` boundary, so during the window in §7.3 a live worker has none. Discovery is therefore consulted for **the socket of a worker already known to be running** — the address everything downstream talks to — and the process table is what says which workers those are.

It also cannot supply the task mapping (the entry carries the eval's `run_id`, not the identifier Steward scheduled) or the pre-spawn intent, which is what the in-flight record adds.

### 7.3 The process table is the liveness source, and the record is what it cannot know

Ground truth is the log directory, and for a worker that has reached its eval the claim holds exactly: a record that is lost, truncated, or stale degrades Steward to scanning the log directory and the discovery directory to rebuild state — slower, never wrong.

**There is one window where it is wrong, and it is not brief.** A worker becomes visible to both ground-truth sources at the same moment — the control server writes its discovery file and the recorder opens its log — and that moment is *after* everything the definition does on the way to `eval_set()`. For a plain script that is imports: a second or two. For Flow it is the measured ~1.2s of spec resolution and the requirements freeze (open question 1). For Hawk it is `uv pip install` into the running interpreter, secrets resolution against Secrets Manager, and building the entire cross product ([hawk.md](hawk.md), *Pre-boundary work that must not be per-worker*) — measured at ~0.5s warm on a plain config, but unbounded on a cold environment or one declaring `packages:`, since it is a real dependency install. Throughout that window a live worker has no log, no discovery entry, and cannot be found by scanning anything. The only thing that knows it exists is its `intent` record.

**Self-identifying workers close the window**, and they cost nearly nothing. Steward writes each worker its own selection document at a known path, and that path is in the worker's environment — `INSPECT_EVAL_SET_SELECTION` is how the worker is told to be a worker at all — so the process table answers *"is a worker for this task already running?"* without consulting any record at all, from the instant the process exists rather than from the instant its eval starts.

**And it answers for one workspace.** The scan is bounded by `.steward/workers/`, not by the marker's presence, so several Stewards on one machine — a common enough shape, since a workspace is just a directory — each see only their own fleet. The bound is the *workers directory* rather than the workspace root, which is what makes the nesting case fall out for free: a workspace inside another workspace's tree keeps its selections under its own `.steward/`, so neither appears in the other's answer, in either direction. Machine-global state is consulted only after the fact — the control discovery directory holds every Inspect process on the host, including a hand-run `inspect eval`, and is read as a `pid -> socket` lookup for pids the scan has already established are ours.

**The environment rather than argv, which an earlier draft assumed.** Argv is only Steward's to compose for a raw `eval_set()` script; a Flow or Hawk worker is that platform's CLI, and an extra positional argument is a parse error rather than a harmless marker — precisely the two definition types whose pre-boundary window is long enough to need this. Reading it costs a `psutil.Process.environ()` per candidate process instead of a `cmdline()`, which is why *nearly*.

**And so the scan is the ordinary path, not the fallback.** An earlier draft kept it as a recovery route, with the record as the answer and the process table as what rebuilt one that was lost. Measurement inverted that: on a table of 756 processes, `process_iter` costs 52ms and `environ()` over all 544 same-user ones costs 6ms — **~60ms for a full sweep**, affordable on every tend. Two things follow. The scan is *exercised*, where a recovery path taken only after a crash is not, which is the same argument that makes recovery the ordinary code path everywhere else in this design. And the record shrinks to what a process cannot tell you: that a spawn was attempted at all, and which task, attempt, and command a running worker belongs to.

**A worker's descendants carry the marker too, and the pid is what separates them.** An environment is inherited, so a sandbox's `docker`, a frontend's `uv`, and every other subprocess an eval starts matches the same selection path — which makes the marker a *subtree* test rather than a process test. Two rules narrow it back. The scan keeps only the **ancestor-most** match of each subtree, which is unambiguous while the worker lives: its own parent is the tend that spawned it, and that carries no marker. And where the record knows a pid, that pid decides — a selection whose only surviving process is a leftover child reads as *departed*, because an orphan that could hold a task open forever is worse than a task retried once.

So the pid is not what the path match replaced; it is what the path match made safe to use. The question is *is the process we launched still running this selection*, and neither half answers it alone: a recycled pid fails the path test, and a descendant fails the pid test. Losing the record therefore costs provenance and exactly one degradation — an unrecorded worker still appears in the scan with its identifier read from the selection document, but with no recorded pid to check it against, an orphaned child of a dead worker reads as the worker.

(psutil is already a hard dependency of `inspect_ai`, but Steward declares it directly: a transitive dependency is not a promise, and this is now load-bearing.)

**Knowing a worker exists is not the same as knowing what it did**, and in this window nothing else records that either. So each worker's stdout and stderr go, merged, to a file beside its selection document (`.steward/workers/`), which is the only account of a definition that died on the way to `eval_set()` — a bad import, a Hawk dependency resolution that failed, a frontend that rejected its own arguments. It is written append-rather-than-truncate, so a duplicate spawn interleaves with a worker that may still be running instead of erasing what it printed.

**One gap survives and is deliberately left open**: the sub-second window between writing `intent` and the process appearing in the process table, and a spawn that failed inside it. An earlier draft closed this with a quarantine rule — no respawn until a full tend interval had passed since the last `intent` — and that is not worth its cost. It introduces a wall-clock timing rule into a design that otherwise has none, and a *starting* state that exists only to express it, in exchange for avoiding an outcome that is merely wasteful: two workers resolve independently, take different `task_id` uuids, and both logs land, so a double-spawn reads as an ordinary retry rather than as an error. Paying a tend interval of latency on every genuinely lost spawn to avoid a rare duplicate is the wrong trade.

> **Not to be confused with the journal.** [workflow.md](workflow.md) uses *journal* for `journal.jsonl`, the durable record of anomalies and adjudication rulings — the one file in a workspace that cannot be reconstructed. The in-flight record described here is its opposite: disposable, machine-only, and rebuildable from the process table at any time.

### 7.4 The parked worker

Detachment costs a worker its human channel, and the loss is silent. Inspect dispatches human-in-the-loop requests **ACP → Textual panel → console** (`util/_input/builtin.py`). On a Steward worker the first is off, the second raises `NotImplementedError` because there is no Textual display, and the third drives Rich prompts against `stdin=DEVNULL`. `console_handler` catches only `KeyboardInterrupt`, so the resulting `EOFError` propagates into the tool call — and under `fail_on_error=False` a request for a human decision lands as **an errored sample in an otherwise successful log**. Not a hang, not a visible failure: an anomaly that does not say what it is.

So worker mode turns the ACP server on (item 12), and a request parks until a client attaches instead. That creates a fourth in-flight condition, distinct from the three the record above can already tell apart:

| condition | process | progress | resolution |
|---|---|---|---|
| running | alive | yes | itself |
| hung | alive | no | a fault, adjudicated |
| gone | dead | — | reaped, then rescheduled |
| **parked** | **alive** | **no** | **a person** |

**Detection rides the read a tend already performs.** The state lives on the eval primitive, not in the ACP layer: `ActiveSample.pending_interaction` returns `"approval" | "question" | None`, derived from two integer counters the routing shims increment on entry to the park and decrement in `finally`. The control server's sample listing walks the same `active_samples()` and already carries an `activity` field for the sample's in-flight operation, classified `tool` → `model` → `retry_wait`. Adding an approval/question branch at the front of that chain is a few lines on data the row builder already holds, in a function whose contract is *O(in-flight ops), never an event scan* — which is why this belongs on the control channel rather than being queried per sample over ACP.

**It is not observable today, and it is worse than not observable — it is camouflaged.** The pending `ToolEvent` for a stage is created before its approval is awaited, so a sample parked for six hours reports `status: running, activity: {type: "tool"}`, indistinguishable from a tool call that has been running for six hours. Neither `InputEvent` nor `ApprovalEvent` helps: both are written only once the interaction concludes. That is why item 13 is not an optional nicety and must not land after item 12 — shipping the park without the signal would trade a loud errored sample for a blind stall.

**Steward detects; it never answers.** Answering an approval is authority over what the eval does, and that is the human's ([agent.md](agent.md), *What the agent may do without asking*). So Steward's whole job is to notice and to say where to attach — and since the ACP discovery file is per-pid, carrying `{pid, eval_id, socket_path}`, and Steward already records the pid, the tend summary and `status.md` can print the `inspect acp` command that reaches that worker. Steward therefore needs no ACP client of its own. This is also, incidentally, the answer to *a detached run cannot be watched live*: any worker can be attached to, not only a parked one.

**A park is not an anomaly, and should not be filed as one.** Anomalies in this design are post-hoc facts about landed logs — they have an exception type, a raising frame, a class, and precedent ([workflow.md](workflow.md), *Three levels*). A park has none of those. It is a live blocking condition on a running process, it resolves by someone answering rather than by someone ruling, and it leaves no trace once answered. It belongs in the tend queue as blocked work, not in `anomalies.md`.

**And it is not deadlined.** Hawk bounds its equivalent with `approval_timeout_minutes` — one week, then auto-reject — implemented by wrapping the approval policy before handing it to `eval_set()`. Steward cannot do that, because the definition builds the policy, and should not want to: auto-rejecting a tool call silently changes what the eval measured, which is the same objection that rules out every other automatic answer to a question only a human can settle. A park waits.

**A parked worker holds its slot.** It still holds its sandbox, its model connections, and its process, so excluding it from the worker ceiling would silently overrun the resource budget the ceiling exists to bound. The consequence follows and is intended: enough parked workers stall the fleet, and at the ceiling they stop it. Nothing should proceed while decisions pile up. `reconcile` must therefore read a parked worker as *running* — not restart it, not reap it, not count it toward a task needing a spawn.

**This is the one thing that breaks the survivability claim.** Elsewhere this design says the fleet keeps converging while an absent agent's decisions merely accumulate ([README.md](README.md) §2, [agent.md](agent.md) §2). With parked workers holding slots that is no longer unconditional: a definition with human approvers accumulates decisions *and* loses throughput, to zero once every slot is parked. Nothing here can fix that — a human decision requires a human — so what keeps an overnight run viable is notification, and a park is exactly the *a run needs you* kind ([workflow.md](workflow.md), *Four kinds*). It is the strongest case yet for upstream item 7.

## 8. The supervisor

"Supervisor" here means *whatever is currently driving the reconcile loop* — in the current design a coding agent scheduling `tend` calls, with `cron` as a backstop (see *The reconcile core, and its drivers*). The claim, the in-flight record, and the registry apply to every driver equally.

**This section designs the detached case, which the current plan does not build.** It is kept because it is the only driver with a lifecycle worth designing, and because writing it down is what established that the lifecycle is a cost rather than a capability — the argument for a short-lived tend on a timer is largely assembled from the paragraphs below. Read it as the specification that would be implemented *if* something ever calls for a stateful supervisor, not as work in the queue. Note the timer that now guarantees the cadence is emphatically **not** this: it carries no state between tends and holds no claim of its own (*Drivers, one core*).

**The run and the supervisor have separate lifetimes.** Requirement 4 says the *run* outlives the process that started it, and detached workers achieve that by themselves. If the supervisor dies, workers keep running and keep writing logs; what stops is *supervision* — no new tasks scheduled, no scan passes, no requeues, no adjudication. That is a real degradation but a graceful one, and it is why both levels detach rather than making workers children of the supervisor: either layer can die without taking the other with it, and a replacement supervisor adopts the survivors from the in-flight record.

**Supervision still has to outlive the invoking session** — unless something else is tending it. Steward exists to run things autonomously for hours, and a coding agent that starts a sweep and then ends its session must not leave it unsupervised until someone notices. Detaching is one answer; an agent that reliably schedules `steward tend` is another, and often the better one. What must not happen is a long run with *no* driver.

**If a detached supervisor is ever built, it would be launched rather than attached to.** There is no foreground driver in the current design — `steward launch` spawns workers and returns, and the loop is driven by scheduled `tend` calls — so the interactive/detached choice Inspect offers does not arise. What is still worth borrowing wholesale is Inspect's contract for detaching: `exec_detached` spawns the child, waits for its `launch` record, and **refuses to leave a detached process running when it failed to bind a control endpoint** — on the grounds that a detached process nobody can observe or cancel is worse than no process. A detached Steward supervisor should be held to the same rule: it advertises its socket in the registry, and if it cannot, the launch fails rather than orphaning an unsupervisable daemon.

That yields a pleasing symmetry: the supervisor is to `steward status` what a `--ctl-server` eval process is to `inspect ctl` — a detached process advertising a socket through a discovery directory, with the CLI as a thin client.

**Not everything needs one.** `steward tasks` is pure enumeration and never touches a supervisor. `steward status` queries a live supervisor when there is one and otherwise reads the log directory directly — slower, same answer, because the log directory is ground truth either way.

**The costs are real and worth naming.** A daemon is a thing to operate: its own diagnostics have to go somewhere, it needs a clean stop, and upgrading the Steward package while a supervisor from the previous version is still running is a version-skew problem the protocol between CLI and supervisor has to tolerate.

### 8.1 What the supervisor decides, and what it escalates

The argument for a supervisor is not that a process is more reliable than a coding agent. It is that **the agent is intermittent and the run is not.** An eval set running for eight hours spans many agent sessions, or none; the agent ends its session, hits a context limit, or is simply not invoked again until morning. Anything that must happen on a cadence — reaping dead workers, starting the next task as a slot frees, scan passes, requeueing a clearly-transient failure, writing a periodic status summary — cannot depend on someone being present to ask for it.

That argument establishes the need for a *cadence*, though, not the need for a *daemon* — a crontab line supplies one at a fraction of the cost. What follows is still the right division of labour; it just does not require a long-lived process to enforce it.

So the division is by *kind of work*, not by reliability:

- **The supervisor keeps the run alive.** Mechanical continuity: maintain the worker pool, record what it launches, run scan passes, requeue within budget, keep the eval-set metadata and status current. All of it policy execution, none of it judgement.
- **The agent (or human) decides what the run means.** Is this error class systemic or incidental? Is this arm worth continuing? Is this score anomalous enough to invalidate? Should this run keep going at all?

The supervisor's other job is therefore to **leave a good trail for the intelligent-but-absent party**: an escalation queue it refuses to act on alone, and a periodic written summary. That is what makes something like an hourly `status.md` load-bearing rather than decorative — it is the handoff artifact the agent reads when it next appears, and the reason a run can be picked up cold.

This division softens considerably when the agent is itself the driver: it sees each reconcile as it happens, so judgement and continuity coincide and the escalation queue never has to accumulate for long. The division still matters — the agent may stop tending at any point, and whatever is left behind has to be legible to whoever picks it up — but it becomes a property of the *artifacts* rather than a split between two actors.

**The honest caveat.** A daemon has a failure mode `tend` does not: it can *wedge* — deadlocked, or blocked on a hung request — while still looking alive. A scheduled `tend` that fails simply does not run; a wedged supervisor holds its claim and blocks the replacement that would have taken over. That is strictly worse than being dead, and it means pid-liveness is not a sufficient definition of "the supervisor is up" (see the heartbeat note under *What enforces single-writer*).

### 8.2 Interacting with a detached run

> **Superseded in part.** [workflow.md](workflow.md) concludes there should be no `steward tui`: a live view presumes a present human, which is the case Steward is explicitly not built for, and `steward status` plus `inspect view` covers what someone actually wants on returning. This section is retained because its *separation* argument — that a view is a client of the same surface as everything else, needing no claim and no live supervisor — is what made that conclusion safe to reach. Read `steward tui` below as "a view, if one is ever built".

The display is an aggregate — tasks by state, sample progress, the adjudication queue, token usage — assembled from the worker control endpoints and the log directory. It cannot be Inspect's own display relayed, because workers are separate detached processes writing their own output elsewhere. That constraint turns out to be a gift: the display is a **client of the same surface** everything else uses, not a privileged view of in-process state.

Which means the view and the supervisor are separable, and should be separated:

| | driver | view |
|---|---|---|
| `steward launch` | none — spawns and returns | none |
| `steward tend` | this process, briefly | none |
| a view, if built | wherever it already is, or nowhere | in this process |

`steward tui` attaches a live display to whatever supervisor is running — the natural way to keep an eye on a detached run for a while and then walk away. It needs no new machinery: it is `steward status` rendered continuously instead of once, over the same registry lookup.

Three properties follow, all of them good:

- **Views need no claim.** The claim is a *writer's* lock. Any number of TUIs can watch one run, and a human and an agent can watch the same run simultaneously. Directives issued from a TUI still go to the supervisor, which serializes them, so even an interactive view stays safe.
- **A view works without a supervisor.** With none live, `steward tui` renders read-only from the log directory — a finished run, or one whose supervisor died. Same fallback as `steward status`, same reason: the log directory is ground truth. With tending done by a short-lived timer-driven verb this is not the fallback but the *ordinary* case: between tends there is no process to attach to, and the TUI is simply `steward status` on a repeat, watching workers it does not own.
- **There is no attached-versus-detached mode to choose.** With `launch` non-blocking and the loop driven by scheduled `tend` calls, a view is only ever a separate process watching from outside. The two code paths that this section was originally reconciling never come into existence.

One consequence survives regardless of whether a view is ever built, because it applies to any interactive surface Steward grows: Ctrl+C must detach the *view*, not the run. Inspect's Ctrl+C cancels an eval, but a Steward run is a longer-lived thing that a coding agent may have started and a human may merely be visiting, so "leaving" and "stopping" must be different gestures — `steward stop` for the latter, with the TUI saying so on exit. This is the `docker attach` / `tmux` convention rather than the `inspect eval` one, and it removes a genuine "did I just kill my overnight sweep?" hazard.

Beyond the TUI, the same surface is reached through the CLI: `steward status`, `steward pause`, `steward resolve …` are thin clients that locate the supervisor via the registry, falling back to the log directory for reads when none is live. **The CLI is the agent's API** — a coding agent should never need to speak the wire protocol, and making it do so would be a design failure rather than a power feature.

Two layers of control channel then stack, and the direction matters:

- **agent → supervisor** (Steward's own surface): status, pause, abandon an arm, resolve samples, retune concurrency.
- **supervisor → workers** (Inspect's `ctl`): requeue, retune limits, pause a model.

An agent *can* read a worker's endpoint directly and that is harmless. It should not issue directives there: requeue budgets and escalation state live with the claim holder, and a second party issuing directives puts that accounting in two places. Reads fan out; writes go through the supervisor.

### 8.3 The reconcile core, and its drivers

The supervisor is not the architecture. The architecture is a **pure function**:

```
reconcile(manifest, inflight, log_dir) -> (actions, summary)
```

Given the eval set's definition-derived manifest and the current on-disk state, decide what to do: which workers to spawn, which finished, what needs a scan pass, what needs adjudication. Nothing in it depends on memory carried from a previous call, because the design already guarantees that everything the supervisor knows is reconstructible from the in-flight record and the log directory — the supervisor is a *cache*, never a source of truth.

Committing to that shape buys three things that are hard to get any other way:

- **Exhaustive testability.** Scheduling correctness becomes "given this directory state, what actions?" — unit-testable without clocks or processes. For a component that decides whether expensive evals run unattended and correctly, that is not a nicety.
- **Crash recovery is the normal code path.** There is no separate resume routine to get wrong; recovery is just the next call, exercised constantly.
- **Driver independence.** A wedged long-lived process stops being frightening: kill it, and anything else can drive the same function.

**Drivers, one core.** The mechanical tend is **guaranteed by a timer**; the agent is a judgement client that may be attached, periodic, or absent.

| driver | role |
|---|---|
| **a system timer** — `cron`, `systemd`, `launchd`, or Hawk's in-pod loop | the floor. Runs `steward tend` on an interval, always |
| **Steward's own detached ticker** | the fallback where no system facility exists |
| **the coding agent** — calling `steward tend` when it wants one now | not the schedule; a client that can also force a turn |
| detached long-lived supervisor holding a claim | **still rejected** — see below |

**This is not the daemon that was rejected, and the distinction is the whole point.** The rejected thing was a long-lived *supervisor*: a process that holds the run claim for hours, needs a heartbeat protocol to detect wedging, and blocks its own replacement when it hangs. What is being scheduled here is the existing short-lived verb — a timer wakes, `steward tend` runs for seconds, takes the claim, releases it, exits. Every property the no-daemon argument bought survives untouched, because they were properties of the *claim's lifetime* and of `reconcile` being pure, not of who called it.

**Why the agent cannot be the floor.** An agent is turn-based: it acts when a human speaks, when a background job completes, or when a scheduled wake-up fires, and never in between ([agent.md](agent.md)). So an agent-scheduled run is silent by construction whenever no agent is in session — and silence is indistinguishable from a healthy run. Worse, the design has steadily added responsibilities to the agent, so an absent one now stops *everything* rather than merely delaying judgement. Making the mechanical half a timer separates those failures:

- **With a timer and no agent:** the fleet keeps converging, logs land, scans drain, `status.md` stays current, mechanical notifications fire. What accumulates is judgement — unruled anomalies, unprobed scan results.
- **With no timer and no agent:** nothing happens at all, and nothing says so.

The first is a run waiting for a decision. The second is a stalled run that looks identical to a healthy one. Only the first is acceptable overnight.

This is also just [hawk.md](hawk.md)'s answer generalized. The pod already runs an in-pod timer as the floor with an external agent supplying judgement over the relay, on the reasoning that *a pod is expensive and an agent can stop being called*. That reasoning was never Hawk-specific; see *Driving and judging are separate roles that usually coincide*.

**The three agent postures this admits**, all of which now work:

| posture | how it learns a tend happened | latency to judgement |
|---|---|---|
| **attached, reactive** | a monitor on the tend output or the journal wakes it | seconds |
| **attached, periodic** | checks on its own cadence, or calls `tend` to force one | its own interval |
| **transient** | reads accumulated state on attach, as runbook policy | until someone opens a session |

The third is the common case and the one the old arrangement served worst: somebody opens a session in the morning and the agent reads what the night produced. Cold pickup ([agent.md](agent.md)) is exactly this procedure, which is why it runs several times a night rather than only when a stranger arrives.

**The ticker fallback is small, and must stay small.** Where no system scheduler is available, `launch` spawns a detached process whose entire behaviour is *sleep, run `steward tend`, repeat*. It holds no state between iterations, takes no claim of its own, and is reaped and observed exactly like a worker. It is not a supervisor and must not become one — the moment it carries state between tends, the wedging problem it was designed around comes back. Its one real weakness against `cron` is that it does not survive a host reboot, which is a reason to prefer a system facility rather than a reason to grow the ticker.

**`launch` arms a timer or refuses.** A timer nobody armed is the failure this whole section exists to prevent, so it cannot be a step in a runbook that someone remembers, and it cannot be a warning either — a warning at launch is read once, by someone who is about to walk away. Detection is ordinary: prefer a system scheduler where one is usable, fall back to the ticker, name the choice in the launch output. **If neither can be armed, the launch fails**, saying what it tried.

The escape hatch is explicit and recorded, following the pattern `signoff --force` already sets: `--no-timer` launches unsupervised, writes that fact to the journal, and makes `status.md` say the run is unsupervised for as long as it stays that way. The point is not to forbid hand-driving a short run — that is legitimate — but to make an unsupervised run *look* unsupervised, rather than looking exactly like a healthy one.

**Confirming it is a separate problem, because the timer cannot detect its own absence.** Two things cover it, and neither costs anything. The first tend records that it ran, so a journal with no tend event an interval after launch means the timer never fired. And `status.md` states its own age ([workflow.md](workflow.md) open question 5), which is what a remote observer reads — a file that stopped changing is the only signal that supervision stopped, whoever was providing it.

**Two ages, not one.** Because tends are formally *collected* by an agent ([agent.md](agent.md), *Tend output is collected*), `status.md` reports both how long since the last tend and how long since the last collection. The pair separates the two ways supervision fails: a stale tend age means the timer stopped, a stale collection age means nobody is exercising judgement. Either alone is ambiguous; together they say which half is missing.

**What a ten-minute interval actually costs** is slot utilization, not responsiveness: a worker that finishes at t=0 leaves its slot idle until the next tend, scaling as `interval/2 ÷ mean task duration`. An earlier version of this paragraph said that cost was largely gone because every pending task launches at once — which held only while the ceiling was core-derived and larger than most sweeps. It is a flat ten now ([scheduling.md](scheduling.md), *Launch everything, up to a ceiling*), so anything past ten tasks queues and the cost is back. It stays small for the hours-long tasks Steward is built for, and the levers are the interval itself and the ceiling; batching is not one, having been ruled out on its own terms.

**Two things stay simple, and they are why the claim's lifetime was the real question all along.**

- **The claim is short-lived** — held for the seconds a `tend` runs, not the hours a run lasts. That dissolves the wedging problem: no heartbeat protocol is needed, because a claim older than a generous `tend` timeout is unambiguously stale. This is conditional on the invariant that a tend spawns and reaps but never does long work itself (see *A scan is a detached process, not part of a tend*) — the moment anything unbounded runs inline, the short claim and everything it buys are gone. It is also what makes a timer and an agent racing each other a non-event: two callers of a pure function, serialized by a lock held for seconds.
- **The failure mode is benign.** If nothing tends the run, workers finish what they are doing, their logs land, and the run stops progressing. It pauses; it does not break, and the next `tend` from anyone resumes it.

Two requirements the design must honour regardless of driver:

- **Idempotence and claim discipline are non-negotiable.** Any driver may tend late, not at all, twice, or be interrupted mid-`tend`. A pure function plus the run claim plus the `intent`-before-spawn entry covers this — a repeated `tend` is a no-op, an interrupted one is reconciled by the next — but it means those pieces are load-bearing rather than defensive.
- **The output must be compact and structured.** Agent context is the scarce resource, so a `tend` must *summarize* rather than dump log headers. That is a real API constraint on `steward tend` ([agent.md](agent.md), *The tend summary*), and it makes the interval an economic choice as well as a utilization one.

**A nice unification falls out.** The `summary` a tend prints *is* the status update. An agent reads it inline and decides; a human sees it on stdout; a timer-driven tend leaves it in `status.md` and the journal for whoever arrives next. One artifact, three consumers — rather than a status file invented separately for the absent reader.

### 8.4 `status` and `tend` are one function, two dispositions

The unification above invites collapsing the two verbs into one — if a tend reports status anyway, why also have `steward status`? The pull is real, and the resolution is better than either single verb, because `reconcile` already returns *both* halves:

| verb | actions | summary |
|---|---|---|
| `steward status` | computed, **discarded** | printed |
| `steward tend` | computed, **executed** | printed |

`status` is literally `tend --dry-run` — same code path, same core, differing only in whether the actions are carried out. They cannot drift or disagree, which is the property that made merging them attractive in the first place.

Note what this does *not* mean: `status` is not the cheap one. It computes the same actions and performs the same reads; only the side effects are withheld. The read cost has to be attacked on its own terms, and it can be, because **completed `.eval` files are immutable** — a log never changes once it lands, so a directory scan is naturally incremental and a repeat scan costs almost nothing however many logs have accumulated. What is genuinely live (which workers are alive, per-sample progress) comes from the in-flight record and the control endpoints and is O(running workers), not O(logs). That is the requirement that lets a view refresh on `status` and lets a human ask "how is it going" as often as they like.

Keeping the names distinct matters for one reason: **`status` must stay read-only, because every convention in the ecosystem promises that it is.** `git status`, `systemctl status`, `docker ps` — a human who types `steward status` to satisfy their curiosity about an overnight sweep must not thereby launch eight workers. Surprise as a side effect of looking is the one thing a runner of expensive jobs cannot afford. There is also a plain mechanical need for a cheap read: the TUI refreshes continuously and must not reconcile at that rate.

Splitting this way makes `status` *more* useful than a state dump, because computing the actions and then discarding them turns it into a **preview of the next tend**: "6 tasks: 3 complete, 2 running, 1 errored — the next tend would launch 2 workers and flag `swe_bench@astropy` for adjudication." That is what both a human and an agent actually want to see, and it is the natural thing to show before authorizing an interval. It should also report the claim rather than take it ("a tend has been running since 14:02, pid 8814"), since holding a writer's lock is exactly what a read must not do.

**Why `tend` and not `tick`, `step`, or `advance`.** Those three all assert that the run only moves when the verb is called, and that is false: workers execute continuously between calls, so what a late call delays is the *scheduling of new work*, not the progress of work already running. The name has to agree with the benign-failure property above — miss a call and the run keeps going, it does not stall — and `tend` is the only candidate that does. `reconcile` is accurate but demands prior exposure to control loops to mean anything.

The name also has an unusual primary audience. This verb is not typed by a human at a prompt; it is scheduled by a coding agent working from a runbook, so CLI convention matters less here than what the word teaches the *agent*. `tend` primes "check on it, do what needs doing, leave" — the intended behaviour. `step` primes "drive the machine", which would encourage over-calling and make a missed call look like a stall. `status`, by contrast, *is* typed by a curious human, which is exactly why it keeps the conventional read-only name.

In prose, treat it as a noun-adjunct compound (`tend interval`, `tend cadence`, like `poll interval`) rather than a possessive.

**What the pair looks like in use.** A human asks the agent how the run is going; the agent calls `status`, which reads without touching anything and reports both the state and what the next tend would do — so the human is *offered* the action rather than having it happen behind their question. The agent adds the one thing no `status` call can: having tended every ten minutes, it holds a time series rather than a snapshot, and "the error rate was fine until 15:40, then three samples hit the same provider timeout" is an answer only the driver can give. That history accumulates for free as a side effect of being the thing that drives the loop.

### 8.5 Supervising workers

Workers run with Inspect's control server enabled, so each has a live HTTP endpoint over an AF_UNIX socket. That channel is what makes Steward a *steward* rather than a batch launcher: it can query a running eval's state and adjust its runtime behavior without restarting it.

Steward finds a worker's endpoint by pid via the discovery directory, correlated to a task through the in-flight record.

The endpoints most relevant to this document are the ones in-flight requeue is built on — `GET /evals/{id}/samples`, `GET /evals/{id}/sample`, and `POST /evals/{id}/sample/requeue` — plus the runtime-tuning directives (`POST /config` for per-sample limits, the pause/resume latches at process, task, and model scope). Model-scoped pause is worth noting alongside requeue: when the classification is "this provider is down", pausing the model is the correct response and requeueing individual samples is not.

#### 8.5.1 The channel changes how work runs, never what work exists

Worth stating as a boundary rather than discovering it as a gap. Across the whole route surface there is nothing that adds a sample: reads (`/tasks`, `/evals/{id}/samples`, `/sample`, `/events`, `/messages`, `/config`), concurrency and routing knobs (`PATCH /config`, `PATCH /tasks/{id}/config`), pause/resume latches at three scopes, and sample-lifecycle operations — `cancel` and `requeue` — that act on samples the task *already has*. A task's sample set is fixed when it starts, from dataset × epochs.

So **epochs cannot be raised on a running eval**, and should not be: epochs is semantic, and the channel is deliberately operational — the same line the selection overrides draw when they refuse anything that changes what an eval means. Extending a project by epochs is handled by convergence instead, and needs no new mechanism at all ([workflow.md](workflow.md), *A project, not a run*): the identifier is unchanged, so when the running worker's log lands it simply reads as incomplete, and the next tend respawns with `resume` to add the missing epochs while reusing the ones already done.

**Letting the running worker finish is unambiguously right here**, which is not true of the superseded case it resembles. Its output is epoch 1, and epoch 1 is exactly what the extension will reuse — so preempting it discards samples that were going to count, and buys nothing, since two workers cannot share a task (one task means one log). The cost is latency: a task six hours from finishing starts its new epochs in six hours. That is the eventual-consistency the convergence model trades for, and the alternative is strictly worse rather than merely different.

**One hazard in the other direction, worth verifying rather than assuming.** `PATCH /tasks/{id}/config` accepts `time_limit`, `token_limit`, and `message_limit` — and all three *are* in `task_identifier`. If a patched value reaches the log's `eval.config`, then `task_identifier(EvalLog)` no longer matches the manifest entry that scheduled it, and the log correlates to nothing. The override machinery it routes through suggests these layer at runtime rather than being written back, but that is an inference. Until it is checked, Steward should treat those three as not-to-be-patched; the concurrency knobs it actually wants for tuning are unaffected either way.

## 9. When the substrate fails

Everything above assumes the machine works. Three ways it stops, all of which a multi-day run meets eventually.

**Credentials for the log store expire mid-run.** This is the one most likely to be missed, because the analogous problem is already solved next door: Hawk refreshes model tokens for the duration of a run, and *nothing refreshes the log store's credentials*. A sweep that runs for three days on a session token issued for twelve hours loses the ability to write its own results partway through, having done all the work.

**The disk fills**, which takes out `.eval` writes, sandbox images, `.steward/`, and Steward's own logging together.

**`log_dir` becomes unwritable** for any other reason — a revoked policy, a deleted bucket, a mount that went away.

### 9.1 The hazard is that a substrate failure wears the costume of an eval failure

`fail_on_error=False` absorbs everything sample-shaped, so a log store that stops accepting writes surfaces as **a wave of errored samples** rather than as an infrastructure alarm. The classing is honest as far as it goes — the exception type will say `OSError` or an S3 error, and [workflow.md](workflow.md)'s class key puts them all in one anomaly — but the ordinary *response* to a wave of errored samples is a re-run, and re-running into a store that is still broken burns the work twice.

This is the same shape as the provider outage that *Considered and declined: pausing a failing model* worked through, and it gets the same answer for the same reason: no mechanical response, because by the time a tend sees the pattern the damage is done, and the correct action depends on a fact Steward cannot check. What it gets instead is a **runbook rule**: a class whose exception is a storage or filesystem error is a *substrate* class, and no re-run is proposed for one until the substrate has been verified by hand. Re-running is not wrong here, it is merely premature, and the ordering is what matters.

### 9.2 Detection is free; recovery is not Steward's

No polling is needed. Steward writes to the workspace and reads the log directory on every tend, so a failing substrate is *observed* rather than watched for — and a tend that cannot write is exactly the event `steward.log` exists for ([workflow.md](workflow.md), *`steward.log`*).

What Steward does about it is narrow, and deliberately so. It **stops scheduling** — spawning more workers into a directory that cannot be written multiplies the loss — **notifies with kind `stopped`**, and leaves running workers alone, since a worker that still holds its own handle may complete normally and one that does not will error into the ordinary path. It does not retry, rotate credentials, or fail over: those need authority and knowledge Steward has neither of.

One honest gap follows from the disk case. A full disk fails the `steward.log` write too, so the condition most in need of a record is the one least able to leave one. That is why the escalation goes out through the notification channel rather than relying on a file, and why "the files stopped changing" remains the outermost signal that something is wrong.

## 10. Clocks

Stated once, because it is the kind of thing that is decided implicitly and inconsistently otherwise.

**Every instant Steward records is UTC, ISO-8601, with an explicit offset.** Journal events, `steward.log` lines, the claim, status timestamps, signoff. Never local time — a workspace is read on a different machine than it was written on often enough that a naive local timestamp is a latent bug, and it costs nothing to be right from the start.

**Durations that gate behaviour come from a monotonic source where one is available**, because wall clocks jump: NTP corrects them, VMs resume with them stale, and a suspended laptop resumes hours later. Tend intervals and in-process timing are monotonic.

**Claim staleness is the exception, and it is safe by accident rather than by design.** A claim's age is its file mtime against the current wall clock, and no monotonic reading of another process's file exists — so a clock jump can misjudge it. Both directions are survivable, which is worth checking rather than assuming: a jump that makes a live claim look stale causes a second tend, which idempotence and the `intent` record already make a no-op; a jump that makes a stale claim look fresh delays the next tend until the timeout passes. Neither corrupts anything, and a generous timeout absorbs any plausible correction.

**Never compare a local instant against a remote store's.** Object mtimes in S3 come from the bucket's clock, not the runner's, and the two are not related. Freshness of anything in `log_dir` is judged by its *content* — the log's own recorded timestamps, its status — never by subtracting an object mtime from local `now`. This is the one clock rule that is a correctness constraint rather than hygiene.

Git's commit metadata stays what [workflow.md](workflow.md) already calls it: a corroborating record of when a decision was committed, not a source Steward reads.

## 11. Topology: what must be co-located, and what must not

Everything above describes one machine, because that is the case the design was written against. Three real deployments exist and they differ in ways that matter, so it is worth extracting the constraint that generates them rather than describing each.

### 11.1 One constraint, and it is narrower than it looks

Three things Steward depends on are **machine-local by construction**:

- **The run claim** is a file in the workspace, and its staleness rule is a local clock against a local mtime.
- **Control discovery** is a directory of files carrying pids and process start times, and a liveness check against a pid is meaningful only on the host that owns it.
- **The in-flight record** names pids on the host that spawned them.

`log_dir`, by contrast, is frequently **not** local — S3 is the common case, and *Syncing the workspace out* exists precisely because the log store may be the only thing a remote observer can reach.

So the constraint is:

> **`steward tend` must execute on the host running the workers.** The *agent* may be anywhere it can execute commands there.

That distinction is the whole of the flexibility. An agent on a laptop driving a rented box over ssh satisfies it; an agent that can only read the S3 bucket does not. Distributing workers across hosts is a different and much larger question ([open question 4](#13-open-questions)) — it needs worker discovery and a real lease with a fencing token, and nothing here assumes it.

### 11.2 Three deployments

| | workers | `tend` runs | judgement | how a person is reached |
|---|---|---|---|---|
| **workstation** | local | local agent | same agent | the conversation |
| **remote runner** | a rented or internal box, often no git and no inbound network | agent's shell on that box (a harness there, or ssh from elsewhere) | same agent | notifications out, workspace synced to a bucket; replying needs a session on the box |
| **Hawk pod** | in the pod | an **in-pod timer** | an external agent over the relay | notifications out; commands in over the relay ([hawk.md](hawk.md), *The relay surface*) |

The middle row is the one the S3 sync was designed for and the one the documents otherwise draw least. Two things about it are worth stating because they are easy to assume away: the agent needs **model access from that box**, which an air-gapped runner must provision deliberately; and the human's reply path is a session on a machine they may not normally use, which is the practical content of [workflow.md](workflow.md) open question 2.

### 11.3 Driving and judging are separate roles that usually coincide

[hawk.md](hawk.md) settles the pod case by splitting them — an in-pod timer runs the mechanical tend while an external agent supplies judgement, both calling the same pure `reconcile`. That is not a Hawk peculiarity. It is the design's own mechanical/judgement line applied to the driver, and stating it generally resolves an apparent contradiction with [agent.md](agent.md)'s *the cadence is a dependency*:

- **Driving is mechanical** — reconcile, spawn, reap, sync — so anything with a clock can do it.
- **Judging is not**, so only an agent can, and only while one is in session.

Where a timer exists, the mechanical floor is covered and an absent agent degrades *judgement alone*: work keeps converging, anomalies accumulate unruled, scans bank unprobed. Where no timer exists — the workstation and runner rows — the agent is both roles and its absence stops everything, which is why arming its cadence is a launch step rather than a habit.

The two never conflict, and the reason is already built: the claim is held for the seconds a tend takes rather than the hours a run lasts, so an external `steward tend` racing an internal timer is exactly the case the short claim was designed for.

### 11.4 The supervisor spends from the budget it is tuning

An agent is a model client. In the runner and pod topologies its calls usually go through the same proxy and the same account as the eval — which makes it an **N+1th consumer that no part of the resource design counts.** [scheduling.md](scheduling.md) says Steward owns both factors of total concurrency, and that is true of the eval and false of the fleet, because the supervisor is also in the bucket.

Two consequences, and the second is the one that bites:

- The agent's own requests contribute, marginally, to the rate-limit signal it reads as evidence of headroom. The magnitude is small — a handful of requests per tend against an eval running hundreds of concurrent samples — but the loop is real and worth naming rather than discovering.
- **Supervision degrades exactly when the run is most loaded.** An eval that saturates the account rate-limits the agent too, so tends get slow or fail at the moment a tend most needs to land, and an escalation waits behind the very congestion it exists to report.

The fix is not machinery. **A separate key for the supervisor** removes it entirely and is the right answer where an account can be provisioned that way. Failing that, the envelope is already the place this belongs: [workflow.md](workflow.md) makes the concurrency ceiling a policy decision, and a ceiling set at the account's true limit leaves the supervisor nothing. Leave headroom, and say that is what it is for.

## 12. Changes required in inspect_ai

1. **Capture mode** — `INSPECT_EVAL_SET_CAPTURE`. *Landed.*
2. **Selection mode** — `INSPECT_EVAL_SET_SELECTION`, including the resume path and the mutual exclusion with capture. *Landed* (`_eval/eval_set_selection.py`, plus the branch in `eval_set()`).
3. **Error-handling overrides** — *landed, as part of worker mode.* `fail_on_error=False` and task-retry-off are applied by selection mode itself, and the definition's requested values are recorded in the capture manifest's `options`. This deliberately avoids routing them through the overrides channel: they are not tunable policy, they are what selection mode *means*.

4. **Operational overrides in the selection document** — *partly landed.* A selection may carry `log_dir` and `max_samples`, overriding what the definition passed (omitting either keeps the definition's value). This deliberately avoided a third env var: overrides only matter in selection mode, and the selection document is already a versioned channel the worker reads.

   **They move into a container at version 3**, `EvalSetSelectionOverrides`, alongside items 8 and 9 — which is the bump those two already require, so the restructure rides a release that was happening anyway. The container is `extra="forbid"` like its parent, so typo safety is unchanged. It is a **clean break rather than a migration**: nothing here is shipped, Steward is the sole writer of a selection document, and no deployment reads version 1 or 2 — so the two fields move rather than being kept as accepted legacy, with no dual shape and no version floor. The honest accounting is that a container saves no version bumps (`extra="forbid"` already refuses an unknown key; the bump buys *"upgrade inspect-ai"* instead of *"unknown field"*, which is wanted regardless). What it fixes is that `version`, `eval_set_id`, and `tasks` are the protocol and the rest are knobs, and at version 3 there are four of them.

   An environment variable could not have served here. `INSPECT_LOG_DIR` and its siblings supply *defaults*, and `eval_set()` declares `log_dir` with no default — so every definition passes it explicitly and a default can never win. That is why `--smoke`'s log-directory half was blocked for script definitions: Flow could be redirected with `--log-dir`, a raw script had no way in at all.

   No override participates in `task_identifier()`, so redirecting a worker cannot desynchronize it from the capture manifest. That is the operative test, and the rule it enforces is stated in *The selection protocol*: an override may change how a worker is **operated** — where its output goes, how fast it runs, how much of its dataset it runs, what surfaces it exposes — never what is evaluated.

   Still unreached, and judged not worth a channel of their own: `log_level` (CLI-only) and `display` (`INSPECT_DISPLAY` is read generally, so it already works). `ctl_server` needs nothing — it defaults to enabled. `max_tasks` is moot in worker mode, where a worker runs one task. Two overrides *are* wanted and did not land here — the dataset `limit` a smoke run needs (item 8) and `max_sandboxes` (item 9). `max_samples` is neither of them. `acp_server` is wanted too and is deliberately **not** an override: it is forced by worker mode, for the reason item 11 gives.

   Hard-coding the error-handling options keeps this surface purely *operational*: nothing that can be overridden changes what an eval means. Had `fail_on_error` gone here, the channel would have needed two tiers to keep semantic overrides visible.

5. **Automatic early pruning** (Layer 2 in configuration.md) — the `@task` registry wrapper returning placeholders for unselected tasks. *Not yet*, and it is what makes a worker's startup cost proportional to its own task rather than to the whole eval set. It has a Steward half, which is why it is a plan step rather than a pure wait: the wrapper matches on `(name, args_hash)`, so the selection document has to carry those as fields, and `extra="forbid"` means Steward cannot send them until the schema version that introduces them ([plan.md](plan.md) step 15).

6. **Public eval-set directory operations** (see *Sharing the directory operations with `eval_set()`*) — a documented public surface that both `eval_set()` and external runners call, so the preparation/cleanup protocol has one implementation rather than two that drift. Also rationalizes a surface that is already inconsistently half-public. Most of the cleanup band needs no change beyond export; the preparation half needs `validate_eval_set_prerequisites` and `write_eval_set_info` refactored to take identifiers and plain `EvalSetTask` rows instead of live `ResolvedTask`s. Low risk, and it retires a whole category of future divergence.

   Deliberately *not* extended to the capture manifest and selection document: those cross a process boundary as data, so they stay private and versioned. See the two-contracts note in that section.

   **One addition rather than a fork:** `cleanup_older_eval_logs` should grow an `archive_dir` that moves superseded logs instead of deleting them. Steward never deletes an eval log ([workflow.md](workflow.md), *Steward never destroys a result*), so without this it must reimplement the one operation this item exists to stop it reimplementing. It is not Steward-specific either — an `eval_set()` user who retries a task and later wants to know what the failed attempt looked like has the same need, and today the log is simply gone.

   **A second addition, found while building the observation layer:** a batched header read that reports per-file failure rather than raising. `read_eval_log_headers_async` collects through `tg_collect`, so one unreadable file fails the whole directory — and an unreadable file is not exotic, it is what a worker's zip looks like for the moment between creation and its first journal entry. Any scheduled reader of a live log directory needs the degrading version; the viewer arguably does too. Steward reimplements the gather for this reason rather than because the function is private.

7. **Notification outside an eval** (see [workflow.md](workflow.md), *The gap: notifying from outside an eval*) — `inspect_ai.util.notify()` is a silent no-op unless an Apprise instance was installed by `eval_resolve_tasks`, so it does nothing when called from a Steward process. `build_apprise()` / `init_apprise()` already do exactly what is needed but are private. Exporting them (or a `notification_scope(config)` convenience) is the same public-surface move as item 6, and it is not Steward-specific — any script that runs evals and wants to be told when they finished hits it.

8. **A dataset `limit` override in the selection document** (see [workflow.md](workflow.md), *Smoke first*) — the one thing a smoke run still cannot express, and the correction to a claim both documents used to make. `max_samples` bounds how many of a task's samples run concurrently; `limit` bounds how many run at all, and only the first of the two arrived in schema version 2. It belongs in the same channel on the same grounds: `task_identifier` hashes a task's *execution* limits — message, token, turn, time, working, cost — but not its dataset slice, so truncating a worker's dataset cannot desynchronize it from the capture manifest. Mechanically it is one field inside the version-3 `overrides` container (item 4), which is the bump it and item 9 jointly require.

   The version bump is not ceremony here, and the reason is worth recording: an unknown *facet hint* ignored by an older inspect costs nothing, but an ignored `limit` means a worker asked for two samples runs five thousand. The declared-version gate is what turns that into a refusal on the writer's machine instead of a surprise in the one deployment still running an older inspect.

   Its sibling is **not** wanted: `time_limit` is in the identifier, so the smoke's wall-clock cap must stay a Steward-side deadline rather than an override. Passing one through would change every task's identity and break the matching the smoke depends on.

9. **A `max_sandboxes` override in the selection document** (see [scheduling.md](scheduling.md), *`max_sandboxes` — divided, but only where the host is the ceiling*) — the third operational override, on the same grounds as the first two: sandbox concurrency is process-global, absent from `task_identifier`, and unreachable by environment variable. It rides alongside item 8 in the version-3 `overrides` container.

   It is more urgent than its siblings, because the failure it prevents is not a slow run. Steward launches a poolful of workers, each independently resolving a sandbox limit that Docker computes as `2 × cores` — a *host* number applied per process. At the default ceiling of ten workers a 64-core box asks for 1,280 containers, and the ceiling is a knob a user is expected to raise. Patching after spawn does not help: by then the containers are open, and the ratchet only lowers a limit as in-flight samples drain.

10. **The capture manifest should record each task's sandbox type, and `max_sandboxes` alongside it** — without them Steward cannot compute the division item 9 delivers. Two fields, both small. Per task: the `SandboxEnvironmentSpec` type name, since tasks in one eval set may use different providers and only the host-bound ones share a budget. In `options`: `max_sandboxes`, which is not recorded today.

    A third field in the same family, `max_samples` in `options`, **has landed**. It was the one that bit without any of the rest: [scheduling.md](scheduling.md) has Steward write an explicit `max_samples` into every selection and yield to whatever the definition set, and it could not see what the definition set — so a definition asking for 60 silently got Steward's 40, with no read-side workaround, since a landed log records only the effective value. One line on each side. `max_sandboxes` is the identical change whenever item 9 is taken up.

    Only the *name* is needed, not a resolved number — Steward can call `registry_find_sandboxenv(type).default_concurrency()` itself, because the provider package is by construction installed in the same environment the workers run in. That keeps the elastic/host-bound distinction where Inspect already draws it (`default_concurrency() -> int | None`, `None` meaning no maximum) instead of duplicating a provider table in Steward.

    One adjacent bug worth fixing while in the area, though Steward can work around it: Docker's `default_concurrency()` calls `os.cpu_count()`, which reports host processors rather than a container's cgroup quota — the same over-count [scheduling.md](scheduling.md) has to avoid in its own worker ceiling.

11. **The capture manifest should record each task's epochs reducer.** One field, and it closes a hole Steward cannot close any other way. Completeness is the one judgement Steward makes that `eval_set()` also makes, and it makes it from the manifest: `samples × epochs` against the log's `total_samples`, which is exactly `log_samples_complete` with the manifest standing in for the `ResolvedTask`. But upstream's predicate has a second half — `epochs_changed` compares the requested reducers against `config.epochs_reducer` and re-runs when they differ — and `build_eval_set_capture` keeps only `task_epochs.epochs`, discarding the reducers. Reducers are held outside `task_identifier` by design, so nothing else catches it either.

    The consequence: changing `Epochs(3, "mean")` to `Epochs(3, "max")` leaves Steward reporting the task complete where `eval_set()` would re-score it. Narrow, but silent, and the sort of thing that is discovered as a wrong number in a paper rather than as an error.

The selection schema can grow the partial facets Layer 2 needs (`name`, `args_hash`, `model`, `sequence`), but **not for free** — an earlier draft of this paragraph said additive optional fields need no version bump, and what shipped decided otherwise. Both selection models are `extra="forbid"`, so an unknown field is refused rather than ignored, and every added field takes an `EVAL_SET_SELECTION_VERSION` bump plus a `_FIELD_MIN_VERSION` entry recording where it began. A document may not use a field newer than the version it declares.

That is the better trade and worth understanding rather than working around. Silent tolerance of unknown fields is what makes a typo (`"resuem"`) read as *no resume at all*, and makes an ignored `limit` run five thousand samples where two were asked for. Forbidding extras converts both into a failure on the writer's own machine. The cost is that the version number moves more often than a purely additive scheme would need, which is bookkeeping rather than friction.

Two further items came out of asking what a *detached* worker can and cannot do:

12. **`acp_server` on in worker mode**, the way `fail_on_error=False` and task-retry-off already are — one line where those two are applied. Not an override and not a default a definition can decline: a detached worker's human-input dispatcher falls **ACP → Textual panel → console**, and with no display and a closed stdin the last of those raises `EOFError` into the tool call. So without this, `approver: human` and `ask_user` neither park nor fail loudly — they land as errored samples in successful logs (*The parked worker*).

    It is a property of worker mode rather than of Steward. Any external runner spawning detached workers has the same dead chain, and only one of the three definition types Steward drives can set the flag itself today — Flow through `FlowOptions.acp_server`, while a raw `eval_set()` script has no channel at all (`INSPECT_EVAL_ACP_SERVER` is a click envvar on `inspect eval`, which a definition never goes through) and Hawk's is a single TCP port that N concurrent workers could not share anyway.

13. **Project the pending human interaction into the control channel's sample row** — and land it *with* item 12 or before it, never after. The state already exists on the eval primitive: `ActiveSample.pending_interaction` returns `"approval" | "question" | None` from two integer counters, and ACP's `inspect/list_samples` already surfaces it as `SampleListing.pending`. The control server's listing walks the same `active_samples()` and already carries an `activity` field classified `tool` → `model` → `retry_wait`; this is an approval/question branch at the front of that chain, with `detail` carrying the tool function. A few lines, on data the row builder already holds, in a function whose contract is *O(in-flight ops), never an event scan* — so a tend learns which workers are parked from the listing it already fetches, with no second channel and no per-sample query.

    **Without it a park is not merely invisible, it is camouflaged.** The stage's `ToolEvent(pending=True)` is created before its approval is awaited, so a sample parked for hours reports `activity: {type: "tool"}` — the same shape as a slow tool call. `InputEvent` and `ApprovalEvent` are both written only after the interaction concludes, so the transcript does not help either. Shipping item 12 alone would replace a loud errored sample with a silent stall, which is the wrong trade: the two are one change wearing two numbers.

## 13. Open questions

1. **Flow's pre-boundary work.** Everything Flow does before reaching `eval_set()` is out of worker mode's reach, and every flow worker repeats all of it. Verified against a four-task spec with four concurrent workers: the run works correctly (four logs, correct identifiers, no eval-set metadata written), but each worker independently

   - scans the log directory (`find_existing_logs`) — the only cost that *grows* with the run, at O(logs) header reads;
   - resolves the spec and writes `flow.yaml` into the log directory;
   - shells out to `uv pip freeze` and writes a ~200KB `flow-requirements.txt`.

   Measured overhead was ~1.1s per worker over an equivalent plain-`eval_set()` definition on a near-empty log directory — modest per worker, N× in aggregate, and growing.

   The last two are also **truncate-in-place writes to shared paths** (`_config/write.py`, `_launcher/freeze.py`). Content is identical across workers, so divergence is not the risk; a concurrent *reader* (e.g. `flow list`) catching a half-written file is. It held up under test, but that is one trial of an unsynchronized write, not a guarantee.

   **Two thirds of it is fixed, without a Flow change.** All three items key off `spec.log_dir`, and `flow run` takes `--log-dir` — so a worker is given a scratch directory of its own for the pre-boundary half while the selection carries the run's (see *The selection protocol*). The two writes then land in a directory no other worker knows about, and the scan finds it empty. Safe because the scan's result never reaches `eval_set()`: `_runner/run.py` uses `logs_result` only for the pre- and post-run display and for the store. Re-measured, one flow worker against a plain-`eval_set()` baseline of 3.0s:

   | | empty log dir | 150 logs |
   |---|---|---|
   | shared log directory (before) | 4.36s | 4.79s |
   | per-worker scratch (after) | 4.14s | 4.21s |

   So the growing term is gone — the old path was paying ~2.4ms per log in the directory, which is ~12s per worker at the 5,000 Hawk's equivalent scan caps at — and the two shared-path writes are gone with it.

   **What is left is the freeze, and it is the whole ~1.1s.** The original attribution spread that figure across spec resolution, `flow.yaml`, and the scan; measured on an empty directory it is almost entirely `write_flow_requirements`, which shells out twice (`uv pip freeze`, then `uv pip compile --generate-hashes`, the second reaching the index). It cannot be skipped from outside — its only early return is `if dry_run or not spec.log_dir`, and an empty `log_dir` makes the run raise a few lines later — so it can only be redirected, which is what the scratch directory does.

   The clean fix for that remainder is still on Flow's side and is still small: a way to skip the pre-boundary artifacts when an external runner owns the run. **A second, smaller ask came out of the redirection itself.** `flow run` records its log directory as a global `last_log_dir` (`_launcher/launch.py`, unconditional), so pointing Flow at scratch leaves that pointer aimed at a disposable directory and a later bare `flow run --resume` resumes nothing. Steward accepts this rather than writing another tool's user-global file, and the fix is the same shape as the first: a way for an external runner to say it owns the run, or simply a knob for the application-data directory. See [configuration.md](configuration.md), *Inspect Flow specs*.

   **This is not a Flow question.** Hawk has the same shape and a sharper version of it — its pre-boundary work includes `uv pip install` into the running interpreter on every invocation ([hawk.md](hawk.md), *Pre-boundary work that must not be per-worker*), and unlike Flow it exposes no `--log-dir` to redirect, so its equivalent scan cannot even be pointed away. Any frontend Steward drives will divide its startup into per-process work, which must repeat, and per-run work, which must not. The general ask — **a way for an external runner to declare that it owns the once-per-run work** — is one protocol question with one answer, and it is better asked once of the boundary than twice of two frontends. `INSPECT_EVAL_SET_SELECTION` is already the signal that an external runner is present; what is missing is any obligation on a frontend to notice it.

   **Redirection only works where the work produces files.** Where it produces *process state* — Hawk resolves secrets into `os.environ` and sets provider variables for gateway routing — there is nothing to point elsewhere, and the answer needs a second half: the frontend must be able to **report** what it resolved, so the runner can hand it to each worker. `DefinitionCommand.env` is already shaped to carry that. This is the part of the ask that no amount of cleverness on Steward's side reaches.

2. **What replaces flow's store.** *Resolved: nothing replaces it, because the mechanism keeps working — but Steward operates it, not the worker* — see *Steward owns both halves, because the store is not really Flow's*. `--store none` disabled two halves to fix one, and the repair is **not** to hand the write half back to workers: an `evalset` or `hawk` worker has no Flow code in it to index anything, so a store fed by workers is empty for two thirds of the projects that would benefit. Workers therefore run with the store off, Steward reads once at launch, and Steward writes at signoff.

   **Only the first of those three is built.** The flow adapter passes `--no-store-read --no-store-write`; both of Steward's halves are step 29, in M4. Until then the store is **inert for Steward runs** — nothing indexes and nothing is reused — where before it was flow workers appending unattested rows on flow's `write=True` default. That is the correct interim state rather than a regression, for the reason *Publication is an act of signoff* gives, and what it costs is narrow: reuse *across* log directories for flow definitions, since reconcile already declines to re-run a task whose log is in this run's directory.

   What remains open is narrower still and is recorded in the sections above: what filter policy governs reuse, and whether a log carrying accepted-as-errored samples is publishable at all.

3. **Worker startup cost without Layer 2 pruning.** Every worker currently constructs every task in the eval set, including datasets, to compute identifiers. For a large sweep this dominates, and it is paid in parallel across the pool rather than amortized. Per-worker memory therefore scales with the manifest rather than with the task, which is why scheduling.md treats Layer 2 as a **prerequisite for raising the ceiling on a large manifest** rather than an optimization. The flat ten-worker ceiling bounds the default exposure at ten copies of the manifest, which makes this smaller than it was under a cores-derived ceiling but does not remove it — the manifest is what scales. Batching was previously named as the mitigation; it is ruled out on its own terms. Verified still absent from `inspect_ai` main as of `0db4111e`.

4. **Cross-host runs.** The in-flight record carries a host per launch, and both control discovery and the run claim are per-machine, so the current design supervises a run from one host. Distributing requires two things Steward would own rather than consume: a discovery mechanism for workers, and a real lease on the log directory with a fencing token to replace the local claim. The lease is the harder half — `log_dir` may be S3, where atomic create-if-absent is not reliably available through the filesystem abstraction Inspect uses.

5. **Second `steward launch` against a live run.** *Resolved* — see [workflow.md](workflow.md), *One trigger, and one gate on it*. A second launch is neither an error nor an attach: it is the **amend** path. It re-captures the manifest, reports the delta against the log directory, and commits it as the new desired state, which the running convergence loop then pursues. Declining would have been refusing the most common legitimate reason to run the command twice. What survives of the original concern is the gate: a delta that would move logs out of `logs/` requires explicit acceptance, because a typo in a task arg is indistinguishable from a deliberate change.

6. **Torn reads of the directory manifests.** Steward is the only writer of `eval-set.json` and `logs.json`, but both writes are truncate-in-place, so a concurrent *reader* can still catch a partial file — and `eval-set.json` has a live reader in the viewer (`read_eval_set_info_async`, which validates with no error handling). Rewriting it as logs land makes the window recur throughout the run rather than once. Either Steward writes these atomically, or the writers in Inspect become atomic. The latter is better for everyone, since `eval_set()` has the same exposure today.

7. **Error classification.** *Resolved, and reduced first.* Nothing automatic turns on "was this failure transient?" any more (see *Two tiers, not three*), so what remained was **grouping** — and that is settled in [workflow.md](workflow.md), *Three levels: instance, class, proposal*. A class is the exception type plus its raising frame, recoverable from the traceback since `eval_error()` builds it with `format_traceback(exc_type, ...)`; message text stays out of the key because ids and hostnames split one cause into forty. Fine classes are then collapsed for the human by a proposal spanning several of them, with the ruling recorded per class. One upstream nicety, not a blocker: `EvalError` is three strings with no type field, and `eval_error()` receives `exc_type` and discards it after formatting — adding `type: str | None` would hand this straight over.

8. **Recovery authority.** *Resolved — see *Two tiers, not three* and *Considered and declined: pausing a failing model*.* Tier 1 is automatic at whatever `retry_on_error` the definition set; everything past it is a ruling, so there is no requeue budget to size and no automatic response to an error class. Pausing a failing model was the one candidate exception and it fails on timing: a sample dies within minutes of an outage, well inside a tend interval, so the response would arrive after the fleet it protects. The work it would have saved is better saved by Inspect's checkpointing, which survives host loss as well. `policy.md` may pre-authorize a class of re-run, which is a ruling made earlier rather than an exception.

9. **Steward-orchestrated scanning.** The shape is settled (see *How Steward would take the scan over*): a third boundary mode, `INSPECT_EVAL_SET_SCAN`, that executes the definition and scans the named logs as single writer. Unbuilt, and these remain open:
   - ~~**Cadence.**~~ *Resolved in [scheduling.md](scheduling.md), *Scanning is scheduled work*: drain eagerly. A scan takes no worker slot, so the many-short-processes objection costs little, and findings arrive last and re-open resolved runs — latency is the thing worth optimizing. One log per pass, batching only when scans fall behind, which is exactly when per-pass startup is being paid too often.*
   - **Sample reads.** The sketch reads whole logs; large transcripts make that expensive. Whether to stream samples, or scan straight from summaries plus a targeted transcript read, is unresolved.
   - ~~**Failure of the scan pass itself.**~~ *Resolved in [scheduling.md](scheduling.md): recorded and reaped like a task worker, surfaced as an anomaly, never automatically respawned. One-log passes make the record name the offending log, which separates "scanning is broken" from "this transcript breaks the scanner".*
   - **Scan errors vs sample errors.** `scan_finalize` leaves a scan resumable when scanners errored. That is a second adjudication queue, distinct from the errored-sample one, and it is not yet clear whether Steward should treat them alike.

   Until it exists, `options["scanners"]` in the manifest lets Steward refuse a scanning definition at enumeration time with a clear explanation rather than failing every worker.

10. **The tend interval.** *The question changed twice and is now much smaller.* It is no longer "who guarantees the cadence" — a timer does (*Drivers, one core*). Slot idle is back, since the worker ceiling is a flat ten rather than a core count and anything larger queues, but it is small for hours-long tasks and the interval is only one of the two levers on it ([scheduling.md](scheduling.md), *Launch everything, up to a ceiling*). What remains is a straightforward tradeoff with no measurements behind it: a shorter interval reaps dead workers sooner, spawns queued work sooner, and drains scans sooner; a longer one writes less to the journal and gives an arriving agent less to read. Ten minutes is still a guess. Note the context argument weakened considerably — most tends are now read by nobody at the time they run, so their cost is storage and an arriving agent's catch-up rather than sixty full reads a night.

11. **Eval-set-scoped hooks fire nowhere.** Selection mode returns at `evalset.py:648`; `emit_eval_set_start` and `emit_eval_set_end` are at `:899` and `:937`. A worker never reaches them, correctly — it is not running an eval set. But Steward does not emit them either, so any hook registered at eval-set scope is silently dropped, while the same hook's run- and sample-scoped handlers keep firing. Found concretely in Hawk, where it costs two metrics and, more seriously, arms nothing for a stuck-eval watchdog ([hawk.md](hawk.md)); it applies to any platform that registers at that scope. Either Steward emits the pair around the run it owns — the honest reading, since it *is* the eval set — or Inspect grows a way for an external runner to, which is the same shape as the shared directory operations above. Whichever, the current state is a silent gap rather than a decision.

12. **What "resolved" means for an eval set.** *Answered in [workflow.md](workflow.md).* A run is **resolved** when no anomaly is open — anomaly state being a fold over `journal.jsonl` — and separately **signed off** when a person has attested to the results. Scan findings arrive last and can re-open a run that looked finished, which is why signoff follows the scan rather than the tasks. `EvalLog.invalidated` and per-sample `invalidation` records carry the provenance half. What remains open there is the anomaly *identity* scheme, not the definition.

13. **How much of a parked request to render.** A queue entry saying *worker 7 is waiting on an approval* is nearly useless without saying what for, and the answer — the tool call, or the `ask_user` prompt — is **model-generated text**. Inspect already draws this line rather than assuming it away: the control channel withholds error messages and limit reasons behind `content=true` precisely because they are agent-influenced free text, and Steward's summaries are read by an agent that then acts. The tool *function name* is safely structural and is what item 13 puts in `activity.detail`; whether `status.md` and the tend summary should also carry arguments or prompt text, quoted and attributed, is unsettled (*The parked worker*). Nothing blocks on it — a name plus an attach command is already actionable.
