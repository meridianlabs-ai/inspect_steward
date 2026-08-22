# Execution

**Status: draft for discussion**

How Steward runs the tasks that [configuration.md](configuration.md) enumerates. That document ends at the manifest — the static list of resolved tasks in an eval set. This one starts there: how those tasks become processes, where their logs go, who retries what, and how Steward keeps track of work it did not stay attached to.

## Requirements

1. **Steward owns orchestration.** Scheduling, retries, scaling, and supervision are Steward's, not `eval_set()`'s. A definition's `eval_set()` call is a *boundary*, not a runner.

2. **Standard Inspect tooling must work, live.** `inspect view`, `samples_df`, `evals_df`, and `inspect log` must work against a running eval set with no Steward-specific adaptation and no post-processing step. A user watching a run in the viewer should see what they would see if they had run `eval_set()` by hand.

3. **Workers execute the definition.** Every process that runs evaluation work executes the whole definition program, so `set_model_info()`, dynamically constructed `Model` objects, and environment setup are in place — see requirement 3 in [configuration.md](configuration.md).

4. **Survive Steward restarting.** A run outlives the process that started it. Steward must be able to exit, be killed, or be upgraded, and on return reconstruct what is in flight without killing or double-running anything.

## The problem with running `eval_set()` per worker

The obvious implementation — spawn N workers, each running the definition with a single-task selection, all pointed at one log directory — does not work, and the reason shapes everything below.

`eval_set()` is *itself* an orchestrator. On every pass it scans the whole log directory, decides which logs to reuse, writes `.eval-set-id` / `eval-set.json` / `logs.json`, and prunes "older" logs. Running N of them over one directory means N orchestrators over shared state, and there is no locking, no atomic manifest write, and no claim protocol anywhere in that machinery. Concretely:

- Worker B lists the directory, sees worker A's in-flight log with `status == "started"`, classifies it as incomplete, and re-runs A's task.
- `cleanup_older_eval_logs` groups logs by `task_id` and deletes the losers by mtime — so one worker deletes another's good log.
- `eval-set.json` and `logs.json` are truncate-in-place writes that can tear, and the viewer's reader for `eval-set.json` validates it with no error handling.
- Each worker does roughly 2N log-header reads per pass, so directory scanning costs O(workers × logs).

Per-task log directories were the first answer (and are what configuration.md originally specified). They avoid the contention, but only by moving it: each worker still runs a full orchestrator, just over its own directory, and Steward inherits the job of stitching a tree of `eval-set.json` files into one view. Inspect's discovery already recurses by default, so nesting is *mostly* invisible in the viewer's default task listing — but drilling into a log loses its eval-set context, because `eval-set.json` is only read from the log's own directory.

**The resolution is to remove the competing orchestrator, not to give each one its own sandbox.** Steward *is* the eval-set runner. A worker should run a single `eval()`, which is the part of Inspect that actually runs a task, and none of the part that decides *which* tasks to run.

## Worker model

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

## The selection protocol

Selection is the execution counterpart to capture. Both are environment-variable interceptions at the `eval_set()` boundary, and they are mutually exclusive.

| Variable | Meaning |
|---|---|
| `INSPECT_EVAL_SET_CAPTURE` | Path to write a manifest. `eval_set()` resolves tasks, writes the manifest, and exits the process without running anything. |
| `INSPECT_EVAL_SET_SELECTION` | Path to a selection document. `eval_set()` resolves tasks, runs only the selected ones through `eval()`, and skips all eval-set orchestration. |

The selection document (`inspect_ai._eval.eval_set_selection`, schema version 1):

```jsonc
{
  "version": 1,
  "eval_set_id": "swe-sweep-2026-08",   // Steward-assigned; stamped into every log
  "tasks": [
    {
      "identifier": "<task_identifier from the manifest>",
      "resume": "logs/2026-08-19T…_mbpp_abc.eval"   // optional prior log to resume
    }
  ]
}
```

`tasks` is a list rather than a single entry so a worker can host several tasks when that is cheaper than several processes (a definition whose import cost dominates, or a batch of very short tasks). One task per worker is the default.

An identifier that matches no resolved task is a hard error naming the likely cause — the definition changed since it was enumerated. An identifier that matches *more* than one resolved task is also an error: outside selection mode, `validate_eval_set_prerequisites` enforces identifier uniqueness across the eval set, and worker mode skips that check, so it makes the same guarantee locally for the tasks it is asked to run.

### What worker mode deliberately skips

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

Two things are *not* skipped. The worker still creates `log_dir` (`mkdir(exist_ok=True)` is idempotent and concurrency-safe), and it still runs the full `eval()` path, so every eval-set-level kwarg the definition set — epochs, limits, solver, generate config, tags, metadata, `retry_on_error` — applies exactly as it would have.

### Scanning

Scanners are **rejected** in worker mode rather than silently dropped, and the reason is worth stating precisely: a scan directory reproduces every hazard that made a shared eval-set log directory unsafe, one level down.

One scan directory (`<log_dir>/scans/scan_id=<scan_id>`) serves a whole eval set, and its bookkeeping assumes a single writer:

- **Create-or-attach is a TOCTOU.** `scan_init` does `if exists(scan_dir): attach() else: init()`, and `init()` resets. N workers starting together all see it absent, and the last one to `init` wipes the others.
- **Finalize prunes another worker's rows.** `_cleanup_orphan_scan_rows` reads sample uuids from *all* logs in the log directory and filters each scanner's parquet down to just those uuids. A worker finalizing while a sibling's log is still `started` computes a `live_tids` set that omits the sibling's samples — and rewrites the shared parquet without the sibling's rows. This is `cleanup_older_eval_logs` again, wearing a different hat.
- **`complete` is eval-set-wide but computed per worker.** `scan_finalize` derives `complete` from a directory-wide status and, when true, cleans up the buffer dir. The first worker to finish marks the whole scan clean, and `scan_already_clean` then makes later passes skip transcripts nobody ever scanned.
- **`_summary.json` is read-modify-write**, so concurrent workers clobber each other's counters.

