# Workflow

**Status: sketch. Actively being figured out — expect whole sections to be wrong.**

[configuration.md](configuration.md) covers how a definition becomes a manifest. [execution.md](execution.md) covers how a manifest becomes running processes. This document is about the layer above both: what a person actually *does* with Steward, from starting a sweep to trusting its results.

## 1. The premise, restated

Three facts drive every decision here:

- **The human is absent.** If someone were going to sit and watch, they could have run `eval_set()` themselves. Steward exists for the run you start and walk away from.
- **The agent is intermittent.** It works in sessions. It hits context limits, gets interrupted, and is not invoked again until morning.
- **The run is continuous.** Workers execute whether or not anyone is looking.

Everything below follows from the mismatch between those three clocks. The directory is the only thing present the whole time, so the directory has to carry the state, the instructions, and the record.

## 2. A project, not a run

The word *run* invites a model this design does not have: a thing that starts, has an identity, and ends. What a workspace actually holds is a **project** — one definition that evolves over time, one log directory that accumulates its results, and one journal recording what was decided. Work happens in episodes, but an episode is not an entity: nothing keys on it, nothing is named after it, and it needs no id.

What replaces it is a **convergence loop**, and Steward already had the shape without naming it:

```
manifest (from the definition)  =  desired state
log directory                   =  observed state
tend                            =  the controller
```

`reconcile(manifest, inflight, log_dir)` is a controller driving observed state toward desired state. Edit the definition, and the next launch captures a new desired state; everything already satisfying it is left alone. That is not a mechanism Steward invents — it is what `eval_set()` has always done when pointed at the same log directory twice, and Steward inherits it by inheriting the boundary.

### 2.1 Inspect already splits identity from completeness, and the split is the whole trick

Two mechanisms upstream do the work, and it matters that they are separate:

- **`task_identifier`** answers *is this the same task?* — task file, name, args, model, resolved plan, generate config, model roles, version, and the execution limits (message, token, turn, time, working, cost).
- **`log_samples_complete`** answers *has enough of it been done?* — and takes `epochs` and the dataset `limit` as arguments held deliberately *outside* the identifier, comparing them against the log's sample counts.

So an edit sorts itself into one of three outcomes with no policy from Steward at all:

| you change | identifier | what happens |
|---|---|---|
| add a task, add a model | new tasks appear | new work; existing logs untouched |
| task args, solver, generate config, a task limit | **changes** | a *different* task — new work, and the previous log is superseded |
| `epochs`, dataset `limit` | **stable** | the *same* task, now incomplete — resumes and adds only the missing samples |
| nothing | stable | everything no-ops |

**There is a fifth row nobody would think to look for: renaming the definition file.** `task_identifier` is `{task_file}@{task_name}#{args_hash}/…`, and `task_file` is the path the task was defined in — so `git mv sweep.py experiment.py` gives every task a new identifier, and the whole previous run reads as orphaned. That is upstream's behaviour and it is defensible (a task in a different file is arguably a different task), but it is a foot-gun for a tool whose job is to reuse results. A Steward workspace is immune to it by accident of a decision made for other reasons: the definition has a **fixed name** (`evalset.py`, `flow.yaml`, `hawk.yaml`) that `init` creates and discovery looks for, so there is nothing to rename. Worth knowing before anyone proposes making the name configurable.

The third row is what makes extending a project cheap rather than merely possible: raising epochs from 1 to 3 reuses the epoch-1 samples and runs epochs 2 and 3, because resume matches samples on id *and* epoch. Nothing has to be re-derived and nothing is re-paid for.

**It also needs no new mechanism, and cannot have one.** Epochs is not a live knob — nothing on the control channel adds work to a running eval, by design ([execution.md](execution.md), *The channel changes how work runs, never what work exists*). Extension happens through the loop instead: the running worker is left alone, its log lands and reads as incomplete, and the next tend respawns with `resume`. Letting it finish is clearly right rather than merely defensible, because what it is producing is epoch 1 — precisely what the extension reuses. The cost is latency, since a task hours from finishing starts its new epochs hours from now.

### 2.2 Steward never destroys a result, but it does curate the directory

The tempting invariant is *additive only* — Steward adds work and never removes anything. That is the wrong line, and the case that breaks it is ordinary: a provider has an outage, you decide mid-flight to drop that arm. Under additive-only its half-failed logs stay in `logs/` forever, so the arm you abandoned is still in your results.

So the invariant is stronger and more useful:

> **A log leaving `logs/` is always a move, never a delete — reversible, and journaled with its reason.**

Superseded and removed logs move to a sibling **archive** directory (`logs-archive/` beside `logs/`; `s3://…/logs-archive` beside `s3://…/logs`), derived from whatever `log_dir` the definition chose rather than from the workspace.

**A sibling and not a subdirectory, for a mechanical reason:** `list_eval_logs` defaults to `recursive=True`, so anything nested inside `log_dir` is still found by `samples_df`, the viewer, and every listing. An archive underneath `logs/` would hide nothing.

What that buys is a precise meaning for the log directory, which it did not have before: **`logs/` is the current definition's results and nothing else.** That is what makes `samples_df(log_dir)` trustworthy without anyone remembering a filter — and it is *closer* to the conformance standard [execution.md](execution.md) sets, not further from it, because an `eval_set()` directory holds the results of one definition rather than of every definition it ever had.

Three unrelated situations turn out to be the same operation:

| a log leaves `logs/` because | previously | now |
|---|---|---|
| its task was **removed** from the definition | accumulated forever | archived |
| its task was **superseded** by an edit (new identifier) | accumulated forever | archived |
| it is a **failed attempt** replaced by a good one | *deleted* by `cleanup_older_eval_logs` | archived |

That third row is the one worth noticing. Folding it in means **Steward never deletes an eval log** — a simple, checkable property for a tool asked to be trusted with unattended expensive work, and it costs only an `archive_dir` variant of a cleanup function Steward was already going to call ([execution.md](execution.md), *Changes required*).

**The archive is also a cache.** Edit a task's args, launch, decide the edit was wrong, revert — the original identifier comes back, and a matching log is sitting in the archive. Restoring it is a move rather than a re-run, so revert-after-edit is free.

It is the project-local member of a pair. Flow's store is the same idea globally — an index from `task_identifier` to log location, spanning projects — and Steward consults both before spawning anything ([execution.md](execution.md), *Flow's store, and who is allowed to read it*):

```
1. logs-archive/   move back  — this project ran it before
2. flow store      copy in    — some project ran it before
3. otherwise       spawn
```

Anything satisfied from either source is journaled with where it came from and named in the launch delta, because a reused log is a result this project did not produce.

The archive grows and never shrinks, which on S3 is a real bill. That is the user's to lifecycle-rule; Steward does not offer a prune verb, because a command whose job is deleting results is precisely the one this section exists to avoid.

### 2.3 One trigger, and one gate on it

Capture executes the definition, so it is expensive — minutes for a Hawk config, which installs packages and resolves secrets on the way to the boundary. More importantly, **the definition is a file a human edits live.** Anything that captures automatically eventually captures a half-saved edit.

So the division is:

| verb | reads the definition? | |
|---|---|---|
| `launch` | **yes** — captures a fresh manifest | computes the delta, reports it, commits it as desired state, spawns |
| `tend` | no | converges toward the *stored* manifest |
| `status` | no | reports state, and whether the definition has drifted since capture |

Drift detection is free: compare the definition's content hash against the stored manifest's. No subprocess, cheap enough to run on every tend, so an edit is **never silently ignored** — which is the failure that actually costs a night. The guidance is one sentence, and it is the whole of the amend story: *if you change the definition, call `launch` again.*

A second `launch` is therefore not an error to decline. It is the amend path, and it resolves [execution.md](execution.md) open question 5.

Not every delta deserves the same treatment, and the asymmetry is sharp. Adding work is what the human just asked for; removing work from `logs/` could equally be a typo:

```
launching would:
  add        3 tasks                              (1,500 samples)
  extend    40 tasks   epochs 1 → 3              (12,000 samples)
  archive   12 tasks   removed from definition    (8 logs, 2 workers stopped)
  archive    8 tasks   args changed → superseded  (4,000 samples redone)

20 tasks would leave logs/. Proceed? [--accept-archive]
```

**One predicate, two surfaces.** The same test gates the CLI and bounds the agent's autonomy:

| delta | who commits it |
|---|---|
| purely additive | **the agent, unasked** — the human typed the instruction; asking whether they meant it is the interruption this design exists to remove |
| anything that archives | **escalate, always** — a one-character change to a task arg reads identically to a deliberate removal, and quietly buys a re-run of everything |

Two consequences follow. **The timer detects drift but never applies it** — a scheduled tend keeps existing work moving; accepting *new instructions* means weighing whether a delta looks like a mistake, which is judgement and therefore the agent's ([execution.md](execution.md), *Driving and judging are separate roles that usually coincide*). So an edited definition sits as an observation until an agent collects it. And **whether the agent auto-applies additive changes at all is a `policy.md` line**, defaulting to yes: it is a standing rule about granted autonomy, which is exactly what that file is for.

The mid-edit hazard survives this but stops mattering. An intermediate save that is additive gets applied early and the next tend picks up the rest, so the final state is right and only the start is staggered — convergence is forgiving that way. One that is syntactically broken fails capture and is noise. One that is valid but wrong is only harmful when it archives, and that is gated.

### 2.4 What signoff attests to

If the definition evolves, an attestation has to name what it covered. It pins to the **manifest digest** rather than to a run id — derived rather than minted, and it gives "a signoff can be invalidated" a precise trigger: the definition changed, so what was signed is no longer what is current. See *Signoff*.

## 3. The commands

| command | who calls it | what it does |
|---|---|---|
| `steward init [DIR] [--type evalset\|flow\|hawk] [--no-git]` | human | Create the workspace: bootstrap `AGENTS.md`, a definition placeholder, `policy.md`, a repository, `.gitignore`, and the journal's first event. Only ever creates — a second run keeps what is there. |
| `steward runbook` | agent | Emit the current mechanics — how to tend, what never to do. Ships with the package so it cannot go stale. |
| `steward launch [--smoke] [--accept-archive] [--no-timer]` | agent | Capture the manifest, report the delta, commit it as desired state, spawn, **arm the tend timer or fail**, and **return**. The only verb that reads the definition, and therefore the amend path too — call it again after an edit. `--smoke` runs a bounded rehearsal first — see *Smoke first*. |
| `steward tend` | **a timer, ~q10m**; an agent may also call it to force a turn | One turn of the loop: reconcile, spawn, reap, rewrite `status.md`, append to the journal, and report which scan results have landed and which look worth investigating. Never blocks. |
| `steward status` | either | `tend --dry-run` — current state plus a preview of what the next tend would do. Read-only. |
| `steward notify [--kind attention\|stopped]` | agent | Send the human a message that carries judgement, through Inspect's notification channel. The two terminal kinds are Steward's alone — see *Four kinds*. |
| `steward signoff [--publish]` | **human only** | Attest that the results are accepted. Terminal journal entry; records who, when, and the exceptions accepted. Curates superseded attempts into `logs-archive/`, and with `--publish` indexes the signed logs into the reuse store. |
| `steward pause` / `steward stop` | either | Stop scheduling new work, or end the run. Neither is "leaving a view". |

Two things the table is meant to make obvious. **Almost everything is agent-facing**: the human's own surface is `init`, `signoff`, and asking the agent questions in prose. And **`signoff` is the one command an agent must never run** — it is a human attestation, and the runbook says so plainly.

Deliberately absent: `steward amend` / `steward update` (a second `launch` is the amend path — see *One trigger, and one gate on it*), `steward prune` (a verb whose job is deleting results, in a design built on never deleting them), `steward journal` (precedent travels with the anomaly instead), `steward tui` (see *Do we need a TUI?*), `steward note` (unproven), and `steward unclaim` (unnecessary — see below). `steward tasks` exists for diagnosing enumeration but is not part of the workflow: `launch` captures the manifest anyway and `status` reports what is running. The pre-flight "what would this cost" question is answered by `launch --smoke`, which measures it rather than guessing.

### 3.1 What `pause` actually pauses

Two things get called pausing, and only one of them is cheap:

- **Stop scheduling.** The next tend spawns nothing; workers already running finish normally. This is entirely Steward-side — it needs no control channel at all, just a flag the reconcile honours — and it is what almost everyone means by "pause the run", because the money being spent is mostly on work not yet started.
- **Suspend work in flight.** This needs Inspect's control channel, which does have pause/resume latches at process, task, and model scope. It is implementable — the journal and discovery directory give Steward every live worker's endpoint — but it is N calls that can partially fail, and a paused worker **still holds its process, its slot, and any sandbox containers it opened**. Pausing is not free the way stopping is.

So `steward pause` means the first. The second exists as an **action a ruling may authorize** — "the provider is down, hold everything on sonnet" is a reasonable thing for a human to decide — but never as something Steward does on its own; [execution.md](execution.md) works through why the automatic version arrives too late to help. Anyone reaching for it should know that a hard pause holds a sample while its `time_limit` keeps running, which Inspect names as the operator's risk. Process-wide suspension of in-flight work is rarely what anyone actually wants.

## 4. The shape, end to end

```
  human                    agent                     steward                run
  ─────                    ─────                     ───────                ───

  steward init  ──────────────────────────────────►  scaffold dir
       │
       │ "run the sonnet/haiku sweep overnight"
       └──────────────────►  reads AGENTS.md
                             steward runbook  ─────► mechanics
                             reads policy.md  ─────► this human's standing rules
                             steward launch --smoke ► 2 samples/task, 15m cap ► ●
                                  │                  (.steward/smoke/, local)
                             steward launch  ───────► claim, manifest,
                                                      spawn first workers ──► ●●●
                             schedules tend q10m
                                  │
       ┌──────────────────────────┤ steward tend ──► reconcile, spawn, reap ──► ●●●
       │                          │      ...                                     │
       │  ◄── steward notify ─────┤ "47 samples hit the same rate limit"          │
       │                          │                                              │
       └── "yes, invalidate" ────►│ records ruling, re-runs ────────────────────► ●●
                                  │      ...
                                  │ steward tend ──► all tasks done, scan drains
                                  │      ...        scan finds a bad grader ──► anomaly
       │  ◄── steward notify ─────┤ "resolved: 998/1000, 2 accepted-as-errored"
       │
       └── steward signoff ──────────────────────► attested, terminal journal entry

  steward status / inspect view
```

## 5. `steward init` — the deliverable is a directory

The output of `init` is a workspace that a human and an agent co-inhabit, and that a *third* party can pick up cold. That framing is doing real work: it means everything important has to be written down rather than held in someone's session.

```
my-sweep/
  AGENTS.md          # authored — bootstrap: "you are tending a run; read the runbook"
  CLAUDE.md          # authored — symlink to AGENTS.md
  policy.md          # authored — this human's standing rules
  evalset.py         # authored — scaffolded by `init --type evalset` (or flow)

  journal.jsonl      # DURABLE — append-only event log; the source of truth
  scanning.md        # DURABLE — agent-authored, per task; what the scans found
  analysis.md        # DURABLE — agent-authored, per task; what any of it meant
  anomalies.md       # rendered — caveats that reached the final data
  status.md          # rendered by every tend
  steward.log        # rendered — whether the machinery worked; bounded, disposable
  logs/              # the flat log directory — the CURRENT definition's results
                     #   ...and where scanning.md / analysis.md are mirrored
  logs-archive/      # DURABLE — superseded, removed, and failed logs; never deleted

  .steward/          # DISPOSABLE — claim, manifest, inflight.jsonl, caches
```

`logs-archive/` is a sibling rather than a child of `logs/` because log discovery recurses by default; nesting it would hide nothing. Both are gitignored for the same reason — `.eval` files are large archives, and outputs rather than source.

`init --type evalset|flow|hawk` places an **empty definition placeholder** — the type chooses the filename (`evalset.py`, `flow.yaml`, `hawk.yaml`) and nothing more. An earlier draft had it scaffold a starter definition "so a new directory is runnable rather than a set of empty conventions"; that lost, because a generated example is a guess at what is being measured and has to be deleted before it can be useful. What a genuinely good starting point contains is a question worth answering on its own, and until it is, a placeholder says where the definition goes without pretending to be one. `init` still accepts an existing definition and merely wraps it — the contract is deliberately "any program culminating in one `eval_set()` call", and `init` should not imply otherwise.

**The definition is found by name, not by inspection.** `.gitignore`-style discovery over the three conventional filenames, rather than `detect_definition_type`, which reads the file: an *empty* `.yaml` validates as both a flow spec and a hawk config and would be reported ambiguous. Content-based detection happens at `tasks` and `launch`, by which point there is content.

**`init` only ever creates.** Everything authored in a workspace is someone's work, so a second run reports each path as created or kept rather than restoring a pristine copy over it. `.gitignore` is the single exception and gains missing entries idempotently, because those entries are Steward's rather than the author's.

### 5.1 Three categories, and the one that matters

The obvious split — "human-readable top level, machine-owned `.steward/`" — conflates *who writes a file* with *whether it can be recovered*, and rulings are the case that breaks it. A ruling and its reasoning exist nowhere else: not in the logs, not in the manifest, not derivable from anything. So the categories are:

| category | examples | if you delete it |
|---|---|---|
| **authored** | `policy.md`, `AGENTS.md`, the definition | the human's own work is gone |
| **durable machine state** | `journal.jsonl`, `logs/`, `logs-archive/` | the audit trail, or the results, are gone |
| **authored by the agent** | `scanning.md`, `analysis.md` | the investigation is gone, and it re-runs from scratch |
| **disposable machine state** | everything in `.steward/`, `status.md`, `anomalies.md`, `steward.log` | rebuilt on the next tend, or simply gone with no loss |

The fourth category is the newest and easy to mislabel as rendered. `scanning.md` and `analysis.md` are folded from the journal and the scan results *in part*, but which findings were noteworthy is judgement, and no replay recovers it — so they sit with the journal on the durable side and are mirrored into the log directory, which is the half of a run that outlives the workspace.

`journal.jsonl` therefore sits at the top level, beside the authored files, where a file nobody can regenerate belongs. Nothing in `.steward/` is irreplaceable, which makes it disposable — a property worth more than a tidy listing.

**The journal is also what marks a directory as a workspace**, and it is the only file that can be. `.steward/` is disposable, so its absence proves nothing; a definition can sit in any directory; and `_steward.yaml` is optional, so its absence proves nothing either. The journal is durable, Steward-specific, and present for exactly as long as the workspace is — which is why `init` opens it with a real `initialized` event rather than leaving it until the first launch. An empty file would have served as a marker, but a record of when the workspace came into being is the same cost and is worth having, and it means the account of what happened starts where the workspace does.

**Losing `.steward/` mostly fails in the safe direction.** Anomalies re-derive from the log directory, since the errored samples are right there; the manifest is re-captured from the definition; in-flight records are rebuilt from the logs. Nothing durable is lost, because the rulings — the part that could not be recovered — are not in there.

**The exception is workers that are starting**, and it is worth stating because "disposable" invites deleting the directory at any moment. A worker is invisible to both the log directory and control discovery until its eval actually begins, which is after everything its definition does on the way to `eval_set()` — a second or two for a script, about a second more for a frontend, and unbounded for a Hawk config that installs packages into a cold environment (execution.md, *Worker model*). Steward normally covers that window by looking for the worker's own selection document in the process table, and deleting `.steward/` takes that away too: the next tend finds a task with no log, no live match, and no record, and spawns it again. Both workers land a log under different task ids, so the duplicate reads as an ordinary retry rather than as an error. What is lost is money and directory clarity, not a result. Still: delete it when nothing is starting, not as a reflex.

The usual reason to delete a state directory — a stuck claim — does not arise here. The claim is a kernel lock held only for the seconds a tend runs: a tend that died released it on the way out whether it meant to or not, and one that is wedged is killed and its claim taken by the tend that finds it (execution.md, *What enforces single-writer*). There is deliberately no `steward unclaim`, and deleting the file by hand is worse than useless: the lock lives in the kernel, attached to the inode, so unlinking the file leaves the holder holding it and lets the next tend create a fresh file, lock *that*, and run concurrently. Nothing can detect this from the new file's side, because the old inode is no longer reachable by name. So it belongs with the starting-worker case above and takes the same rule — delete `.steward/` when nothing is running, not as a reflex.

### 5.2 Why there is no `journal.md`

The obvious companion to the log is a rendered markdown version, committed so the record is browsable. It was considered and dropped, because what it buys is narrow and what it costs is not.

What it buys is **readability for someone who cannot run a command** — a reviewer in a pull request or browsing the repo. That case is real but speculative; nothing yet says anyone will review an eval journal in a browser. Everything else is already covered: `jq -r '.reasoning' journal.jsonl` serves a technical reader with no Steward installed, and append-only JSONL **diffs cleanly** — every commit is pure additions with no context churn, so a diff shows exactly what happened between two points even though each line reads badly.

What it costs is a standing obligation: a generated-file header, a never-hand-edit rule, a gitignore decision, "which of these two do I read", and the render code itself — grouping, evidence summarization, formatting — which is real implementation surface and a real source of bugs.

**The decisive argument is reversibility.** Adding the file later is nearly free; withdrawing an artifact people have linked to is not. The trigger for revisiting is concrete rather than a guess: someone actually asking to read a journal in a browser.

In git, `.steward/` is ignored and `journal.jsonl` is committed, so cloning the directory carries the account of what happened without dragging along one machine's claim and in-flight state.

There is also no `steward journal` command, for a related reason. What an agent needs from the record is not a rendering but a **query** — "have we ruled on this class before?" — and that should not be something it must remember to ask. See *Precedent travels with the anomaly*.

### 5.3 The alternative that looked best and is not

A single markdown file with YAML front-matter per entry — structured fields above, narrative below — is the most attractive rejected option, and it wins on two counts worth naming. Drift becomes structurally impossible rather than merely unlikely, and prose sits **with** the data it describes instead of at a distance from it. It would also make the journal co-writable: prose is inert to the fold, so a human could annotate an entry with no risk of breaking anything.

It fails on one property, and the failure is not recoverable by care: **line-delimited formats fail locally; block-delimited formats fail globally.** A corrupt JSONL line costs one entry, is detectable, and can be reported. A missing or mistyped `---` does not cost one entry — it merges two, or swallows the remainder of the file, and a crash mid-append leaves an unterminated block that absorbs everything written after it. For a file whose fold decides whether Steward re-asks a human or calls a run finished, a failure that cascades past its own record is disqualifying in a way a local one is not.