Worker mode fixed these for logs by *removing* the orchestrator, not by adding locks. Scanning still has its orchestrator inside each worker, so the same fix has to be applied at the same level: **the runner scans, over the log directory, as the single writer.** That does not have to mean waiting for the run to finish — Steward can scan completed logs incrementally as they land, the same way it periodically rewrites `logs.json`, which recovers most of the liveness of in-worker scanning with one writer instead of N.

Because rejection happens at execution, capture records `options["scanners"]` so a runner learns at *enumeration* time that a definition scans, rather than discovering it when every worker fails.

### How Steward would take the scan over

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
                sample, scanner, scanned, sample_semaphore,
                scan_id=eval_set_id, eval_id=eval_log.eval.eval_id,
                log_location=location, model=eval_log.eval.model,
                eval_spec=eval_log.eval,
            )
```

`scan_context` is self-contained (`scan_init` on enter, `scan_finalize` on exit), and `resume_scan_previous_sample` already encapsulates the skip-if-already-recorded check that makes re-scanning cheap. No task resolution, no dataset matching, no eval loop.

The protocol would mirror the other two: `INSPECT_EVAL_SET_SCAN` naming `{version, scan_id, logs: [...]}`.

**Why not reuse selection mode with `resume` instead.** It is tempting, because `eval_set()` already scans landed logs this way — `_resume_scan_tasks` builds `PreviousTask`s for *successful* logs purely so the sample-reuse path dispatches scans for transcripts whose row never landed. But that route drags in task resolution and dataset matching, and carries a failure mode a scan pass must not have: a sample the reuse path *cannot* match gets executed, silently turning a scan into an eval run with model calls. Scanning a log should never run a sample. A dedicated mode over log locations avoids the question entirely.

**Properties that follow.** Exactly one scan process runs at a time — not by convention but because spawning is serialized by the run claim and recorded, so a tend checks for a live scan before starting one and a restarted Steward adopts an in-flight scan rather than starting a second (see *What enforces single-writer*). All four hazards above are then gone by construction. Passes are incremental: `scan_init` attaches to an existing scan dir and `_invalidate_finalized_flag` flips `complete` back to `False` — the same path `eval_set()` resume uses. Passes are idempotent: a transcript with a row for every scanner is skipped.

### A scan is a detached process, not part of a tend

A scan reads whole transcripts and, for model-graded scanners, makes model calls — so a pass over a large eval set can run for hours. That makes it the same kind of thing as a task worker, and it must be spawned the same way: **detached, recorded, and reaped by a later tend.** A tend that ran a scan inline would hold the run claim for the scan's whole duration, which would destroy the property the driver model is built on — that a claim older than a generous tend timeout is unambiguously stale, so no heartbeat protocol is needed. Blocking on long work inside a tend brings the wedged-supervisor problem straight back.

This generalizes past scanning, and scanning is just the case that forces it to be said: **a tend spawns and reaps; it never does long work itself.** Task workers, scan passes, adjudication re-runs, and end-of-run bundling all obey it. Anything whose duration is unbounded is a detached child whose completion some later tend observes.

Two consequences specific to scans:

- **The claim protects the decision, not the duration.** A scan process outlives the tend that spawned it, so the claim is not what keeps the scan directory single-writer — the in-flight record plus a liveness check at spawn time is, exactly as it is for not double-spawning a task. The claim's job is only to serialize the *decision*.
- **Scanning is a drain, not a pass.** Because a scan can outlast the tend interval, Steward cannot start one per tend. The workable model is a queue of unscanned logs drained by one process at a time, each run taking whatever has accumulated since the last. That self-regulates: slow scans simply batch more logs, and coverage lags the run rather than stalling it.

It also means a run is not finished when its tasks are. The final scan is a long job that begins after cleanup and adjudication settle (see the ordering constraint above), so `status` needs *complete, scanning* as a distinct state, and end-of-run finalization is itself a multi-tend affair rather than a blocking step.

**The one real ordering constraint.** `scan_finalize` runs `_cleanup_orphan_scan_rows`, which prunes rows whose uuid appears in no current log. So Steward's *final* pass must run **after** log cleanup and after adjudication re-runs have settled — otherwise rows belonging to superseded attempts survive, and rows for re-run samples are keyed to uuids that no longer exist. Mid-run passes are unaffected: in-flight samples have no rows yet, so there is nothing for the pruner to get wrong.

## Log directory

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

### `eval-set.json` must be written incrementally

This is the one entry that cannot be written once at run start, and the reason is worth recording because the obvious implementation is silently wrong.

`eval-set.json` earns its place: the viewer reads it (`read_eval_set_info_async`) to render **pending tasks** — entries in the eval set that have no log yet — via `appendPendingItems` in `LogsPanel` and `SamplesPanel`. That is exactly the live progress view Steward wants, and without the file the viewer shows only landed logs with no sense of the whole set. Absence degrades gracefully (the reader returns `None`), but the feature is lost.

The trap is `EvalSetTask.task_id`. `to_eval_set_task` resolves it as `existing_task_id or task.id or eval_set_identifier`, and `task.id` is a fresh `uuid()` assigned at resolution (`loader.py:112`). In an ordinary `eval_set()` run the manifest's task_ids match the logs' exactly — because one process both resolved and ran. **Steward's workers each resolve independently**, so a worker's log task_id is unpredictable to Steward. Writing `eval-set.json` up front from the manifest would give every task an id matching no log, and the viewer would render each one as *both* landed and pending.

The fix needs no protocol change, just the same algorithm driven from a different source: rewrite `eval-set.json` as logs land, taking `task_id` from the log for tasks that have one and falling back to the task's `identifier` as a placeholder for those still pending. That is `existing_task_id or … or eval_set_identifier` computed from the log directory rather than from `ResolvedTask`s, and Steward is already rewriting directory metadata on the same trigger.

### Sharing the directory operations with `eval_set()`

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

**One difference sharing does not erase.** `cleanup_older_eval_logs` keeps the newest log per task id and deletes the rest, but Steward's adjudication model needs failed attempts *kept* until they are resolved. Steward therefore calls the same function on a different schedule — once at the end, after adjudication settles, rather than on every pass. Same code, different timing; worth recording so nobody moves the call earlier as a tidy-up.

### What enforces single-writer

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

### Multiple logs per task

A task can end up with more than one log: a first attempt that failed, then a resumed attempt. That is the same situation `eval_set()` produces on retry, and Steward resolves it the same way — the latest successful log for an identifier wins, and the run's final sweep removes superseded failed logs. Steward keeps them until then, because the attempt history is exactly the diagnostic material it exists to reason about.

## Recovery: retry, requeue, adjudication

The model rests on **`fail_on_error=False`**: sample errors never mark a task failed, so a task that reaches the end of its dataset finishes `status="success"` whatever residue of errored samples it carries. Because the whole design depends on it, worker mode **hard-codes it** rather than routing it through configuration — a definition asking for fail-fast is asking for a completion decision that belongs to the runner. `continue_on_fail` needs no override at all: it is moot once `fail_on_error` is `False` (`_should_eval_fail` returns `False`, so the mid-run abort it guards can never fire).

Sample-level retry is the opposite case and stays the definition author's to set. `retry_on_error` passes through worker mode untouched, because how many attempts a sample deserves is a property of the eval — a flaky sandboxed task and a pure-inference task want different numbers, and the author knows which they wrote. Steward's assumed default is 3, but it is a default, not a constraint.

The point of forcing `fail_on_error=False` is to convert a binary task outcome into a **sample-level work list**. Under `fail_on_error=True`, three bad samples out of five hundred make the task "failed", and the only lever is a whole-task respawn. Under `False`, the task is done and what remains is precisely "these three samples need resolution" — the granularity adjudication actually operates at.

The definition's own `fail_on_error`, `continue_on_fail`, and `retry_on_error` are recorded in the capture manifest's `options`, so Steward can see what it is honouring and what it is overriding rather than having to guess.

Recovery therefore happens at three tiers, in increasing order of how much judgement each requires.

### Tier 1 — in-eval sample retry (no judgement)

`retry_on_error` handles transient sample failures inside the worker, with no supervision, at whatever count the definition set. Two properties matter:

- **Retries do not hold a concurrency slot.** Inspect performs the retry recursion deliberately outside the sample semaphore (`_eval/task/run.py`, the `retry_on_error > 0` branch after the sample scope exits), so a retrying sample releases its `max_samples` slot and re-enters at the back of the sample queue. No head-of-line blocking, no deadlock against the cap. The only observable effect is ordering — retries land behind pending samples and so tend to finish late in a task.
- **Exhaustion is terminal but not fatal.** A sample that burns all three attempts is recorded errored, and (with `fail_on_error=False`) the task carries on.

### Tier 2 — in-flight requeue (cheap judgement, live)

Inspect's control channel exposes `POST /evals/{eval_id}/sample/requeue`, alongside `GET /evals/{id}/samples` (listing plus status histogram) and `GET /evals/{id}/sample` (summary **plus error detail**). That is the full loop Steward needs to act on a failure *while the task is still running*: read the error detail, classify the cause, and re-open the sample's slot if the cause was transient — cluster pressure, a model API outage, a sandbox hiccup — rather than a genuine failure.

This is strictly better than waiting for the end when the classification is confident: the sample re-runs inside the task that is already warm, with no respawn and no resume read. Requeue is idempotent (a repeat lands in the already-queued rows and reports `changed: False`), and it re-opens a *terminal* sample's slot, so it deliberately goes beyond the configured `retry_on_error` budget.

**That last point needs a guard.** Inspect enforces no per-sample requeue ceiling — the endpoint will keep accepting. A naive "error looks transient → requeue" rule loops forever against a model API that stays down. Steward must carry its own per-sample requeue budget and an escalation path when it is spent.

### Tier 3 — post-completion adjudication (real judgement)

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

### What is left for task-level retry

With tiers 1–3 covering sample failures, whole-task retry narrows to what no sample-scoped mechanism can reach:

- **Errors outside sample scope** — dataset load, task setup, sandbox provisioning, scorers and metrics at the end. These still produce `status="error"` regardless of `fail_on_error`.
- **Hangs.** A sample past its `time_limit` or `working_limit` records a limit event, not an exception, so `retry_on_error` never fires.
- **The process dying** — OOM, host loss, SIGKILL. Not an error from Inspect's point of view at all.

All three are handled the same way: respawn with `resume`. And a worker performs **no task-level retry of its own** — worker mode forces `task_retry_attempts=0` regardless of the definition's `retry_attempts`. Honouring the definition's value would multiply Steward's attempt budget by it and leave a failed log per in-process attempt in the shared directory. With three sample attempts already inside, an in-process task loop would be a third multiplier on the same failures.

This reverses the lean recorded in configuration.md's open question 1. Worker mode changed the trade-off: with one `eval()` per process, in-process task retry and a Steward respawn are the same operation at different levels, and `resume` preserves the sample reuse that made the in-process version worth keeping.

### Two invariants this model creates

**Completion is not success.** With `fail_on_error=False`, a worker exits 0 and its log says `success` even if every sample errored. Worker exit status therefore carries no information about whether the work is good — Steward must read the log, always. This is the sharpest version of the rule that the log directory is ground truth.

**An eval set is not done while samples are unresolved.** `fail_on_error=False` removes the mechanism that used to make a broken run loud. A task whose model API was down for its entire duration now completes "successfully" with five hundred errored samples and a meaningless score, and nothing in the log's status says otherwise. Adjudication is consequently not optional garnish — it is the only thing standing between a broken run and a plausible-looking result. Steward must treat *any* errored sample as unresolved state, and must refuse to report an eval set complete until every sample is either resolved or explicitly accepted by a human. Metrics computed while the queue is non-empty are provisional and must be reported as such.

**A trap worth naming, because Inspect's own code steps into it.** `eval_set()` decides a log is complete with `log_samples_complete`, which compares `results.total_samples` against the expected count — and `total_samples` is "dataset samples × epochs", errored samples included. `completed_samples` is the field that means "completed without error". Under `fail_on_error=True` this is harmless, because an errored sample already made the log `status="error"`. Under `fail_on_error=False` it is not: 497 good plus 3 errored reads as `status="success"`, `total_samples == 500`, therefore complete. Inspect's own machinery would call that task done and never revisit it — only `invalidated` reopens it (`list_latest_eval_logs` routes invalidated logs to the retry bucket).

Worker mode skips that classification entirely, so nothing is broken today. But it means Steward must not reimplement completeness the obvious way: **completeness is `completed_samples`, never `total_samples`.**

## Detachment and the in-flight record

Steward spawns workers **detached** (`start_new_session` on POSIX, `DETACHED_PROCESS` on Windows) so a run survives Steward exiting — including the supervisor exiting, which is why workers are not its children (see *The supervisor*). Note that Inspect's `--detach` is a *CLI* feature (`inspect eval --detach`), not an `eval_set()` kwarg, and Steward's workers are never the Inspect CLI — so Steward does its own detached spawn rather than passing a flag through.

Detachment creates the tracking problem: Steward must be able to answer "what is running right now?" after a restart, without a live parent-child relationship.

### Why `task -> pid` is not enough

PIDs are recycled, are meaningful only on one host, and — critically — are *unknown during the window between deciding to spawn and the spawn returning*. A crash in that window leaves a worker whose existence Steward has no record of. So the tracking artifact is an **append-only record** (`.steward/inflight.jsonl`), not a table of current state:

| Record | Written | Carries |
|---|---|---|
| `intent` | **before** the spawn | task identifier, argv, target log dir, attempt number |
| `launched` | after the spawn returns | host, pid, process start time, control socket path |
| `exited` | when the worker is reaped or observed gone | exit status, observed-at |

Current state is *derived* by replaying the record, never stored. An `intent` with no `launched` is the ambiguous case, and it is the one that matters: on restart Steward reconciles it against the log directory and the control discovery directory before deciding anything.

Process start time is recorded alongside the pid specifically to defeat PID recycling: a live process whose start time differs from the recorded one is a different process.

### Leaning on control discovery

Inspect already maintains a discovery directory — `<inspect_data_dir>/control/<pid>.json`, holding `pid`, `socket_path`, `started_at`, `run_id`, and the control API version, with stale-PID reaping in `list_alive_discovery_entries`. **Liveness is therefore a solved problem to consume, not rebuild.** What discovery cannot supply is the task mapping (the record carries the eval's `run_id`, not the identifier Steward scheduled) and the pre-spawn intent, which is precisely what the in-flight record adds.

### The in-flight record is an accelerator, not a source of truth

Ground truth is the log directory. A record that is lost, truncated, or stale degrades Steward to scanning the log directory and the discovery directory to rebuild state — slower, never wrong. Nothing in the design may make a decision it alone can justify.

> **Not to be confused with the journal.** [workflow.md](workflow.md) uses *journal* for `journal.jsonl`, the durable record of anomalies and adjudication rulings — the one file in a workspace that cannot be reconstructed. The in-flight record described here is its opposite: disposable, machine-only, and rebuildable from the log directory at any time.

## The supervisor

"Supervisor" here means *whatever is currently driving the reconcile loop* — in the current design a coding agent scheduling `tend` calls, with `cron` as a backstop (see *The reconcile core, and its drivers*). The claim, the in-flight record, and the registry apply to every driver equally.

**This section designs the detached case, which the current plan does not build.** It is kept because it is the only driver with a lifecycle worth designing, and because writing it down is what established that the lifecycle is a cost rather than a capability — the argument for agent-driven tending is largely assembled from the paragraphs below. Read it as the specification that would be implemented *if* the utilization evidence ever calls for a daemon, not as work in the queue.

**The run and the supervisor have separate lifetimes.** Requirement 4 says the *run* outlives the process that started it, and detached workers achieve that by themselves. If the supervisor dies, workers keep running and keep writing logs; what stops is *supervision* — no new tasks scheduled, no scan passes, no requeues, no adjudication. That is a real degradation but a graceful one, and it is why both levels detach rather than making workers children of the supervisor: either layer can die without taking the other with it, and a replacement supervisor adopts the survivors from the in-flight record.

**Supervision still has to outlive the invoking session** — unless something else is tending it. Steward exists to run things autonomously for hours, and a coding agent that starts a sweep and then ends its session must not leave it unsupervised until someone notices. Detaching is one answer; an agent that reliably schedules `steward tend` is another, and often the better one. What must not happen is a long run with *no* driver.

**If a detached supervisor is ever built, it would be launched rather than attached to.** There is no foreground driver in the current design — `steward launch` spawns workers and returns, and the loop is driven by scheduled `tend` calls — so the interactive/detached choice Inspect offers does not arise. What is still worth borrowing wholesale is Inspect's contract for detaching: `exec_detached` spawns the child, waits for its `launch` record, and **refuses to leave a detached process running when it failed to bind a control endpoint** — on the grounds that a detached process nobody can observe or cancel is worse than no process. A detached Steward supervisor should be held to the same rule: it advertises its socket in the registry, and if it cannot, the launch fails rather than orphaning an unsupervisable daemon.

That yields a pleasing symmetry: the supervisor is to `steward status` what a `--ctl-server` eval process is to `inspect ctl` — a detached process advertising a socket through a discovery directory, with the CLI as a thin client.

**Not everything needs one.** `steward tasks` is pure enumeration and never touches a supervisor. `steward status` queries a live supervisor when there is one and otherwise reads the log directory directly — slower, same answer, because the log directory is ground truth either way.

**The costs are real and worth naming.** A daemon is a thing to operate: its own diagnostics have to go somewhere, it needs a clean stop, and upgrading the Steward package while a supervisor from the previous version is still running is a version-skew problem the protocol between CLI and supervisor has to tolerate.

### What the supervisor decides, and what it escalates

The argument for a supervisor is not that a process is more reliable than a coding agent. It is that **the agent is intermittent and the run is not.** An eval set running for eight hours spans many agent sessions, or none; the agent ends its session, hits a context limit, or is simply not invoked again until morning. Anything that must happen on a cadence — reaping dead workers, starting the next task as a slot frees, scan passes, requeueing a clearly-transient failure, writing a periodic status summary — cannot depend on someone being present to ask for it.

That argument establishes the need for a *cadence*, though, not the need for a *daemon* — a crontab line supplies one at a fraction of the cost. What follows is still the right division of labour; it just does not require a long-lived process to enforce it.

So the division is by *kind of work*, not by reliability:

- **The supervisor keeps the run alive.** Mechanical continuity: maintain the worker pool, record what it launches, run scan passes, requeue within budget, keep the eval-set metadata and status current. All of it policy execution, none of it judgement.
- **The agent (or human) decides what the run means.** Is this error class systemic or incidental? Is this arm worth continuing? Is this score anomalous enough to invalidate? Should the budget be raised?

The supervisor's other job is therefore to **leave a good trail for the intelligent-but-absent party**: an escalation queue it refuses to act on alone, and a periodic written summary. That is what makes something like an hourly `status.md` load-bearing rather than decorative — it is the handoff artifact the agent reads when it next appears, and the reason a run can be picked up cold.

This division softens considerably when the agent is itself the driver: it sees each reconcile as it happens, so judgement and continuity coincide and the escalation queue never has to accumulate for long. The division still matters — the agent may stop tending at any point, and whatever is left behind has to be legible to whoever picks it up — but it becomes a property of the *artifacts* rather than a split between two actors.

**The honest caveat.** A daemon has a failure mode `tend` does not: it can *wedge* — deadlocked, or blocked on a hung request — while still looking alive. A scheduled `tend` that fails simply does not run; a wedged supervisor holds its claim and blocks the replacement that would have taken over. That is strictly worse than being dead, and it means pid-liveness is not a sufficient definition of "the supervisor is up" (see the heartbeat note under *What enforces single-writer*).

### Interacting with a detached run

> **Superseded in part.** [workflow.md](workflow.md) concludes there should be no `steward tui`: a live view presumes a present human, which is the case Steward is explicitly not built for, and `steward status` plus `inspect view` covers what someone actually wants on returning. This section is retained because its *separation* argument — that a view is a client of the same surface as everything else, needing no claim and no live supervisor — is what made that conclusion safe to reach. Read `steward tui` below as "a view, if one is ever built".

The display is an aggregate — tasks by state, sample progress, the adjudication queue, spend — assembled from the worker control endpoints and the log directory. It cannot be Inspect's own display relayed, because workers are separate detached processes writing their own output elsewhere. That constraint turns out to be a gift: the display is a **client of the same surface** everything else uses, not a privileged view of in-process state.

Which means the view and the supervisor are separable, and should be separated:

| | driver | view |
|---|---|---|
| `steward launch` | none — spawns and returns | none |
| `steward tend` | this process, briefly | none |
| a view, if built | wherever it already is, or nowhere | in this process |

`steward tui` attaches a live display to whatever supervisor is running — the natural way to keep an eye on a detached run for a while and then walk away. It needs no new machinery: it is `steward status` rendered continuously instead of once, over the same registry lookup.

Three properties follow, all of them good:

- **Views need no claim.** The claim is a *writer's* lock. Any number of TUIs can watch one run, and a human and an agent can watch the same run simultaneously. Directives issued from a TUI still go to the supervisor, which serializes them, so even an interactive view stays safe.
- **A view works without a supervisor.** With none live, `steward tui` renders read-only from the log directory — a finished run, or one whose supervisor died. Same fallback as `steward status`, same reason: the log directory is ground truth. Under agent-driven tending this is not the fallback but the *ordinary* case: between tends there is no process to attach to, and the TUI is simply `steward status` on a repeat, watching workers it does not own.
- **There is no attached-versus-detached mode to choose.** With `launch` non-blocking and the loop driven by scheduled `tend` calls, a view is only ever a separate process watching from outside. The two code paths that this section was originally reconciling never come into existence.

One consequence survives regardless of whether a view is ever built, because it applies to any interactive surface Steward grows: Ctrl+C must detach the *view*, not the run. Inspect's Ctrl+C cancels an eval, but a Steward run is a longer-lived thing that a coding agent may have started and a human may merely be visiting, so "leaving" and "stopping" must be different gestures — `steward stop` for the latter, with the TUI saying so on exit. This is the `docker attach` / `tmux` convention rather than the `inspect eval` one, and it removes a genuine "did I just kill my overnight sweep?" hazard.

Beyond the TUI, the same surface is reached through the CLI: `steward status`, `steward pause`, `steward resolve …` are thin clients that locate the supervisor via the registry, falling back to the log directory for reads when none is live. **The CLI is the agent's API** — a coding agent should never need to speak the wire protocol, and making it do so would be a design failure rather than a power feature.

Two layers of control channel then stack, and the direction matters:

- **agent → supervisor** (Steward's own surface): status, pause, abandon an arm, resolve samples, adjust budgets.
- **supervisor → workers** (Inspect's `ctl`): requeue, retune limits, pause a model.

An agent *can* read a worker's endpoint directly and that is harmless. It should not issue directives there: requeue budgets and escalation state live with the claim holder, and a second party issuing directives puts that accounting in two places. Reads fan out; writes go through the supervisor.

### The reconcile core, and its drivers

The supervisor is not the architecture. The architecture is a **pure function**:

```
reconcile(manifest, inflight, log_dir) -> (actions, summary)
```

Given the eval set's definition-derived manifest and the current on-disk state, decide what to do: which workers to spawn, which finished, what needs a scan pass, what needs adjudication. Nothing in it depends on memory carried from a previous call, because the design already guarantees that everything the supervisor knows is reconstructible from the in-flight record and the log directory — the supervisor is a *cache*, never a source of truth.

Committing to that shape buys three things that are hard to get any other way:

- **Exhaustive testability.** Scheduling correctness becomes "given this directory state, what actions?" — unit-testable without clocks or processes. For a component that decides whether expensive evals run unattended and correctly, that is not a nicety.
- **Crash recovery is the normal code path.** There is no separate resume routine to get wrong; recovery is just the next call, exercised constantly.
- **Driver independence.** A wedged long-lived process stops being frightening: kill it, and anything else can drive the same function.

**Drivers, one core.** The intended arrangement is that **the coding agent is the only driver**, scheduling `steward tend` on an interval of roughly ten minutes.

| driver | status |
|---|---|
| **the coding agent** — schedules `steward tend` | the design centre |
| `cron` calling `steward tend` | near-free backstop if the agent stops tending |
| detached long-lived supervisor | **not currently justified** — see below |

**Why the agent, and not a daemon.** If the agent schedules the tend, the two halves that *The supervisor decides, and what it escalates* splits apart — mechanical continuity and judgement — come back together: the agent sees each reconcile as it happens, can adjudicate in the moment instead of accumulating a queue, and *is* the escalation path rather than needing one.

The strongest argument for a daemon was low-latency tier-2 requeue, and it does not survive scrutiny. Deciding that a sample's failure was transient is **judgement**, and this design explicitly says the mechanical layer does not exercise judgement. So a daemon would either apply crude rules or escalate — and if it escalates, the latency is agent-bound anyway. The daemon never had a real claim to that work. Requeue that genuinely warrants no human involvement is rare; a ten-minute delay on the rest is invisible next to a task that runs for hours.

**What a ten-minute interval actually costs** is slot utilization, not responsiveness: a worker that finishes at t=0 leaves its slot idle until the next tend. That only bites when concurrency is capped below the task count, and it scales as `interval/2 ÷ mean task duration` — a few percent for multi-hour tasks, badly for short ones. Short tasks have their own fix that needs no daemon: **batch several into one worker's selection**, which the selection document already supports by taking a list. The bad case for a long interval is exactly the case where per-worker overhead argued for batching anyway.

**Two things get materially simpler without a long-lived process.**

- **The claim becomes short-lived** — held for the seconds a `tend` runs, not the hours a run lasts. That very nearly dissolves the wedging problem, which was the daemon's worst failure mode: no heartbeat protocol is needed, because a claim older than a generous `tend` timeout is unambiguously stale. This is conditional on the invariant that a tend spawns and reaps but never does long work itself (see *A scan is a detached process, not part of a tend*) — the moment anything unbounded runs inline, the short claim and everything it buys are gone.
- **The failure mode is benign.** If nothing tends the run, workers finish what they are doing, their logs land, and the run simply stops progressing. It pauses; it does not break. The next `tend` from anyone resumes it. Compare a wedged daemon, which holds its claim and blocks its own replacement.

**What would justify adding the daemon later** is a measurable signal, not a preference: sustained slot idle that batching cannot absorb. The pure-function core keeps that option open at no cost — the daemon would be a driver of the same `reconcile`, not a rewrite.

It does impose two requirements the design must honour:

- **Idempotence and claim discipline are non-negotiable.** An agent is an unreliable scheduler in a specific way: it may tend late, not at all, twice, or be interrupted mid-`tend`. A pure function plus the run claim plus the `intent`-before-spawn entry already covers this — a repeated `tend` is a no-op, an interrupted one is reconciled by the next — but it means those pieces are load-bearing rather than defensive.
- **The output must be compact and structured.** Agent context is the scarce resource, so a `tend` must *summarize* — "3 finished, 2 errored, spawned 3, one arm needs a decision" — not dump a thousand log headers into the conversation. That is a real API constraint on `steward tend`: a small JSON summary designed for agent consumption, with detail available on request. It also makes the tend interval an economic choice (tokens) as well as a utilization one.

The one thing an agent cannot promise is cadence: its reliability is its harness's reliability across session boundaries. That is what the `cron` row is for — the same verb, on a timer, costing one crontab line.

**A nice unification falls out.** The `summary` a tend prints *is* the status update. An agent reads it inline and decides; a human sees it on stdout; a `cron` driver appends it to a file. One artifact, three consumers — rather than a status file invented separately for the absent reader.

### `status` and `tend` are one function, two dispositions

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

### Supervising workers

Workers run with Inspect's control server enabled, so each has a live HTTP endpoint over an AF_UNIX socket. That channel is what makes Steward a *steward* rather than a batch launcher: it can query a running eval's state and adjust its runtime behavior without restarting it.

Steward finds a worker's endpoint by pid via the discovery directory, correlated to a task through the in-flight record.

The endpoints most relevant to this document are the ones tier 2 recovery is built on — `GET /evals/{id}/samples`, `GET /evals/{id}/sample`, and `POST /evals/{id}/sample/requeue` — plus the runtime-tuning directives (`POST /config` for per-sample limits, the pause/resume latches at process, task, and model scope). Model-scoped pause is worth noting alongside requeue: when the classification is "this provider is down", pausing the model is the correct response and requeueing individual samples is not.

## Changes required in inspect_ai

1. **Capture mode** — `INSPECT_EVAL_SET_CAPTURE`. *Landed.*
2. **Selection mode** — `INSPECT_EVAL_SET_SELECTION`, including the resume path and the mutual exclusion with capture. *Landed* (`_eval/eval_set_selection.py`, plus the branch in `eval_set()`).
3. **Error-handling overrides** — *landed, as part of worker mode.* `fail_on_error=False` and task-retry-off are applied by selection mode itself, and the definition's requested values are recorded in the capture manifest's `options`. This deliberately avoids routing them through the overrides channel: they are not tunable policy, they are what selection mode *means*.

4. **Overrides channel** — `INSPECT_EVAL_SET_OVERRIDES` for `log_dir` and the operational supervision kwargs (`display`, `log_level`, `ctl_server`, `max_tasks`, and the concurrency setpoints). *Not yet.* Until it exists, Steward must reach `log_dir` per frontend (flow accepts `--log-dir`; a raw script does not). Note the corresponding `INSPECT_EVAL_*` environment variables are click `envvar=` bindings on Inspect's own CLI only (`_cli/eval.py`) — neither a raw `python evalset.py` nor `flow run` goes through it, so they are not a shortcut.

   The concrete motivating case is `steward launch --smoke` ([workflow.md](workflow.md), *Smoke first*), which must send a rehearsal's logs to local scratch rather than to the definition's `log_dir` — frequently S3, and no place for throwaway objects. Flow definitions can be redirected today; script ones cannot, so smoke is Flow-only until this lands.

   Hard-coding the error-handling options keeps this channel purely *operational*: nothing on the whitelist can change what an eval means. Had `fail_on_error` gone here, the channel would have needed two tiers to keep semantic overrides visible.
5. **Automatic early pruning** (Layer 2 in configuration.md) — the `@task` registry wrapper returning placeholders for unselected tasks. *Not yet*, and it is what makes a worker's startup cost proportional to its own task rather than to the whole eval set.

6. **Public eval-set directory operations** (see *Sharing the directory operations with `eval_set()`*) — a documented public surface that both `eval_set()` and external runners call, so the preparation/cleanup protocol has one implementation rather than two that drift. Also rationalizes a surface that is already inconsistently half-public. Most of the cleanup band needs no change beyond export; the preparation half needs `validate_eval_set_prerequisites` and `write_eval_set_info` refactored to take identifiers and plain `EvalSetTask` rows instead of live `ResolvedTask`s. Low risk, and it retires a whole category of future divergence.

   Deliberately *not* extended to the capture manifest and selection document: those cross a process boundary as data, so they stay private and versioned. See the two-contracts note in that section.

7. **Notification outside an eval** (see [workflow.md](workflow.md), *The gap: notifying from outside an eval*) — `inspect_ai.util.notify()` is a silent no-op unless an Apprise instance was installed by `eval_resolve_tasks`, so it does nothing when called from a Steward process. `build_apprise()` / `init_apprise()` already do exactly what is needed but are private. Exporting them (or a `notification_scope(config)` convenience) is the same public-surface move as item 6, and it is not Steward-specific — any script that runs evals and wants to be told when they finished hits it.

The selection schema can grow the partial facets Layer 2 needs (`name`, `args_hash`, `model`, `sequence`) as optional additive fields without a version bump — Steward writes the file and Inspect reads it, and an older Inspect ignores fields it does not know. A change in the *meaning* of an existing field requires bumping `EVAL_SET_SELECTION_VERSION`, which Inspect checks and rejects when it is too new.

## Open questions

1. **Flow's pre-boundary work.** Everything Flow does before reaching `eval_set()` is out of worker mode's reach, and every flow worker repeats all of it. Verified against a four-task spec with four concurrent workers: the run works correctly (four logs, correct identifiers, no eval-set metadata written), but each worker independently

   - scans the log directory (`find_existing_logs`) — the only cost that *grows* with the run, at O(logs) header reads;
   - resolves the spec and writes `flow.yaml` into the log directory;
   - shells out to `uv pip freeze` and writes a ~200KB `flow-requirements.txt`.

   Measured overhead was ~1.1s per worker over an equivalent plain-`eval_set()` definition on a near-empty log directory — modest per worker, N× in aggregate, and growing.

   The last two are also **truncate-in-place writes to shared paths** (`_config/write.py`, `_launcher/freeze.py`). Content is identical across workers, so divergence is not the risk; a concurrent *reader* (e.g. `flow list`) catching a half-written file is. It held up under test, but that is one trial of an unsynchronized write, not a guarantee.

   The clean fix is on Flow's side and is small: a way to skip the pre-boundary artifacts when an external runner owns the log directory — either a flag Steward passes, or Flow noticing `INSPECT_EVAL_SET_SELECTION` and skipping `write_config_file` / `write_flow_requirements` / the log scan. Steward would then have one worker write those artifacts once, or write them itself. Accepted as-is until it hurts.

2. **What replaces flow's store.** With `--store none`, flow's completion decisions and log reuse go away. Steward makes those decisions from the manifest and the log directory, but the store also provided cross-run reuse (finding an identical task's log from a *previous* run). Whether Steward offers an equivalent, and whether a single shared log directory makes flow's store usable again rather than disabled, is unresolved.

3. **Worker startup cost without Layer 2 pruning.** Every worker currently constructs every task in the eval set, including datasets, to compute identifiers. For a large sweep this dominates. Layer 2 fixes it; until then, batching several tasks into one worker's selection is the available mitigation.

4. **Cross-host runs.** The in-flight record carries a host per launch, and both control discovery and the run claim are per-machine, so the current design supervises a run from one host. Distributing requires two things Steward would own rather than consume: a discovery mechanism for workers, and a real lease on the log directory with a fencing token to replace the local claim. The lease is the harder half — `log_dir` may be S3, where atomic create-if-absent is not reliably available through the filesystem abstraction Inspect uses.

5. **Second `steward launch` against a live run.** Declining is the safe first behaviour; attaching to the existing run and reporting its status is what a caller (especially a coding agent) actually wants. What "attach" means concretely — read-only status, streaming progress, or the ability to issue directives through the supervisor — is unresolved.

6. **Torn reads of the directory manifests.** Steward is the only writer of `eval-set.json` and `logs.json`, but both writes are truncate-in-place, so a concurrent *reader* can still catch a partial file — and `eval-set.json` has a live reader in the viewer (`read_eval_set_info_async`, which validates with no error handling). Rewriting it as logs land makes the window recur throughout the run rather than once. Either Steward writes these atomically, or the writers in Inspect become atomic. The latter is better for everyone, since `eval_set()` has the same exposure today.

7. **Error classification.** Tiers 2 and 3 both turn on "was this failure transient?", and nothing answers that today. The inputs are available (`GET /evals/{id}/sample` returns error detail; logged samples carry their error and the `error_retries` history of prior attempts), but the taxonomy — provider outage, rate limit, cluster pressure, sandbox failure, agent-caused, genuine task failure — and how confidently each can be inferred is unresolved. Everything Steward does automatically rests on this, so it deserves its own design.

8. **Requeue budget and escalation.** Inspect enforces no per-sample requeue ceiling, so Steward needs its own: how many tier-2 requeues a sample gets before it drops to tier-3 adjudication, and what escalates to the human. Related: when the classification is provider-wide, the right action is a model-scoped pause rather than per-sample requeues, and Steward needs a rule for choosing between them.

9. **Steward-orchestrated scanning.** The shape is settled (see *How Steward would take the scan over*): a third boundary mode, `INSPECT_EVAL_SET_SCAN`, that executes the definition and scans the named logs as single writer. Unbuilt, and these remain open:
   - **Cadence.** Incremental passes during the run cost a full sample read per log per pass, and a pass can outlast the tend interval — so the queue-and-drain model above is the shape, but how much to let accumulate before spawning is unresolved. Draining eagerly keeps coverage current at the cost of many short processes; draining lazily batches better but leaves scan results further behind the run. Model-graded scanners push this further, since a pass then has a token cost as well as a time cost.
   - **Sample reads.** The sketch reads whole logs; large transcripts make that expensive. Whether to stream samples, or scan straight from summaries plus a targeted transcript read, is unresolved.
   - **Failure of the scan pass itself.** A scan worker can crash like any other. It is idempotent, so the recovery is "run it again" — but it needs to appear in the in-flight record and its accounting like a task worker does.
   - **Scan errors vs sample errors.** `scan_finalize` leaves a scan resumable when scanners errored. That is a second adjudication queue, distinct from the errored-sample one, and it is not yet clear whether Steward should treat them alike.

   Until it exists, `options["scanners"]` in the manifest lets Steward refuse a scanning definition at enumeration time with a clear explanation rather than failing every worker.

10. **Tend cadence, and the evidence that would justify a daemon.** With the agent as sole driver, the cost of the interval is slot idle: roughly `interval/2 ÷ mean task duration` whenever concurrency is capped below the task count. Ten minutes is a guess, not a measurement, and a tend has a second price a daemon does not — agent context per call — so the optimum is not simply "as often as possible". What is needed is actual idle-time accounting from real sweeps, which the in-flight record can supply directly (`exited` to next `intent` per slot). Whether batching absorbs the short-task case, and whether the residue ever exceeds what a daemon would cost to operate, is the question that decides if the detached driver gets built.

11. **What "resolved" means for an eval set.** *Answered in [workflow.md](workflow.md).* A run is **resolved** when no anomaly is open — anomaly state being a fold over `journal.jsonl` — and separately **signed off** when a person has attested to the results. Scan findings arrive last and can re-open a run that looked finished, which is why signoff follows the scan rather than the tasks. `EvalLog.invalidated` and per-sample `invalidation` records carry the provenance half. What remains open there is the anomaly *identity* scheme, not the definition.