(YAML's coercion of `no` and version-like strings is a lesser hazard and a controllable one, since Steward writes the file. And a `---` immediately after a paragraph makes it an H2 in CommonMark, so the delimiter is both load-bearing and ambiguous.)

Its co-writability is worth noting but not worth building for: the need for human annotation was a property of *that* design rather than a requirement anyone stated. If it turns out to be wanted, appending a narrative event to the journal is a small addition at any time.

### 5.4 The one file Steward must never write

`status.md`, `anomalies.md`, and `steward.log` are generated, carry a header, and are expendable. `policy.md` is its counterexample, and the reason the line is worth drawing visibly in the directory listing: it is the human's own document, and the one thing Steward only ever *proposes* changes to.

### 5.5 State is a fold over the journal

Given an append-only event log in `journal.jsonl`, the rest follows the discipline execution.md already established for the reconcile core — *the supervisor is a cache, never a source of truth*:

```
reconcile(manifest, inflight, log_dir)   -> actions, summary     # execution.md
fold(journal.jsonl)                      -> anomaly state        # this document
```

Anomaly state — what is open, what was proposed, what was ruled and how — is a **pure fold over the journal**, not a separately maintained file. Any `anomalies.json` is a cache of that fold, living in `.steward/` with the rest of the disposable state. The property this buys is the same one that made reconcile worth writing as a pure function: crash recovery for adjudication state is the normal code path, exercised on every tend rather than in a rescue routine nobody tests.

### 5.6 The journal records observations, not only decisions

A journal of rulings alone would be too thin, and the reason is a claim made elsewhere in this design that is otherwise false. [execution.md](execution.md) says the agent watches a run as a time series — ramped here, pulled back there, this class started at 11pm and grew. **An agent's own memory does not survive a session boundary, and there are several of those in a night.** If the series is not written down it does not exist, and the 6am agent inherits a list of open items with no idea which are getting worse.

So each tend appends **what it observed**, not just what it decided. The cost is bounded and small: roughly sixty records over a night, each a few hundred bytes, against a fold that runs on every tend anyway.

**Machinery goes somewhere else.** A failed tend, a spawn error, a sync timeout are records of the runner working or not, and mixing them with the record of decisions makes both harder to read. Those go to Steward's operational log; the journal stays the record of *what was seen and what was decided about it*. The split is by subject, not by observation-versus-decision — an observation about an anomaly belongs in the journal.

Event types, all sharing a `ts` (UTC ISO-8601) and `type` envelope:

| type | carries | written by |
|---|---|---|
| `initialized` | the workspace exists, and which definition filename it expects | `init`, once |
| `observation` | per-class instance counts, task states, concurrency settings in force, rate-limit episodes since the last tend | every tend |
| `collected` | an agent has read and acted on everything up to this point — the high-water mark that makes "uncollected" computable | the agent, on attach and as it works |
| `instance` | one new instance joining a class — sample or task ids, log location, error detail | as observed |
| `opened` | a class seen for the first time — its computed key and first evidence | as observed |
| `proposal` | the classes it covers, the action, the evidence, and the precedent behind it | the agent |
| `ruling` | the decision, its reasoning, and who made it — **one record per class**, even when made against a group | the human, via the agent |
| `action` | what was actually done — requeue, respawn, archive, pause — and its outcome | Steward |
| `resolution` | what became of it: re-ran and passed, re-ran and failed again, accepted | as observed |
| `signoff` | terminal; who, when, the manifest digest, the exceptions accepted | the human |

**An event type a reader does not recognise is data, not an error.** The vocabulary above grows as Steward does, and a workspace outlives the version that wrote it — so a reader yields an unknown type as a generic event rather than refusing the file. This is deliberately the opposite of the selection document, which forbids unknown fields ([execution.md](execution.md), *Changes required in inspect_ai*): a selection is **input**, validated before it changes what runs, where a silent misreading costs a wrong run; a journal is **history**, where refusing to read costs the record itself. Same reasoning that puts `identifier_version` in the manifest — anything persisted has to survive the next upgrade.

**Reasoning stays free text, and that turns out not to be the problem it looked like.** The worry was that prose cannot be matched against, so precedent lookup would need it constrained. It does not: **precedent keys on the class, not on the prose.** "You ruled on this class twice before" is a structural query over `ruling` records, and the reasoning is what a person then *reads* to decide whether the precedent applies. Constraining it would cost the only field that carries why, in exchange for a lookup that does not need it.

Two consequences worth naming. Rulings recorded per class are what let a group decision be unpicked later — the fold sees twelve rulings sharing a proposal id, not one ruling over twelve things. And because every event is timestamped and appended, "this class started at 11pm and tripled by 2am" is a query, which is what makes the weight-based re-notification above computable at all.


### 5.7 `steward.log` — whether Steward itself worked

The journal is the record of the *eval set*: what was observed about it and what was decided. A tend that crashed, a spawn that failed, a sync that timed out, a ticker restarted, a claim found stale and reaped — none of these is a fact about the eval, and they go to `steward.log` instead.

**A second file needs a real justification, because the obvious alternative is cheaper.** The journal already carries a `type` on every event and is already consumed by a fold that ignores types it does not care about, so machinery events could simply be more types. One file, one append path, one thing to sync. Legibility is *not* the argument against that: the journal is JSONL read by a fold, and `analysis.md` and `anomalies.md` are the human-facing renderings.

**The argument is durability.** `journal.jsonl` is committed to git — that is where *A small integrity bonus falls out* comes from, and it is the file the design calls irreplaceable. Appending a spawn error and a sync result every ten minutes would bloat a record meant to be reviewed, and would dirty the working tree on a cadence, which is the thing that makes `revision.dirty` useless in a Steward workspace to begin with. Machinery is high-volume, low-value, and disposable; decisions are the opposite. They want different lifetimes, so they get different files.

So they go to `steward.log`, and the rule is a single question:

> Is this a fact about **the eval set**, or about **Steward**? The first goes to the journal, the second to `steward.log`.

It sits at the top level and is **disposable**, which is exactly the shape `status.md` already has — top-level so the sync carries it out without an exception to the deny list, disposable because nothing in it is irrecoverable. Being disposable also settles the growth question: an append-only log needs a bound, and truncating the oldest entries is free when nothing depends on them.

**The split turns out to answer a question neither record could answer alone.** A successful tend writes an `observation` to the journal, so *"no tend for four hours"* is computable from the journal's silence — and *why* is in `steward.log`, where the failures went. Had the two shared a file, a run whose tends were crashing would look like a run with nothing to report.

One honest limit: the conditions most worth logging are the ones most likely to prevent logging. A full disk fails the `steward.log` write along with everything else, which is why the substrate failures in [execution.md](execution.md) escalate through the notification channel rather than relying on a file.

### 5.8 `init` and version control

Where version control is available, `journal.jsonl` survives and is reviewable because it is committed — but nothing so far makes that happen, so `init` takes care of it. (On machines with no git or no network, the S3 sync described under *Syncing the workspace out* plays this role instead; `init` treats a missing git as an ordinary condition, not an error.)

- **`git init` if, and only if, there is no repository already.** Detection walks up from the directory: a Steward workspace created inside an existing project (`evals/sweep-2026-08/` in a monorepo) belongs to that repository, and initialising a nested one there is a footgun rather than a convenience. Announce it when it happens, and allow `--no-git` for people who mean it.
- **Write a local `.gitignore` either way**, listing `.steward/`, `logs/`, and `logs-archive/`. Scoping the rules to a nested `.gitignore` means Steward never edits a file it does not own — the parent project's own ignore rules stay untouched. Append missing entries idempotently if one already exists.

`logs/` and `logs-archive/` are ignored because `.eval` files are large archives, they are outputs rather than source, and they are shared through a log server or object store rather than through git — ignored, but *durable*: gitignored is not the same category as disposable, and only `.steward/` is safe to remove. `.steward/` is ignored because it is disposable by construction.

**A small integrity bonus falls out.** Committing an append-only journal gives a second, independent record of when each decision was made — git's own metadata — and any edit to a past entry shows up in review as a *modification* rather than an append. It does not make the record tamper-proof, but it makes tampering visible in the course of ordinary review, which is most of the value.

### 5.9 A config file may not say anything the definition can

The original position here was that there is **no** config file at all, and most of it survives. Every candidate belonged somewhere else:

| candidate | where it actually goes |
|---|---|
| definition pointer | discovered (`evalset.py` / `flow.yaml` in the directory) |
| `log_dir` | defaults to `logs/` |
| notification channel | `INSPECT_EVAL_NOTIFICATION` — reference-only *by design*, so a config file is the wrong home |
| log-reuse store location | `INSPECT_STEWARD_STORE` — a machine-level resource shared across projects, not a property of one |
| whether to publish to it | a `signoff` decision, defaulting from `policy.md` — publishing a result for others to reuse is an attestation, not a setting |
| eval configuration | the definition, which configuration.md establishes as the single source of truth |

What is left over is mostly per-run and belongs on the command line — `--smoke`, `--accept-archive`. A config file is for settings you re-apply across many invocations; a run is launched once. (An earlier draft made the case with `--max-spend`; see *Spend is not Steward's to manage* for why that argument lost its example.)

**Two rows did not survive, and they are why `_steward.yaml` exists** ([plan.md](plan.md) step 15). *Tend interval* was routed here to "the runbook, and the agent's scheduling", which does not hold: the runbook is prose, while cron and the ticker fallback both need a number, so it arrives as a `launch` flag — a standing property of a workspace showing up as a per-launch argument, which is the thing this section concedes config files are *for*. And **fleet shape** was never in the table, because until there was a choice about how tasks divide across processes there was nothing to express. Both affect Steward and neither is anything Inspect has an opinion about.

**The drift objection is what the rule answers.** configuration.md spends its length establishing that the definition is the single source of truth for what an eval set *is*, and a second file beside it is precisely where a contradicting `log_dir` or `model` ends up. So the file may express only what the definition **cannot** — and the keys the definition owns are refused by name, with a message saying where they belong, rather than ignored. Unknown keys are rejected outright. That is the same posture a selection document takes and for the same reason: this is input, not history. Policing by schema is cheap; policing by discipline is what was being avoided.

The underscore sorts it to the top of a directory listing, beside `AGENTS.md`.

## 6. Mechanics and policy are different documents

This is the distinction I most want to get right, because conflating them causes both of the obvious failure modes.

| | `steward runbook` (a command) | `policy.md` (a file) |
|---|---|---|
| answers | how Steward works | what this human wants |
| owner | the package | the user |
| lifetime | ships with the version | lives with the project |
| example | "call `tend` every 10 min; never block on a human" | "never spend over $200 without asking; sandbox timeouts in this eval are expected" |

Making the runbook a *command* rather than a file solves version skew: instructions and implementation ship together, so an agent can never follow last year's runbook against this year's CLI. Making policy a *file* lets it be edited, reviewed, and version-controlled by the person whose standards it encodes.

`AGENTS.md` is the thin bootstrap that points at both. It is the only thing that has to be discovered by convention.

### 6.1 A ruling is not a policy

`policy.md` grows over the life of a project, but **Steward should not write to it.** The distinction that matters: a *ruling* is a human's decision about one situation, with all of that situation's context behind it. A *policy* is a standing rule for every future situation of that shape. Promoting the first into the second is itself a judgement, and it is the human's.

Auto-promotion would quietly convert "invalidate those 47 rate-limit errors from the 15:40 outage" into "always invalidate rate-limit errors" — which is a different and much larger claim, and one the human never made. For a tool whose output is evaluation results, silently widening the human's standards is close to the worst failure available.

So the division is:

- **`journal.jsonl`** — every ruling, machine-written, with reasoning and evidence. Append-only, and the only copy.
- **`policy.md`** — standing rules, human-authored. Steward *proposes*: "you have ruled the same way on this class three times; add it to policy?" The human accepts, edits, or declines.

The accumulation benefit survives, and authorship stays where it belongs. It also gives the workflow a legible success metric: **interruptions per run, trending down.**

### 6.2 The journal, and a naming collision to fix

`journal.jsonl` is the append-only event log — launches, anomalies observed, proposals, rulings, resolutions, completion. It is what answers "what happened here, and who decided what" a year later, and the only file in the directory that nothing else can reconstruct.

Two files named *journal* would be one too many. execution.md currently uses the word for the append-only `intent`/`launched`/`exited` process records, which are ephemeral, machine-owned, and explicitly reconstructible from the log directory — the opposite of durable, and confusingly so given they are also append-only JSONL. **Proposal: rename that one** to say what it tracks (`.steward/inflight.jsonl`), leaving *journal* to mean the durable record, which is what the word means to everyone else.

The two then differ in what they are for, which is why they sit in different places: `inflight.jsonl` is a **performance cache** over the log directory, discardable at any moment, while `journal.jsonl` is the **record**, and the only file in the directory that cannot be rebuilt.

## 7. `steward launch`

Takes the claim, captures the manifest, writes the initial state, spawns the first workers, **returns**.

It must not block: the caller is an agent that has to go on and schedule tends, and a blocking start would trap it. `run` is the wrong word for that — it says *you* are doing the running, and invites the reader to expect a foreground process. `launch` says the opposite, and says it without a footnote: you set something in motion and it goes on without you.

It also completes a coherent verb family. **launch → tend → status** are all words that presuppose the thing has its own life; you launch a ship and then tend it. `run → tend` would have been two different metaphors bolted together. The verbs now teach the model that matters most: *the run is a live thing you look after, not a program you are executing.*

The one thing lost is that `steward run` paired cutely with discovering `run.py`. That was never worth much — file discovery does not need to echo the verb.

### 7.1 Smoke first

Standing practice before an expensive sweep is a **smoke run**: a couple of samples per task under a wall-clock cap, to find out whether the thing works at all before committing real money to it. `steward launch --smoke` makes that a flag rather than a ritual people reconstruct by hand, and the runbook names it as the default first step.

Defaults of two samples per task and a fifteen-minute cap match how this is done in practice. Neither is magic; the point is that both are *bounded*, so a broken definition costs minutes instead of a night.

**Smoke logs go to `.steward/smoke/`, on local disk.** Two-sample truncated logs written into `logs/` would be indistinguishable from real results to `samples_df`, to the viewer, and to anyone analysing the eval six months later — so they need their own directory regardless. Making it local rather than a sibling of `log_dir` matters because **`log_dir` is frequently S3**, and a rehearsal has no business writing throwaway objects to a bucket: slower, billable, and leaving junk that needs lifecycle rules to clear. Local disk is free, fast, and already where disposable state lives.

Putting it under `.steward/` also means the cleanup question answers itself. It is disposable state by construction, so it needs no special rule: each smoke clears the previous one, a failed smoke's logs stay put for as long as anyone wants to read them, and everything goes when `.steward/` goes. `inspect view --log-dir .steward/smoke` works on it like any other directory.

Not deleted the moment a smoke passes, though — reading a transcript or two after a green run is a real practice, and the point of a smoke is partly to check that the agent is doing something sensible rather than merely something that terminates.

**What it catches** is most of what actually goes wrong before anything interesting does: a definition that will not import, a model name or key that is wrong, a sandbox image that will not start, a scorer that throws, a grader container that falls over. All of them are cheap to find at two samples and expensive to find at five thousand.

**It also measures token usage rather than estimating it.** Tokens per sample across the smoke extrapolate to the real run, which is a far better answer to "how big is this" than counting tasks — and it arrives exactly when someone is deciding whether to commit to it. An earlier draft said *cost*; Inspect reports dollars only where the definition supplied prices, and Steward does not supply them (see *Spend is not Steward's to manage*). Nor does the smoke measure a *rate* — two samples per task on a small pool says nothing about fleet throughput, so completion-time projections come from the live run's first completed samples, not from here.

**A smoke is valid for a manifest, not for a directory.** Whether a smoke still applies is answered by the capture manifest: if the definition changed, its task identifiers changed, and the smoke is stale. That gives `launch` a precise check to warn on — "no passing smoke for this manifest" — without needing to guess at what edits matter. Warn rather than refuse: re-launching after a fix, or resuming, are both legitimate reasons to skip it, and a hard gate would only teach people to bypass it.

Almost nothing about a smoke needs special machinery. It is a run with a sample limit, a time cap, and a different log directory — launched, tended, and reported like any other, then recorded in the journal as part of the same story. The cap is the one exception, for the reason below.

**Half its upstream dependency has landed.** Redirecting a definition's `log_dir` needs an override the definition cannot pre-empt, which no environment variable can supply — `eval_set()` declares `log_dir` with no default, so every definition passes it explicitly and `INSPECT_LOG_DIR` always loses. Selection documents carry that override in their `overrides` container (execution.md, item 4), so the log-directory half works for script definitions as well as Flow ones.

**The sample slice does not, and a name collision is what hid it: `max_samples` is concurrency, not a limit.** The override that landed beside `log_dir` bounds how many of a task's samples run *at once*; a smoke needs `eval_set()`'s separate `limit`, which bounds how many run *at all*. Nothing in the selection document supplies that today — see execution.md, item 8, where it is one more entry in the same container for the same reason the others were: `task_identifier` hashes a task's *execution* limits (message, token, turn, time, working, cost) and not its dataset slice, so truncating a worker's dataset cannot desynchronize it from the capture manifest.

**The time cap is Steward's to enforce, and this is where the obvious implementation is wrong.** `time_limit` *is* in the identifier, so passing one through would change every task's identity and break selection matching against the very manifest the smoke was captured from. The fifteen minutes is therefore a wall-clock deadline Steward applies by stopping the smoke's workers — which is what it had to be regardless, since the cap bounds the *rehearsal* rather than each sample within it.

## 8. The tend loop

The agent schedules `steward tend` every ~10 minutes (see execution.md, *The reconcile core and its drivers*). Each tend:

1. reconciles — spawns what should be running, reaps what died, requeues within policy
2. rewrites `status.md`
3. returns a compact structured summary to the agent
4. **never blocks** — everything long-running is a detached child

The agent reads the summary and decides whether anything warrants a human. Most tends warrant nothing and should produce no output beyond the file rewrite.

**One thing in the summary always warrants a human: a parked worker.** A worker that has stopped on a tool approval or an `ask_user` is alive, holding its slot, and waiting for a person — nobody else may answer ([agent.md](agent.md), *What the agent may do without asking*). Reconcile learns which workers are parked from the same control-channel read it already performs for liveness ([execution.md](execution.md), *The parked worker*), so this costs no extra work per tend. Both `status.md` and the summary list them by task, with the command that attaches to one:

```
blocked: 2 workers waiting on a human
  mbpp/gpt-5            approval  bash          4h12m   inspect acp --eval-id <id>
  swebench/opus-5 (e2)  question                  38m   inspect acp --eval-id <id>
```

That command is also the general answer to *a detached run cannot be watched live*: any worker can be attached to and observed, not only a parked one.

## 9. Syncing the workspace out

Some runs happen on machines with no git, and sometimes no internet at all. The deployment worth designing for has exactly two pipes out: a localhost model endpoint that proxies, and an S3 bucket. On such a machine **the bucket is the only observability channel there is** — the alternative to syncing is shelling into the runner, which is precisely what an unattended overnight job should not require.

So each tend mirrors the workspace's top-level files to object storage. Someone watching from another system reads `status.md` for progress, `analysis.md` for what has been found and what it means, `anomalies.md` for accumulating caveats, `journal.jsonl` for what has been decided, `steward.log` for whether the machinery is working, and the definition and `policy.md` for what is being run and under what rules — without touching the runner.

It also completes the picture in one place: when `log_dir` is in the same bucket, the remote reader has the logs and the run's state side by side, and `inspect view` works against the same prefix.

### 9.1 The policy is exclusionary

The point of syncing is to carry out artifacts nobody predicted — an analysis an agent wrote, a note a human left, a report a scaffold generated. An allow-list defeats that by construction: anything unanticipated is silently left behind, which is the failure you notice last and regret most. So everything at the top level goes, minus a short deny list:

| excluded | why |
|---|---|
| directories | `logs/` syncs by other means (or *is* the target); `.steward/` is disposable machine state |
| dotfiles | keeps `.gitignore` out, and more importantly keeps a stray `.env` from being pushed to a bucket |
| `AGENTS.md`, `CLAUDE.md` | agent bootstrap, static and meaningless to a remote reader |

The `.env` case deserves the explicit rule rather than an incidental one. Syncing is an **outbound data flow**, and a workspace is a directory humans put things in.

A pleasant consequence of exclusionary-by-default: if a rendered `journal.md` is ever added, or a report, or anything else, it flows out with no configuration change.

### 9.2 What actually leaves, and why `.env` is not the whole of it

The `.env` rule is correct and it is the easy case. The harder one is that the files being synced *on purpose* carry material lifted out of transcripts:

- **`journal.jsonl`** holds error text and evidence — tracebacks, model output, sandbox paths, dataset content, and occasionally a credential that a library helpfully interpolated into an exception message.
- **`analysis.md` and `scanning.md`** are an agent's writeup of what transcripts contained, and a good writeup quotes.
- **`anomalies.md` and `status.md`** carry error text wherever it is the clearest way to say what happened.

Usually the sync target is the same bucket as `log_dir`, which bounds the audience to the one that already has the transcripts — so this is not a new *audience*. **It is a large change in accessibility.** An `.eval` file is a zip that needs tooling to read; `journal.jsonl` is plain text that anyone with bucket read access can grep. The same information becomes far easier to extract, and easy extraction is what turns a theoretical exposure into a real one.

**Steward does not attempt to redact.** A redactor that catches most secrets is worse than none, because it converts "this contains transcript material, treat it accordingly" into an implied guarantee that it does not. Three things instead, all cheap:

- **Say it plainly**: the synced workspace carries transcript-derived content and deserves the access controls the transcripts deserve. When the sync target is not the log bucket, that is a decision someone should make deliberately rather than inherit.
- **Bound the volume.** Error text in a journal event is truncated to a stated length with a pointer to the log, which a full traceback badly needs anyway — the journal is read by a fold on every tend, and an untruncated stack trace is both a size problem and gratuitous surface.
- **Keep the deny list.** Dotfiles and directories stay excluded, for the reason above and because the failure of an allow-list is silent.

### 9.3 It must never raise

The sync is advisory. It is important — on an air-gapped runner it is the whole window — but it is not on the critical path of producing results, and an eval must never fail because a bucket was briefly unreachable. So: bounded timeout, failures caught and recorded in `steward.log`, tend proceeds regardless — a sync failure is a fact about the machinery, not about the eval set. This also keeps it consistent with *a tend spawns and reaps; it never does long work itself* — a handful of small files with a timeout, not an unbounded transfer.

**A failed sync is invisible from the far end**, which is the awkward part: the remote reader sees a `status.md` that simply stopped changing, and cannot tell a stalled run from a broken pipe. That promotes the status timestamp from a nicety to load-bearing — a remote observer detects sync failure precisely by noticing the file is old. It should say its age plainly rather than merely carrying a timestamp to be compared.

Note the recursion, and that it resolves: the record of *why* the sync failed is in `steward.log`, which the sync is what would have carried out. So a remote reader sees staleness and nothing else, and diagnosing it means reaching the machine. That is acceptable because the alternative — a second, independent channel purely for reporting the first one's failure — costs more than it is worth for a monitoring pipe, and because the ages in `status.md` already distinguish the two failures that matter ([execution.md](execution.md), *Clocks*).

The sync is **outbound only**. Editing `policy.md` in the bucket does not change the run; two-way sync would need conflict resolution nobody wants for a monitoring channel.

### 9.4 What this means for git

The durability argument earlier leans on version control, and these machines have none. Neither mechanism is universal, so they are alternatives rather than a stack:

| environment | how the record leaves the machine |
|---|---|
| ordinary workstation | git, as described under *`init` and version control* |
| air-gapped runner | this sync — the bucket is both durability and observability |

`init` must therefore treat git as unavailable-and-fine rather than an error, and the sync target should default to the parent prefix of `log_dir` when that is remote, so the common case needs no configuration.

One conclusion this does *not* overturn, though it dents its reasoning: *why there is no `journal.md`* argued partly that append-only JSONL diffs cleanly in git, serving the technical reader. That argument is worth nothing here. The decision still stands on reversibility — adding the render later is cheap, and the exclusionary sync would carry it out automatically — but if remote readers turn out to want a rendered journal, this is the environment that will ask for it.

## 10. Resource allocation

Two resources gate a run, and only one of them needs Steward's attention.

**Connections are already handled.** Inspect's adaptive connections — on by default, ceiling of 100 — discover a provider's throughput on their own and back off when they reach it. Left alone, they do the right thing, and nothing in this section is about improving on them.

**Sample concurrency is the one that costs money.** A sample waiting on a model-API semaphore is not free: it holds its sandbox container, its memory, and its slot in the event loop the whole time it waits. Concurrency set far above what the provider will actually serve means hundreds of samples sitting on EC2 consuming compute and doing nothing. The right level is roughly *the concurrency the provider actually supports* — not a number anyone can look up, since it varies by tier, by model, and by time of day.

### 10.1 Setting `max_samples` explicitly is what makes it a knob

Three paths produce a task's sample semaphore, and which one you land on decides whether Steward can steer at all:

| condition | limiter | retunable? |
|---|---|---|
| `max_samples` set explicitly | `ResizableLimiter` | **yes** |
| unset, adaptive connections active | `DynamicSampleLimiter` | no — tracks the model's controller |
| unset, adaptive off | `ResizableLimiter`, defaulted from `max_connections` | yes |

**Explicit `max_samples` wins over adaptive, silently and deliberately.** Leaving it unset hands sample concurrency to the model's connection controller, which grows it as the provider allows — excellent inside one process, and not something Steward can adjust.

That settles the scaffolding question with a second, stronger reason. `steward init` should write an explicit `max_samples=` into the definition not only so the author thinks about it, but because **an explicit value is the difference between a fleet Steward can coordinate and one it can only watch.**

### 10.2 What Steward actually has to solve

**Per-process adaptation is per-process, and Steward runs many.** Left to adapt independently, eight workers each discover headroom that is partly headroom the others have not claimed yet — so they all climb, collectively overshoot, and get rate-limited together. Each is confidently optimizing against a resource it believes it has to itself.

Nothing inside a worker can see the total. What reaches a provider is the sum across workers, and Steward is the only party that knows how many there are. Setting explicit per-worker limits is therefore not merely a way to make the user think — it is how the budget becomes **deterministic** rather than emergent, and Steward owns both factors: how many workers run, and each one's share.

The division trades against things already weighed here. Fewer, larger workers pay less per-worker startup cost (roughly a second for Flow definitions, on every spawn); more, smaller workers give finer scheduling granularity and less slot idle between tends.

### 10.3 There is no single budget — there are two, with different shapes

Tasks in one eval set often run against different models, and throughput varies enormously between them: a high-tier hosted provider and a single-GPU local vLLM are not the same resource and do not compete for the same thing. A uniform per-worker limit is then wrong in both directions at once — too high for the constrained model, whose samples pile up waiting, and too low for the fast one, which sits underused.

So the budget splits along the same line the two-signals argument already draws:

| budget | scope | who competes |
|---|---|---|
| **throughput** | one per rate-limit bucket | only tasks sharing that bucket |
| **local compute** | genuinely global | **every** task, whatever model it uses |

Throughput is partitioned; sandboxes, memory, and CPU are shared. Two workers on different providers do not contend for tokens at all, and contend fully for the host.

**The manifest already carries the grouping.** A task identifier includes its model, so Steward can partition tasks by model at enumeration time and size each group separately, without discovering the structure at runtime.

**The shape of the sweep decides which budget binds**, and the two common shapes sit at opposite extremes. A model comparison — one task across many models — has almost no throughput contention and is bound entirely by local compute. A task sweep on a single model is the reverse: local compute is usually ample and the provider is the wall. Recognizing which one is in front of it tells Steward which ceiling to manage.

Setting concurrency **per task rather than per fleet** follows naturally from the mechanism, since each worker runs one task in its own process. This used to carry a caveat — batching several short tasks into one worker would make the process's single limit wrong for whichever of them used a different model — but [scheduling.md](scheduling.md) rules batching out entirely, so one process serves one task on one model and the limit is always about exactly one thing.

Two caveats keep this from being exact. A rate-limit bucket is not quite a model — several models can share an account's quota, so grouping by model over-partitions. And a task may consume more than one provider: a grader model, or an agent calling a different model for subtasks, neither of which appears in the identifier. Steward can see a task's primary model and not its full appetite.

### 10.4 The levers

All of these are live-tunable through the control channel — `GET`/`PATCH /tasks/<task-id>/config` for task-scoped knobs, `GET`/`PATCH /config` for process-global ones — so tuning reaches **running** workers, not just newly spawned ones.

| lever | scope | what it bounds |
|---|---|---|
| `max_samples` | task | sample concurrency, given an explicit setpoint |
| `max_sandboxes` | process | sandbox concurrency — the lever for the *local* ceiling |
| `max_connections` | process | the connection pool, and the adaptive controllers' scaling ceiling |

`max_sandboxes` is the one that matters under Docker, because it bounds compute directly rather than by proxy.

One detail worth relying on: **semaphores are task-scoped, not attempt-scoped.** A retune survives an in-process retry rather than silently reverting to the definition's value, so a worker Steward has tuned stays tuned.

### 10.5 The ratchet is asymmetric, and the mechanism says so

This is not an inference about container lifetimes — it is how the limiters behave. **Lowering a limit below the current in-use count blocks new acquires until in-flight holders drain; it never preempts. Raising one lets work start immediately.**

So climbing is instant and descending is gradual. Undershooting costs wall-clock and recovers in minutes; overshooting commits compute that only releases as samples finish, and under Docker can thrash or OOM the host first. Ramp in shrinking increments, and stop short of anything that cannot be undone quickly.

**Overshoot has a partial repair, and knowing which half is fast matters.** Having climbed into rate limits, lowering `max_connections` clamps live connection concurrency down *at once* and the backoffs stop — that half is immediate. The sample side is not: those samples already hold their containers and memory, and the only way that releases is by finishing. So the correct first move on overshoot is the connection ceiling, which buys relief in seconds, followed by letting sample concurrency drain to a lower setpoint over minutes.

The provider-side damage is undoable in seconds; the compute-side commitment is only outlastable — and on a Docker host it can take the box down before it drains. That argues for shrinking increments, **not** for timidity, and the difference matters more than it first appears.

### 10.6 Over-scaling risks failure; under-scaling wastes time and often money

The asymmetry above is easy to read as "be cautious", which would be the wrong lesson. The tempting summary — over-scaling costs money, under-scaling costs wall clock — is half wrong, and the wrong half is the money.

**Token spend is invariant to concurrency.** The same samples make the same calls whether forty or a hundred and twenty run at once. Concurrency changes the *rate* of spend, never the total. Whatever over-scaling costs, it is not the model bill.

What is left is compute, and it depends on the regime:

| | over-scaling | under-scaling |
|---|---|---|
| **fixed host** (Docker, one EC2 box) | free — the box is paid for either way — until it OOMs, and then catastrophic | **pays for the box three times as long** |
| **elastic** (k8s) | rents capacity to hold blocked samples: real waste | more nodes for less time ≈ a wash, so wall clock dominates |

On a rented box the conservative choice is the *expensive* one. A job at a third of the viable concurrency runs three times as long on the same instance, for identical work — under-scaling costs both time and money there, and over-scaling costs nothing at all right up until it takes the host down.

So the two downsides are not commensurable, and pretending they are is what produces false caution: **over-scaling risks a failure; under-scaling guarantees a loss.** One is an unbounded availability risk with a low probability, the other is a certainty that compounds every hour. The genuine dollar cost of over-scaling is narrow — idle capacity on elastic infrastructure, plus re-run tokens if the overload actually breaks samples.

Set against that, **undershoot compounds**: running at 40 where 120 was available does not lose a little time, it triples the run. A job started at 10pm that could have scaled at 11pm and instead waits for a human until 8am has thrown away most of the night — and being useful during exactly those hours is the whole reason Steward exists.

(One place the *rate* matters even though the total does not: a run someone intends to stop early — because they are watching for a signal, or because a deadline is close — reaches the stopping point sooner when it runs fast, with less of the sweep covered. That is an argument about which work happens first, and it is answered by spawn order rather than by scaling ([scheduling.md](scheduling.md), *Spawn order transposes the crossing*).)

So delegating scaling to the agent is not a risk to be minimized, it is most of the value. Where the right action is clear — no pushback for half an hour, ample local headroom, well under the envelope — it should simply raise, and the escalation list above should be read as the **exceptions** it is rather than the default posture.

**Overnight is the best time to probe, not the worst.** Nobody is waiting on interactive latency, provider load is often lower so there is genuinely more headroom to find, and there are hours in which to recover from a bad probe. The instinct that unattended means careful is partly backwards. What should actually govern boldness is **time remaining, not supervision**: probing at 11pm with a night ahead is cheap, and the same probe at 4pm against a 5pm deadline is not.

### 10.7 Authorize at 10pm, do not interrogate at 3am

The cost of a question is not constant — it decays with the human's availability. Asked at launch it costs a sentence; asked at 3am it stalls the run until morning, which is the very outcome the escalation was meant to prevent.

So the agent should **front-load** the decisions while the human is present, and the smoke run is the natural moment because it has already measured throughput. One exchange at launch — *"smoke sustains about 50 concurrent, ETA six hours; push toward 100 if headroom appears?"* — is worth more than any number of well-judged 3am escalations, because it converts the whole night into standing authority.

This is the pre-authorization idea from *Notification is the gate on autonomy*, applied to scaling, and scaling is where it pays best: the questions are predictable, they can be asked before anything has gone wrong, and the answers stay valid all night.

### 10.8 Rate limits are the wrong signal for the local ceiling

Provider pushback says nothing about memory. A run can climb to 200 concurrent samples with no rate limits at all and still take the box down. Which constraint binds depends on where sandboxes run:

| sandbox | local gate | provider gate | consequence |
|---|---|---|---|
| Docker | **hard** — one host's memory and CPU | soft | the host is the ceiling; `max_sandboxes` is the lever |
| k8s | elastic | **binding** | the provider is the ceiling; let adaptive climb |
| none | slight (memory per sample) | **binding** | as k8s |

"Ramp until rate limits appear" is correct advice on k8s and a way to kill a laptop under Docker.

### 10.9 The signal exists

The config view carries an `adaptive` section reporting each controller's live limit, in-flight count, scaling bounds, and **recent scale changes** — so pushback is observable rather than inferred. Scale-downs across several workers at once are the signal that the fleet has collectively overshot, which is precisely the condition no individual worker can detect.

`PATCH` also supports `dry_run`, and applies what it can while warning about knobs that do not apply rather than failing the whole request — so Steward can probe before committing and tune several things at once without brittle error handling.

### 10.10 The envelope is policy; the tuning is the agent's job

The **ceiling** is a judgement call about infrastructure that only the user can make — how big the box is, whether the cluster scales, how much they are willing to have running at once. It belongs in `policy.md` or as a launch argument. Everything inside that envelope is the agent's to tune **without asking**, and doing so is one of its standing jobs rather than an exceptional intervention. The envelope exists precisely so that the agent can move freely inside it: start conservatively (40 concurrent samples is a reasonable default to scaffold), raise while pushback stays absent and local headroom holds, pull back when scale-downs cluster, rebalance across groups as workers finish.

All of that is observation and arithmetic. What it cannot settle is a short list, and the items on it are unclear for structural reasons rather than for want of data:

- **Attribution.** Scale-downs mean *someone* is at the limit — not necessarily us. Another workload on the same API key looks identical from inside a worker. So does memory pressure from another process on a shared host. Ramping into someone else's workload is worse than running slowly.
- **Risk appetite.** Faster-but-riskier against slower-but-safe has no observable right answer. Near a deadline a person may accept OOM risk they would never take overnight.
- **Scope, when the numbers come back badly.** If observed throughput implies forty hours instead of four, the useful question is not what to set concurrency to — it is whether to drop a model, cut epochs, or let it run anyway.

### 10.11 Escalate in the units the human thinks in

That last item generalizes into the rule that matters most here. **A human cannot usefully rule on whether `max_samples` should be 60 or 80.** They can rule immediately on "at current throughput this finishes at 3am rather than 9pm", or on "the sonnet arm is a third of the remaining work and it is the one erroring".

So the agent's tuning output is a projected completion time and a picture of what is left, not a concurrency number, and those are what a notification carries. Throughput measured on the live run makes this available within the first completed samples rather than three hours in, which is the difference between a decision and a rescue.

Tuning belongs in the record like anything else: each adjustment is a journal event, so "ramped to 80 at 14:10, scale-downs at 14:25, settled at 60" is reconstructible. Where the situation is genuinely unclear it becomes an **anomaly** and takes the ordinary lifecycle — open, investigating, proposed, ruled — rather than needing a parallel mechanism for resource questions.

### 10.12 Spend is not Steward's to manage

An earlier draft had `steward launch --max-spend 200`, a cap Steward would enforce, spend attributed per arm, and escalations denominated in dollars per hour. **None of that is built.** Steward does not track spend, does not project it, does not cap it, and takes no action on it.

This is a decision rather than a deferral, and four things argue it.

**Inspect ships no prices, so Steward would have to own them.** `ModelUsage.total_cost` is a real field, but it is populated only when `ModelInfo.cost` is set — and *no model in Inspect's bundled model data has one*. Prices arrive exclusively from the user, via `model_cost_config` or `set_model_cost()`, which Inspect makes explicit by refusing `cost_limit` outright when they are missing: *"Use set_model_cost() or --model-cost-config to configure pricing."* For Steward to report dollars it would have to carry a price table, or make every definition carry one — a per-provider surface that goes stale monthly, in exchange for a number that is an estimate either way.

**The cap could not do the job a cap is for.** Spend is observed after it is spent, and the observation cadence is the tend interval. A fleet at $40/hour spends around $7 between tends, so any cap is soft by construction — it cannot stop the spend that motivates wanting one, only notice it slightly late. A soft cap that reads as a hard one is worse than none.

**Enforcement would add a trigger, not a capability.** Everything a cap would do on firing already exists and is already reachable: `stop`, `pause`, archiving an arm, ruling on an anomaly. The only new thing is a threshold deciding *when* — and that decision has exactly the shape this document reserves for the human, since whether $200 of overrun matters depends on the deadline, the funder, and what the run is for.

**The real control is upstream of the run.** A manifest is a complete enumeration before anything executes — tasks × models × samples × epochs, known at `launch`. Someone who wants to spend less runs less, and they can see what they are committing to. What Steward contributes to not wasting money is not accounting but **noticing the thing that is silently broken at 2am**, which is failure adjudication. A run that burns $400 producing unusable results was not saved by a $500 cap.

**Where it does belong is the platform.** Inspect has a per-sample `cost_limit`, Hawk's config has `model_cost_config`, and an organization running many evals has billing at the account level. All three are better positioned than a per-project runner, and under Hawk the definition already supplies prices — so `total_cost` lands in those logs whether or not Steward looks at it.

**What survives is reporting, not management.** Token usage is already in every log header (`EvalStats.model_usage`, per model), and Steward reads those headers on every tend for other reasons. Surfacing usage in `status` costs nothing and requires no prices. The line is that Steward *reports what the logs say* and does not track, project, cap, or act on spend. Nothing in the design depends on more than that.

### 10.13 Tuning precedent is the most reusable kind

Accumulated rulings pay off more here than anywhere else in the design, because **a provider's throughput is broadly stable across runs in a way that error conditions are not.** A ruling about a transient outage is worth little next month; "this model on this account sustains about 50 concurrent" is worth a great deal, and stays true. After a few runs Steward should simply know it, and start there rather than rediscovering it by ramping from 40 every time.

That is the first thing in this design that wants to persist **outside the workspace**. Throughput is a property of a model and an account, not of one sweep, and `journal.jsonl` is per-project. Where cross-project learned knowledge lives — a user-level store, something under `~/.steward`, or nowhere — is unresolved, and it carries the usual hazard of implicit state: knowledge that changes behaviour while living somewhere nobody thinks to look.

## 11. Notification is the gate on autonomy

The entire value proposition is "don't bother me unless it matters," and both failure modes are bad in the same way: notify too much and the human stops reading; notify too little and they discover in the morning that $400 went somewhere wrong. So notification policy is *the* tuning knob for how much autonomy the human has actually granted, and it belongs in `policy.md`.

Two sources, and the distinction matters:

- **Steward notifies mechanically** — the run reaching its gate, signoff, no workers alive but tasks pending, a task exited without a log. Conditions with no judgement in them.
- **The agent notifies with judgement** — `steward notify`, carrying an interpretation: "the sonnet arm is failing systematically, I've paused it, here's why."

The second is the valuable one and the reason `steward notify` should exist as a command rather than being Steward-internal. It also means the agent needs to know when *not* to use it, which is a policy question, not a mechanism question.

### 11.1 Four kinds, and two of them are not the agent's to send

Every post carries a **kind**. It is not decoration: it sets how the post presents, and its whole job is keeping the one message that needs a person distinguishable from the ones that merely inform.

| kind | means | sent by |
|---|---|---|
| `attention` | something worth knowing, and work continues — a class growing fast, a task that died, a scan finding, a re-run proposed | the agent |
| `stopped` | nothing progresses until a person answers — a hard stop, a question the agent is blocked on, or a **parked worker** | the agent, and Steward for a park |
| `gate` | tasks finished, scans drained, anomalies settled; the run is waiting on `signoff` | **Steward only** |
| `complete` | signed off | **Steward only** |

The split is the point. `attention` and `stopped` carry judgement, which is why they are the agent's. The other two are **terminal**, they are made **once**, and `steward notify` refuses them.

**A parked worker is the one `stopped` Steward sends itself, and the exception proves the rule.** It needs no judgement — reconcile can see it in the control-channel read it already performs ([execution.md](execution.md), *The parked worker*) — and it must not wait for an agent, because an absent agent plus a parked worker is precisely the silence-while-stalled that made the timer mechanical in the first place. So it is latched like `gate`: posted once when a worker parks, recorded in the journal, and re-armed when that park is answered. A worker that waits six hours produces one post, not thirty-six.

**Why `complete` cannot be hand-sent.** Completion means a human adjudicated, not that the processes exited — that is the whole content of *Signoff*. A post saying otherwise would be a claim nobody made, and hand-written completions arrive in batches and bury the one post that actually needs attention.

**`gate` is the handover, and it is a latch.** The first tend that finds the run settled posts it and records that it did; every later tend writes its files and posts nothing. So the channel going quiet *is* the signal that the automatic work is over. It needs no new state to implement — "the gate was posted" is a journal event, so the fold already knows. And because a workspace is a project rather than a run, a later `launch` that puts work back in flight reopens it: the latch clears, and the next settling posts again. That is the case a manual convention would get wrong, since a relaunch is exactly where nobody would think to re-arm anything.

**Post freely.** With no channel configured, `notify()` is a silent no-op that never raises ([What Inspect already provides](#112-what-inspect-already-provides)) — so the agent never has to check whether notification is set up, and a machine with no target does not accumulate failures. The cost of an unnecessary `attention` is one line in a channel; the cost of a skipped `stopped` is a run that waits all night for an answer nobody knew was wanted.

### 11.2 What Inspect already provides

`inspect_ai.util.notify(message, title=None)` — async, best-effort, backed by [Apprise](https://appriseit.com), so every channel (Slack, email, SMS, desktop, webhook) comes from Apprise's URL DSL rather than from Inspect. It never raises and never blocks more than five seconds, which suits a caller that must not stall a tend. Apprise is an optional dependency.

Three properties of it shape Steward's design:

**Configuration is reference-only, and Steward must preserve that.** Inspect deliberately refuses notification URLs as API or CLI arguments — you pass `True` (read `INSPECT_EVAL_NOTIFICATION`) or a path to an Apprise config file. The rationale is keeping credentials out of source, shell history, process listings, and eval logs. Steward inherits that discipline for free and should not break it — the channel is named by the environment, never by an argument Steward accepts. Worth writing down before someone adds a well-meaning `--notify-url` flag.

**There is no policy layer whatsoever** — no rate limiting, no dedup, no severity, no thresholds, no only-on-failure. So the notification policy this document cares about is entirely Steward's to build. Inspect's own `notify_user()` tool description already carries the concern as advice to the model ("operators get noise fatigue; batch into milestones"), which is the same problem one level down.

**There is no eval-completion notification.** Nothing fires on task or eval completion today. That makes Steward the natural owner rather than a duplicator, and appropriately so: *complete* is a Steward-level concept — tasks finished **and** the scan drained **and** adjudication resolved — that Inspect is not in a position to know.

### 11.3 The gap: notifying from outside an eval

`notify()` resolves an Apprise instance from a `ContextVar` installed inside `eval_resolve_tasks`, so it is a **silent no-op anywhere outside a running eval**. Steward's tend, and therefore `steward notify`, runs in a process that is not inside an eval at all — so the function most relevant to Steward is exactly the one that does nothing when Steward calls it.

The fix is small: `build_apprise()` and `init_apprise()` already exist and do precisely what is needed, but live in the private `inspect_ai.util._notify`. Making them public (or adding a `notification_scope(config)` convenience) is the same move as *Public eval-set directory operations* in execution.md — a documented surface for external callers rather than a private one reached around. It is also not Steward-specific: any script that runs evals and wants to be told when it finished hits the identical wall.

### 11.4 A distinction worth not blurring

Inspect's existing `notify()` call sites are human-in-the-loop moments — `request_input()` behind the `ask_user()` tool, and `human_approver`. Both **block the sample** until a human responds. Steward's escalations use the same channel for the opposite semantics: queue the question, notify, and *carry on*. Same pipe, blocking versus non-blocking, and conflating them would reintroduce the one thing adjudication must never do.

## 12. Anomalies are structured state

Adjudication needs a data structure, not just a conversation. Without one, an unresolved problem is only ever a sentence in a summary — which means at a ten-minute cadence it gets re-discovered and re-reported on every tend, and nothing can tell whether it was already raised, already ruled on, or already fixed.

An **anomaly** is anything observed in the run that may need a decision: a cluster of errored samples, samples that hit a token or time limit, a task that scored uniformly zero, **any task that failed at all**, a scan pass that failed, or — see below — something a scan pass *found*.

That fourth item is stronger than it reads, and it is the one place an anomaly is not merely *observed* but *blocking*. Because `fail_on_error=False` absorbs everything sample-shaped, a task that fails has failed structurally, and Steward never restarts one on its own — the restart is an action a ruling authorizes ([scheduling.md](scheduling.md), *Failure is adjudicated, not retried*). Classing is what makes that affordable: forty workers killed by one reboot are one anomaly with forty instances and one decision, not forty of anything. Samples cut short by an **operator** limit count — including those a tool-approval monitor terminated; samples that exhausted a limit their own task declared do not, since that is the measurement working as designed (see *`anomalies.md`*). It is not stored directly: anomalies are **folded out of `journal.jsonl`** (see *State is a fold over the journal*), cached in `.steward/`, and surfaced through `status.md`. Each tend appends what it observed and what it decided; current state is the replay.

The fields that earn their place:

| field | why |
|---|---|
| `id` | **stable across tends** — the whole point |
| `class` | the computed key; 47 samples become one item, and it is what a ruling applies to |
| `evidence` | sample ids, error text, time window, counts |
| `effect` | how the final data is marked, when the ruling is *accept* — the field `anomalies.md` reports |
| `state` | `open` → `investigating` → `proposed` → `ruled` → `resolved`, or `accepted` |
| `proposal` | what Steward suggests, so the human can agree in one word — **may span several classes** (see *Three levels*) |
| `ruling` | the decision and its reasoning |
| `resolution` | what happened after — re-ran and passed, re-ran and failed again |
| `precedent` | prior rulings on this class, carried along rather than looked up |

**Stable identity is the hard requirement**, and it is easy to get wrong. The key is the class — *not* the set of affected samples, because that set grows as more samples fail into the same class. Get this wrong and either the same anomaly notifies fifty times over an overnight run, or a growing problem keeps looking like a new one and never accumulates the weight that would justify escalating it.

**A parked worker is deliberately not an anomaly**, and the boundary is worth drawing because it looks like one — it blocks, it needs a person, and it recurs across tends. Every field above fails on it. It has no class (nothing is grouped; each park is one worker asking one question), no evidence (nothing went wrong), no effect on the final data, no proposal Steward could make, and nothing to rule on — it resolves when someone *answers*, not when someone decides what to do about it, and it leaves no trace afterwards. An anomaly is a post-hoc fact about work that already happened; a park is a live condition on work that has stopped. It belongs in the tend summary and `status.md` as blocked work ([execution.md](execution.md), *The parked worker*), and never in `anomalies.md`. Its state is held by the worker, not by the journal — the fold has nothing to remember, because when the worker is answered the condition is simply gone.

### 12.1 Three levels: instance, class, proposal

One grouping level is not enough, and trying to make it serve both ends is what makes the identity question feel unanswerable. There are two different consumers with opposite needs, so there are two groupings.

| | what it is | derived by | why it exists |
|---|---|---|---|
| **instance** | one errored sample, one failed task, one scan finding | observation | the unit a re-run acts on |
| **class** | instances sharing a computed key | mechanically, no judgement | the unit a *ruling applies to* |
| **proposal** | a set of classes presented as one decision | judgement — the agent's | the unit a *human answers* |

**The class key is computed and deliberately fine.** For a sample error it is the exception type plus the frame it was raised in — recoverable from the traceback, which `eval_error()` builds with `format_traceback(exc_type, ...)`, and the one part of an error that does not vary with ids, hosts, or timestamps ([execution.md](execution.md)). For a task failure it is the failure signature ([scheduling.md](scheduling.md), *The failure signature classes the anomaly*). Message text is deliberately **not** in the key: it carries uuids, hostnames, and counts, and one of those splits a single cause into forty classes.

Fine is the right setting because **over-merging is the expensive direction.** A ruling authorizes re-runs, so a class that merged two causes re-runs instances that did not earn it — and does so silently. Under-merging costs a longer list, which is visible and recoverable.

**But a fine key over-splits relative to reality, and the proposal layer is the answer.** In practice a run produces two or three real causes, not thirty flavours: a provider outage shows up as `APIConnectionError` at several frames, `ReadTimeout` at others, and a handful of status errors — a dozen computed classes, one thing that went wrong. Nobody wants to answer twelve questions about it.

So **a proposal names the classes it covers**, and the human agrees once. The ruling is then recorded against *each class individually*, which is what keeps the grouping from re-introducing the over-merge risk: if one of those classes later turns out to be something else, it is separately visible and separately revisable, and the record shows it was ruled as part of a group.

**Grouping is judgement, so it is the agent's**, on the same division that puts investigation there. That degrades gracefully: with no agent in session the classes stand on their own and `status.md` lists twelve, which is noisier but never wrong. When an agent arrives it collapses them into two proposals. Nothing is lost by waiting, and precedent makes the collapse cheaper each time, since the classes that grouped together last time are recorded as having done so.

### 12.2 The window closes when someone rules

Given classes, the remaining identity question is when one ends. **An anomaly stays open and absorbs new instances until it is ruled on; an instance of the same class arriving after a ruling opens a new anomaly.**

This is better than a clock boundary because it means something. A fixed window splits a slow burn arbitrarily and merges a recurrence arbitrarily; a state boundary says exactly the useful thing — *this happened again after you decided about it*, which is the signal that a ruling did not work. It also gives "do not notify fifty times" a precise mechanism rather than a heuristic.

Its one weakness is real and needs a mitigation rather than an argument: an anomaly nobody rules on absorbs instances silently and never re-escalates, so a problem that is getting worse looks unchanged. **Re-notification therefore triggers on weight, not on novelty** — an open anomaly that crosses an order of magnitude in instance count is worth saying again, where each new instance is not.

**This also answers a question execution.md left open.** Open question 10 asks what "resolved" means for an eval set. The answer falls out: **a run is resolved when no anomaly is open.** Not "all tasks finished" — tasks can finish with holes — but every observed problem carried to a ruling and a resolution. That is a definition Steward can actually enforce, and it is why completion is a Steward-level concept that Inspect could not compute.

*Resolved* is not the same as *done*, though. See *Signoff*.

### 12.3 Scan findings are anomalies, and they arrive last

Scanners are purpose-built to notice things, which makes them the most valuable anomaly source in the system. Worth separating two cases that the word "scan error" blurs:

- a scan pass **failing** — infrastructure, handled like any dead worker;
- a scan pass **succeeding and reporting something bad** — reward hacking, an attempt to escape the sandbox, a gamed or malfunctioning grader, a misconfigured environment, a tool harness that broke.

The second is a *finding*, not an error, and it is the kind most likely to need a human. Execution errors say what broke mechanically; scan findings say what broke **semantically**, and semantic damage is exactly what a person has to weigh: is this grader failure bad enough to re-run, or is the score still trustworthy?

This settles the lifecycle ordering. Scans run over logs that have already landed, and the final pass runs after cleanup and adjudication settle (execution.md, *The one real ordering constraint*), so **scan-sourced anomalies appear after everything else looks finished**. A run that read as resolved can un-resolve when its scan drains. Hence:

```
tasks complete  →  scan drains  →  anomalies settle  →  human signs off
```

Which is the concrete reason signoff sits after the scan rather than after the tasks, and another reason a run is not finished when its workers are.

### 12.4 Adjudicate as you go, because that collapses to the other thing anyway

There is a coherent alternative worth recording, because a previous system in this shape chose it: hold *every* question for one blocking gate at the end, once all tasks have finished and all scans have drained. Its argument is real — a scan surfaces re-run candidates the error census cannot see, so ruling before the scans are in risks ruling twice, and an arm that just finished then needs nothing from anyone.

**Steward adjudicates continuously instead, and the reason is that continuous adjudication contains the other option rather than competing with it.** An anomaly raised at 11pm that nobody answers is still sitting there at 8am, joined by everything raised since — which is precisely the single end-of-run gate, arrived at by doing nothing. The choice is therefore not between two behaviours but between one behaviour and a strict subset of it: asking as you go costs nothing when no one is listening, and when someone *is*, the night produces re-runs instead of a queue of questions to start the morning with. That is the whole premise of prioritizing re-runs ahead of fresh work ([scheduling.md](scheduling.md), *Approved re-runs go first*).

The wasted-work risk the end-gate model worries about is real but small, and **the scan cadence is what shrinks it.** Because a pass spawns as soon as a log lands rather than accumulating (scheduling.md, *Scanning is scheduled work*), a task's findings usually arrive well before anyone gets around to ruling on its errors. Where they have not, the residual cost is a cheap re-run of errored samples that a later invalidation supersedes — against a night spent idle, that trade is not close.

Two things follow for the agent. It should **hold a proposal whose task has not been scanned yet** where the re-run looks expensive and the scan looks imminent — judgement, not a rule. And it should ask only what it is genuinely blocked on; everything else accumulates and is answered together, which is the end-gate model reappearing as the default rather than as a policy.

### 12.5 Scanning collects; investigation digs

A distinction worth importing from how scanning is used in practice: **scanning collects results across everything, investigation digs into the interesting ones.** They are different activities with different economics — one is broad, mechanical, and runs over every transcript; the other is narrow, targeted, and follows a specific question.

That maps directly onto a state the anomaly model was missing. Scanning produces *candidate* anomalies, and some of them are not yet decidable: the grader container fell over, but was that one flaky sandbox or a systematic harness break? Nobody can rule on it until someone looks. Investigation is the step between observing an anomaly and being able to propose a ruling, and it needs its own state — `investigating` — so that the next tend does not re-propose it and `status` can report that it is being worked rather than ignored.

**Investigation is mostly the agent's job, not a scanner's**, and that follows the division running through the whole design. Scanning is mechanical: it runs on everything and exercises no judgement. Investigation is judgement-adjacent — choosing what to look at, deciding what it means — and the agent already has the tools for it (`inspect log`, `samples_df`, reading the transcript). Writing a scanner to answer a one-off question is the wrong shape.

Its cost is also a different kind. A scan pass is expensive in wall-clock and tokens across many logs; an investigation is expensive in **agent context** for a few. That matters for pacing: an anomaly whose investigation means reading a five-hundred-sample transcript may need to be narrowed before it is opened, not after.

Where investigation *is* a scan — a targeted pass with more expensive scanners over a handful of logs — the mechanism already exists. The scan protocol takes a list of log locations, so a narrow pass over three logs is the same call as a broad pass over three thousand. What it does not yet have is a way to ask for *different* scanners than the definition's, which is what an investigation pass would usually want.

For an anomaly to be investigable at all, it has to carry the pointers: which logs, which samples, which time window. That is already in `evidence`, and this is the use that justifies keeping it precise.

### 12.6 A scan result is a measurement, and only the agent can read it

Asking what threshold turns a scanner value into a finding is a category error, and recognizing that is what unsticks the question. An error is *definitionally* a problem. A scan result is a **reading** — most are normal, and "is 0.3 bad" has no answer in the abstract. Nobody asks Steward to threshold scorer values; scan values are the same kind of thing. `Result.value` is `JsonValue` and carries no severity, verdict, or threshold anywhere in the API, which is the shape of a measurement rather than an omission.

So the question splits, and only one half is hard:

- **A scanner *erroring* on a transcript** is definitionally a problem and classes mechanically like any sample error, keyed on scanner name plus exception type. Note this is finer than the "a scan pass failed" case above, and usefully so: it separates *scanning is broken* from *this scanner hates this transcript*, and only the first is worth waking anyone for.
- **A scanner *result*** is a measurement, and **judging it is the agent's job.** This follows the line already drawn by *scanning collects; investigation digs* — the scanner ran on everything and exercised no judgement, so the judgement has to happen after it.

**Steward's contribution is narrowing, not judging.** It cannot tell that 0.3 is bad; it can tell that 0.3 is *unusual* when thirty-seven tasks scored 1.0 and three scored 0.0. That is computable straight from `scan_results_df()`, which carries `scanner`, `transcript_id`, `value`, and an inferred `value_type`, with no semantics required — and it is the same insight that settled error classification: **one process sees one transcript, Steward sees the distribution over the whole run.** The `@scanner` decorator's `metrics` argument helps here too, not by saying what is bad but by declaring that a scanner's values aggregate at all, which is exactly the discriminator for whether comparison is meaningful.

So **`tend` reports that scan results are ready and which of them look worth a look**, in the same summary that carries everything else. The goal of that signal is precisely stated: *identify where investigation may be fruitful*. It is a shortlist, never a verdict.

#### 12.6.1 What these scanners are actually looking for

The abstraction above is fine but it hides the stakes, and the stakes change what the machinery has to be good at. In practice scanners are looking for **reward hacking, attempts to escape the sandbox, environment misconfiguration, grader gaming or malfunction, and infrastructure failures**. These are not incidental readings; several of them mean the numbers are wrong, and one of them means something happened that a person needs to know about today.

They divide by *consequence*, which is more useful than dividing by scanner, because consequence is what decides where a finding goes:

| kind | examples | what it means | where it lands |
|---|---|---|---|
| **validity** | reward hacking, grader gaming, grader malfunction, environment misconfiguration | the measurement is wrong | a caveat on the data — must reach signoff and `anomalies.md`, and must travel with any published log |
| **behavioural** | sandbox escape attempts | the score stands; the *model did something* | escalation in its own right, on a single instance |
| **infra** | environment and infrastructure failures | some samples never got a fair run | closest to an error; the response is a re-run |

Two things follow that the general treatment above gets wrong.

**Rarity and importance run in opposite directions for the behavioural kind.** One sample attempting to escape its sandbox is *more* significant than forty, not less, and it is worth saying now rather than in the next tend's shortlist. Narrowing by distribution happens to work well here — a rare `True` is exactly what outlier detection finds — but the framing must not imply that a single instance is a weak signal. For this kind, one is a finding.

**And the blind spot is the headline case, not an edge case.** *Successful* reward hacking is systematic by nature: a model that finds the exploit uses it everywhere, so the scanner returns the same value on every sample and there is no outlier to detect. The most important thing a scan can find is precisely the thing variance-based narrowing cannot see. That is worth stating as a limitation of the mechanism rather than hoping nobody notices.

So the two mitigations are not optional extras, they are what makes the mechanism honest:

- **Report each scanner's distribution unconditionally**, not only its deviations. A uniform 0.0 across every task is then visible in `scanning.md` even though nothing stands out within the run — and a human or agent reading a flat distribution for a reward-hacking detector will recognize it immediately, where a shortlist of outliers would have been empty.
- **Compare against the project's previous runs** once there are any. This is the same cross-run precedent [Tuning precedent is the most reusable kind](#1013-tuning-precedent-is-the-most-reusable-kind) wants for throughput, and for the systematic case it is the only thing that catches a break — "this scanner fired on 4% of samples last month and 81% this month" is the shape a successful new exploit actually has.

One further consequence, and it sharpens a hazard already recorded. A validity finding is a caveat that belongs *with the data*, which is a second and stronger reason both files are written into the log directory. It also bears directly on publication: [Publication is part of the attestation](#132-publication-is-part-of-the-attestation) already worries that a task signed with accepted exceptions carries a caveat that travels nowhere. When the exception is "this task's grader was gameable", handing another project the log without the footnote is not untidy, it is passing on a known-bad result.

### 12.7 `scanning.md` and `analysis.md` — what investigation produces

Investigation that leaves no artifact is investigation done again next session. Two files, both **per task**, and both **authored by the agent** — a category the design did not previously have, and the thing that makes the agent's judgement outlive its session:

| file | per task, holds | written |
|---|---|---|
| `scanning.md` | what the scanners said about this task, and what investigation concluded — including "looked, nothing here", which is worth as much as a finding | as scan passes drain |
| `analysis.md` | anything noteworthy about this task: what came out of scanning, plus what the journal holds — error classes, rulings, re-runs, accepted exceptions | rolled up from `scanning.md` and the journal |

Per task because that is the manifest's unit, the unit a ruling and a re-run act on, and the unit anyone reading results actually thinks in.

**This fills a real gap.** The design has produced `status.md` (current state, ephemeral), `anomalies.md` (terse signoff footnotes), and `journal.jsonl` (raw events) — and nothing a person reads to find out *what happened*. `analysis.md` is that document, and its relationship to the journal is the one already established for observations and interpretation: **the journal carries the time series, `analysis.md` carries what it meant.**

#### 12.7.1 They belong in the log directory

Both files are written into `log_dir` alongside the results, not only at the workspace root.

The reason is that **the workspace is often the ephemeral half.** A run on a rented box, an air-gapped runner, or a Hawk pod leaves nothing behind but its log directory — and `log_dir` is frequently S3, which is the artifact people share and the one that is still there in six months. Someone who has the `.eval` files and no workspace has the data and no idea which grader fell over. Analysis has to travel with the results or it does not travel.

It is also consistent rather than novel: Steward already owns the log directory's metadata, writing `eval-set.json` and `logs.json` there. And it is safe — `list_eval_logs` filters by format extension, and `cleanup_older_eval_logs` only ever operates on logs it found through `list_all_eval_logs`, so markdown in the directory is invisible to discovery and never deleted.

They are mirrored on every tend that changes them rather than only at signoff, on the same reasoning that makes the workspace sync unconditional: a run that dies before signoff is exactly the run whose analysis someone will want. Signoff makes them final, not present.

**Both are durable, not disposable.** They belong in the same category as `journal.jsonl` and for the same reason — the interpretation exists nowhere else. `analysis.md` re-derives partly from the journal, but *which things were noteworthy* is judgement, and judgement is not recoverable by replaying events.

### 12.8 Precedent travels with the anomaly

An agent adjudicating an anomaly wants to know whether this class has come up before and what was decided. Making that a lookup it must remember to perform is a design failure — it will sometimes forget, and the whole point of accumulating rulings is that they get applied.

So **prior rulings for a class are attached to the anomaly** wherever it surfaces, in `tend` output and in `status`. The agent can apply precedent without a round trip, and a human deciding sees "you ruled this way twice before, on these dates, for this reason" at the moment of deciding rather than after. It is also the mechanism that makes *interruptions per run, trending down* actually happen rather than merely being hoped for.

## 13. Signoff

Steward can compute that no anomaly is open. Only a person can say **I accept these results**. Those are different claims, and conflating them is how a run ends up looking certified because a machine ran out of things to flag.

`steward signoff` is that attestation: the terminal event in the journal, recording who, when, and what was true at the time — task counts, samples resolved, exceptions accepted. Those accepted exceptions are exactly the contents of [`anomalies.md`](#14-anomaliesmd--the-caveats-that-reached-the-data), so signing off is also the moment the caveat list stops changing.

Three properties it needs:

- **Accepting known holes must be explicit, not blocked.** Real evals ship with failures nobody intends to fix. Refusing to sign until everything is clean would just push people to fake resolutions, so signoff records accepted exceptions by name — "2 samples accepted as errored" is a signed statement, not a silent gap.
- **It is an attestation, not access control.** Nothing can stop an agent from running the command, so the design does not pretend to: it records the signer, and the runbook states plainly that the agent never signs. A forged signature is then visible rather than prevented, which is the same bargain a commit author line makes.
- **It can be invalidated.** A scan landing after signoff, or a later invalidation, re-opens anomalies. The signature stays in the journal as a record of what was believed at the time, and a fresh one is required.

### 13.1 Curation is part of the attestation too

A re-run leaves its predecessor behind. Over a project with several adjudications, `logs/` accumulates superseded attempts, failed ones, and cancelled ones alongside the results that count — and somebody reading that directory in six months has to know that `latest_completed_task_eval_logs` semantics decide which log is real.

**So signoff curates: superseded, failed, and cancelled attempts move to `logs-archive/`, leaving `logs/` holding exactly what the attestation covers.** Nothing is deleted, which is the point — *Steward never destroys a result* is unchanged, and the archive sitting beside the log directory makes that promise visible rather than merely true.

**The argument for doing it here is not tidiness, it is that this is the only moment "superseded" is unambiguous.** Mid-run the answer flickers: a log may be superseded by an attempt still in flight, the newest log by timestamp may be incomplete while an older one is complete, and a re-run authorized at 11pm may itself fail and leave its predecessor as the current result after all. Signoff is the point where every task has settled and the attestation has pinned a manifest digest, so *the signed set* is exactly definable — and it is definable at no other time.

It also makes publication coherent by construction. `--publish` indexes what is in `logs/`, and this section already notes that archiving a signed log removes its store row; doing both inside one command means the store and the directory cannot disagree about which log is the result.

Three details worth fixing now rather than discovering:

- **The predicate is "most recent *completed*", not "newest".** Where the latest attempt is incomplete and an earlier one finished, the earlier one is current and the later one is the attempt to archive. Getting this backwards archives the result and keeps the wreckage.
- **Steward has to compute that itself, or wait.** `latest_completed_task_eval_logs` encodes exactly this predicate and is private and exported nowhere, so curation either reimplements it — simple, and free to drift — or rides on [execution.md](execution.md)'s upstream item 6, which proposed the `archive_dir` for precisely this. That moves item 6 earlier than the roadmap first placed it.
- **Resume is unaffected, and it is worth checking rather than assuming.** The rule that a log must never move exists because resume matches logs where they are — but a *superseded* log is by definition not a resume target, and the current one stays put. A later invalidation that re-opens the project resumes from a log that was never archived.

Because a workspace is a project rather than a run, this happens once per signoff rather than once ever, and each pass curates what that signoff covered. Signoff reports what it moved, in the journal entry and on stdout.

### 13.2 Publication is part of the attestation

Signoff is also where results leave the project, if they leave it at all. The reuse store ([execution.md](execution.md), *Flow's store, and who is allowed to read it*) indexes logs by task identifier so that another project — possibly someone else's, on a shared bucket — skips running a task Steward has already run. A row in it is a claim that a result is good enough to be reused sight-unseen.

That is the same claim signoff makes, which is why the two are one command rather than two. **Nothing is published as logs land**, and the reason is the invariant that governs everything else here: with `fail_on_error=False` a task finishes `status="success"` while carrying errored samples, so a freshly landed log is exactly the provisional thing adjudication exists to examine. Publishing then would export unexamined results into a cache where no one is positioned to catch them.

So the store contains **results a person accepted**, rather than logs that happen to exist — a stronger property than a cache usually has, and the one that makes reading from it automatic rather than a decision. A project that is stopped, abandoned, or simply never signed publishes nothing, and that is the right outcome rather than an oversight.

Two edges follow from the same place. A signed log later superseded by an amendment leaves a store row pointing at an archived path, so archiving removes the row. And a task signed *with* accepted exceptions carries a caveat that lives in this project's `anomalies.md` and travels nowhere — whoever reuses it gets the log without the footnote, which is the sharpest open question about what should be publishable at all.

**It pins to a manifest digest, not to a run.** Because a project's definition evolves (see *A project, not a run*), an attestation has to name what it covered, and the manifest digest is the natural handle — *derived* rather than minted, so nothing has to allocate or persist an id for it. It also gives invalidation a second precise trigger alongside the anomaly one: the definition changed, so what was signed is no longer what is current. A launch whose delta is purely additive leaves an existing signoff standing over the tasks it covered while the new tasks are plainly unsigned; one that archives supersedes part of what was signed, which is a further reason that case escalates rather than proceeding quietly.

The result is a lifecycle with two distinct terminal states rather than one overloaded boolean: **resolved** (computed — nothing is open) and **signed off** (attested — a person accepted it). A run can be resolved and unsigned, or signed with exceptions, and those are usefully different things to report.



## 14. `anomalies.md` — the caveats that reached the data

The journal answers *was this run conducted properly*. A different reader asks a different question — *what caveats apply to these numbers* — and that reader is writing up the results, or reading the write-up, and may never open the journal at all. They need something short, and they need it to be honest.

**The filter is whether an anomaly left a mark on the final data**, and the state machine already draws that line:

| resolution | in the data? | example |
|---|---|---|
| **resolved** | no | 47 rate-limit failures, invalidated, re-ran clean |
| **accepted** | **yes** | 2 samples re-ran twice, still failed, accepted as errored |

So `anomalies.md` is a fold over `journal.jsonl` filtered to `accepted` — no new state, no second record, and it cannot disagree with the journal because it is derived from it. A run with four hundred journal events may have three entries here, and that brevity is the point: it is quotable as footnotes.

Each entry needs what a footnote needs, and one field the journal's ruling may not carry explicitly:

- **what happened** — the class, in a sentence
- **scope** — how many samples, which tasks and models
- **why it was accepted** — the ruling's reasoning, verbatim
- **who accepted it, and when**
- **effect on the data** — the report-facing field: `n` excluded, or samples truncated at a limit, or an arm dropped

That last one is what makes the file usable rather than merely accurate. A reader needs the denominator: *"998 of 1000 samples scored; 2 excluded — sandbox startup failures, accepted after two re-runs."*

**Operator-limited samples belong here; task-limited samples emphatically do not.** A sample that exhausts the token, time, turn, or message limit its task declared has produced a *result* — the eval said "you get this much budget", and not finishing inside it is precisely what the measurement was for. Footnoting those would be reporting the eval's own design as a caveat.

An **operator** limit is different in kind: it comes from outside the eval's design, imposed by whoever is running it rather than by the task author. That sample was cut short by something the task never specified, so its result does not measure what the task set out to measure. It is in the data, it may be biased, and nothing about it looks like a failure.

Inspect draws exactly this line already, so detection is exact rather than heuristic. `EvalSampleLimitType` is `"context" | "time" | "working" | "message" | "token" | "turn" | "cost" | "operator" | "custom"` — `operator` is its own type, distinct from every task-declared limit. The rule is a field comparison, not a judgement:

| `EvalSampleLimit.type` | verdict |
|---|---|
| `token`, `time`, `turn`, `message`, `working`, `cost`, `context` | a result — not an anomaly |
| `operator` | **an anomaly** — raise it |

That is worth noticing against open question 1, where classifying anomalies is otherwise the hard, unresolved part of this design. Here the taxonomy already exists upstream and Steward only has to read it.

### 14.1 Approval terminations

A sample killed by a tool-approval monitor arrives as `type="operator"` too — `TerminateSampleError` is caught and recorded as `EvalSampleLimit(type="operator", limit=1)`, the same shape as a bridge or manual termination. That is convenient: they need no special handling, falling out of the operator-limit rule above and into `anomalies.md` like any other externally-truncated sample.

**No attempt is made to decide whether a termination is a defect or a finding.** For an eval whose subject *is* the approval system, a caveat entry reads oddly — the monitor firing is the result. That discordance is left to the people running such evals, because the alternative is a policy question asked of every project to serve a rare one.

**Distinguishing a termination from any other operator limit is left to investigation.** An earlier draft worked through how Steward might do it mechanically, and the answer was always "read transcript events", which is the one thing scanners are meant to avoid. It is unnecessary: terminations are rare, they are already in `anomalies.md` as externally-truncated samples, and reading a transcript for a handful of samples is exactly what an agent investigating an anomaly does anyway (*Scanning collects; investigation digs*). The mechanism would have served only to save a person from looking at something they should look at.

## 15. Adjudication is a conversation, and it has rules

The hardest part of the workflow, and the least designed. Some starting positions:

**Adjudication never gates execution.** If the agent needs a ruling and the human is asleep, the run keeps going. Queue the question, notify, carry on. This falls straight out of `fail_on_error=False` — everything runs to the end and anomalies are settled afterwards.

**Rule on classes, not instances.** A human cannot adjudicate 47 errored samples, but they can rule on one *class* with evidence attached: "47 samples failed `RateLimitError` against anthropic between 15:40–16:05 — invalidate and re-run?" Clustering anomalies into a handful of decidable classes is the agent's core contribution here, and it depends entirely on the error taxonomy that execution.md lists as unresolved.

**A ruling is reusable, and that is how policy grows** — but only with the human's consent (see *A ruling is not a policy*). Rulings land in `journal.jsonl` automatically; promotion into `policy.md` is proposed and never assumed. Over months the human's actual standards accumulate in place of the ones they guessed at up front, which is the most interesting property in the whole design.

**The default is conservative, because this is a scientific judgement.** Invalidating samples changes the number you report. The agent proposes; the human disposes; everything is recorded with provenance. A run where someone quietly dropped the inconvenient samples is worse than a run with known holes in it.

## 16. Do we need a TUI?

Probably not, and the reason is the premise: **a TUI assumes a present human**, which is the case Steward is explicitly not built for.

Test it against what someone actually wants when they come back:

| they want | the answer |
|---|---|
| "how is it going" | `steward status` |
| "what happened while I was out" | `status.md` + notifications received |
| "look at this weird sample" | `inspect view` — already exists, already good |
| "why did you invalidate those" | the agent, reading `journal.jsonl` |

Nothing in that list wants a bespoke live UI. And if a run-level live view is genuinely wanted later, the right home is probably the Inspect viewer rather than a second UI in a second tool.

This deletes a component, which is the best kind of design decision. It does leave one gap: `inspect view` shows *evals*, not *runs* — pending tasks, adjudication queue, and budget have no home in it today.

## 17. The audit trail

A thread running through all of the above: Steward's directory is an **integrity record**, not a scratch space. Someone reviewing results months later should be able to answer "which samples were re-run, which were dropped, who decided, and why" from the directory alone. Inspect already carries part of this (`EvalLog.invalidated`, per-sample `invalidation` records with provenance); the reasoning and the class-level rulings are Steward's to keep.

This is also the argument for the whole workspace being git-friendly: the record of an eval's conduct is as reviewable as the code that produced it.

## 18. Open questions

1. **Anomaly identity.** *Resolved — see *Three levels: instance, class, proposal* and *The window closes when someone rules*.* The normalization problem dissolved once message text left the key: a class is the exception type plus its raising frame (or, for a task, its failure signature), neither of which carries ids, hostnames, or counts. The window is a state boundary rather than a clock — an anomaly absorbs instances until ruled, and a later instance of the same class opens a new one, which is the useful signal that a ruling did not hold. Fine classes over-split relative to real causes, and the proposal layer is what collapses them for the human without collapsing what a ruling applies to.
2. **How does a human answer when no agent is in session?** Notifications are outbound only, and Inspect's are deliberately one-way. The reply path is "start a session and the agent reads the open anomalies" — workable, but it means a yes/no costs a terminal. The mitigation is making questions rare (pre-authorization plus accumulated policy) rather than making answers fast, but that is a bet, not a solution.
3. **What does Steward propose, and how confidently?** *Partly resolved.* The unit is settled — a proposal covers a set of classes, because real runs produce two or three causes rather than thirty flavours, and the ruling is recorded per class so the grouping stays unpickable. What a proposal must *carry* is not: an action alone is easier to accept than to check, and stated confidence is the field most likely to be miscalibrated and most likely to be leaned on at 3am. Precedent is the promising alternative to self-assessed confidence, since it grounds the proposal in the human's own past decisions rather than Steward's estimate of itself.
4. **Who commits the journal?** `init` prepares the repository, but nothing in the workflow commits. If nobody does, the durability-through-git story quietly fails to happen. Candidates: the agent commits at milestones as a runbook instruction, `signoff` commits as its terminal act, or it stays the human's job. Auto-committing on every tend would collide with the user's own working tree, so the cadence matters as much as the owner.

5. **`status.md` staleness.** *Mostly resolved, and it needs two ages rather than one.* The file states how old it is, and separately how long since the last **collection** ([agent.md](agent.md)) — the pair distinguishes a stopped timer from an unattended run, which a single timestamp cannot. This matters most once the file is synced to a bucket, where a stated age is a remote reader's only evidence that anything has failed. What remains open is presentation: the age must read as a fact about the file rather than as part of the snapshot, or a reader skims past exactly the line that says the snapshot is worthless.
6. **Multiple runs per directory.** *Resolved, and the question was the wrong shape* — see *A project, not a run*. A workspace is a **project**: one evolving definition, one log directory holding the current definition's results, one archive holding everything superseded, and one journal that is a project history. There is no run entity for anything to be "per", so claims key on the log directory, the manifest is whatever the last `launch` captured, and anomalies scope to the logs they concern. What remains is not a question but a consequence: every fold over the journal spans the project's whole history, which is what makes precedent accumulate.
7. **How does a scanner result become an anomaly?** *Resolved — see *A scan result is a measurement, and only the agent can read it*.* It does not, mechanically: a result is a reading rather than an event, so no threshold Steward could apply would mean anything. A scanner *erroring* classes like any other error; a scanner *result* is judged by the agent, with Steward narrowing rather than judging — surfacing distributional outliers from `scan_results_df()`, which needs no semantics — and `tend` reporting which results have landed and which look worth a look. The product is `scanning.md` and `analysis.md`, per task, mirrored into the log directory. What survives is sharper than it was: successful reward hacking is *systematic*, so it produces no outlier at all — the most important finding is the one variance cannot see, which is why distributions are reported unconditionally and why cross-run comparison is load-bearing rather than a nicety. Also unresolved: an investigation pass wants different scanners than the broad pass, which a definition's single scanner configuration cannot express.

8. **How are the concurrency budgets divided?** *Resolved in [scheduling.md](scheduling.md), *Setting the concurrency knobs*, and the framing was wrong: the three knobs are not one budget to divide.* `max_samples` is per-task and set statically at 40, grown by the agent on the absence of rate limits (not on saturation, which measures demand); `max_connections` is process-global and adaptive, so N controllers on a shared bucket coordinate through 429s and need no division; only `max_sandboxes` is a genuine allocation, divided by outstanding tasks of each host-bound sandbox type. Rebalancing dissolves with it. The grader-model question survives as a real gap — a task whose scorer consumes a bucket the manifest never revealed is invisible to all of this.

9. **What is in a journal event?** *Resolved — see *The journal records observations, not only decisions*.* Nine event types over a shared `ts`/`type` envelope, including a per-tend `observation` so the agent's time series survives a session boundary. The crux turned out to be a false one: reasoning stays free text because **precedent keys on the class, not on the prose**, so nothing needs to match against it. Machinery — failed tends, spawn errors, sync timeouts — goes to the operational log instead, keeping the journal a record of what was seen and decided.
