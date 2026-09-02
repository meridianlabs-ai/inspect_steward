# Reference – Inspect Steward

Steward CLI - supervise evaluations.

#### Usage

``` text
steward [OPTIONS] COMMAND [ARGS]...
```

#### Subcommands

|  |  |
|----|----|
| [ack](#steward-ack) | Accept an open item, so that nothing reports it again. |
| [collect](#steward-collect) | Read what has accumulated, and mark how far you have read. |
| [init](#steward-init) | Create a steward workspace. |
| [investigate](#steward-investigate) | Mark an anomaly class as under investigation. |
| [launch](#steward-launch) | Start a run: capture the definition, commit it, arm a timer, tend once. |
| [notify](#steward-notify) | Post MESSAGE to this run’s notification channel. |
| [pause](#steward-pause) | Stop scheduling new work, leaving what is running to finish. |
| [propose](#steward-propose) | Propose one disposition for one or more anomaly classes. |
| [raise](#steward-raise) | Record that an item is now with the person who can decide it. |
| [ramp](#steward-ramp) | Hold or resume the tuning loop’s climb. |
| [resume](#steward-resume) | Start scheduling again. |
| [rule](#steward-rule) | Rule on anomaly classes: what the failures mean, and what happens to the data. |
| [runbook](#steward-runbook) | Print the agent runbook: how Steward works. |
| [signoff](#steward-signoff) | Attest that these results are accepted, and end the run. |
| [status](#steward-status) | Report where the run stands, and what the next turn would do. |
| [tasks](#steward-tasks) | Enumerate the tasks defined by an eval set definition. |
| [tend](#steward-tend) | Run one turn of the supervision loop. |
| [timer](#steward-timer) | Arm, disarm, and inspect the timer that tends this run. |

## steward ack

Accept an open item, so that nothing reports it again.

`ITEM` is an item id, or any unambiguous prefix of one — ids are printed beside each item by `steward status`.

The item leaves `status.md`, the tend summary, and the verdict; the journal keeps the record. It comes back only if the condition changes in a way that matters, because an item’s id is chosen so that it does: acknowledging one edit to a definition does not acknowledge the next one.

#### Usage

``` text
steward ack [OPTIONS] ITEM
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--reason` | text | Why this is being accepted. Recorded in the journal, and the only account of the decision that survives. | \_required |
| `--by` | choice (`human` \| `agent`) | Who decided. An agent relaying a person’s answer records `human`; one disposing of something on its own judgement records `agent`. | `human` |
| `--json` | boolean | Output the acknowledgment as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward collect

Read what has accumulated, and mark how far you have read.

The agent’s view of the run: the decisions that are still the agent’s to act on, where the run stands, and everything that has happened since the last collection. Whatever the filter sets aside is counted rather than dropped, so a shortened section never reads as an empty one.

Advancing the cursor is a bookmark, not a pop — the journal is append-only, nothing is consumed by being read, and an open item stays open until somebody acts on it. `--peek` leaves the cursor where it is.

#### Usage

``` text
steward collect [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--peek` | boolean | Read without advancing the cursor, so the next collection sees the same history again. | `False` |
| `--since` | integer range (`0` and above) | Show history from this journal position instead of the last collection. `--since 0` shows everything. | None |
| `--help` | boolean | Show this message and exit. | `False` |

## steward init

Create a steward workspace.

DIRECTORY defaults to the current directory and is created if it does not exist.

Safe to re-run: existing files are kept and only what is missing is added. Steward never overwrites your work — least of all `_steward.yaml`, which changes only where you have approved the change.

#### Usage

``` text
steward init [OPTIONS] [DIRECTORY]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--type` | choice (`evalset` \| `flow` \| `hawk`) | Definition type, which decides the placeholder’s filename. | `evalset` |
| `--no-git` | boolean | Do not initialise a git repository. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward investigate

Mark an anomaly class as under investigation.

`CLASS` is an open class key as `steward status` prints it, or any unambiguous prefix. Investigating a proposed class pulls it back out of its proposal.

#### Usage

``` text
steward investigate [OPTIONS] CLASS
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--note` | text | Where the investigation stands — written for the next session, not this one. | \_required |
| `--by` | text | Who is investigating. | `agent` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward launch

Start a run: capture the definition, commit it, arm a timer, tend once.

DEFINITION is a Python file culminating in an eval_set() call, an Inspect Flow spec (Python or YAML), or a Hawk eval set config (YAML). Omitted, this workspace’s own definition is used.

Safe to run again. A second launch is the amend path — it re-captures, reports what changed, and refuses to commit anything that would move results out of logs/ unless you pass –accept-archive.

#### Usage

``` text
steward launch [OPTIONS] [DEFINITION]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--arg`, `-A` | text | Argument for the definition (flow spec function args only). Can be specified multiple times. Defaults to the committed manifest’s on a re-launch. | None |
| `--no-args` | boolean | Capture with no definition arguments, rather than reusing the committed manifest’s. | `False` |
| `--no-overrides` | boolean | Capture at the definition’s own shape, rather than reusing the overrides the committed manifest recorded. Ignores STEWARD\_\* and INSPECT_EVAL\_\* for this launch too. | `False` |
| `--type` | choice (`evalset` \| `flow` \| `hawk`) | Definition type (auto-detected, or taken from the committed manifest). | None |
| `--accept-archive` | boolean | Commit even though results would leave logs/ — archived, or left behind by a log directory that moved. | `False` |
| `--no-timer` | boolean | Launch without arming a timer. The run is then recorded as unsupervised until something arms one. | `True` |
| `--no-env-check` | boolean | Arm even though a scheduled tend would not inherit this shell’s credentials. | `True` |
| `--log-root` | value | Root this machine keeps eval logs under. Used only where the definition names no log_dir, in which case this run writes to /. Overrides `log_root` in `_steward.yaml` and `STEWARD_LOG_ROOT`. | None |
| `--no-log-root` | boolean | Keep this run’s logs in the workspace, whatever root the machine configured. | `False` |
| `--log-store` | value | Where to look for logs this run does not have to produce. Recorded now; read when signoff can publish to it. Overrides `log_store` in `_steward.yaml` and `STEWARD_LOG_STORE`. | None |
| `--no-log-store` | boolean | Run against no log store, whatever this project or machine configured. | `False` |
| `--notification` | value | Where to post what this run cannot decide — an Apprise URL, several separated by commas, or an Apprise config file. Reaches every worker too. Overrides `notification` in `_steward.yaml` and `STEWARD_NOTIFICATION`. | None |
| `--no-notification` | boolean | Post nothing about this run. Silences Steward only — a worker waiting on a person still asks. | `False` |
| `--scan-model` | value | Model scanners use, for this launch’s own turn. Reaches every worker too. Overrides `scan_model` in `_steward.yaml` and `STEWARD_SCAN_MODEL`. | None |
| `--no-scan-model` | boolean | Configure no scan model — scanners use each sample’s own model under evaluation. | `False` |
| `--max-workers` | integer range (`1` and above) | Worker processes, or unset for a process per task. Overrides `max_workers` in `_steward.yaml` and `STEWARD_MAX_WORKERS`. | None |
| `--stall-after` | integer range (`1` and above) | Fruitless respawns before a task is given up on. Overrides `stall_after` in `_steward.yaml` and `STEWARD_STALL_AFTER`. | None |
| `--samples-ramp` | value | Range to discover sample concurrency in, e.g. `[40, 300]`, or `false` to fix it. Overrides `samples_ramp` in `_steward.yaml` and `STEWARD_SAMPLES_RAMP`. | None |
| `--stuck-after` | value | Quiet time before a running sample is reported stuck, with a unit, e.g. `5h`. Overrides `stuck_after` in `_steward.yaml` and `STEWARD_STUCK_AFTER`. | None |
| `--preauthorized` | value | Rulings granted in advance: class patterns to dispositions, e.g. `{'error:ReadTimeout@*': rerun}`, or `false` to decline every standing grant for this turn. Overrides `preauthorized` in `_steward.yaml` and `STEWARD_PREAUTHORIZED`. | None |
| `--tend-interval` | value | How often a scheduled tend runs, with a unit, e.g. `10m`. Overrides `tend_interval` in `_steward.yaml` and `STEWARD_TEND_INTERVAL`. | None |
| `--sync` | value | Where to mirror this workspace’s own files. Defaults to the run’s log directory, so results and what explains them sit together. Overrides `sync` in `_steward.yaml` and `STEWARD_SYNC`. | None |
| `--no-sync` | boolean | Leave the workspace on this machine, whatever this project configured. | `False` |
| `--smoke` | boolean | Rehearse first instead of launching: a few samples per task under a cap, into .steward/smoke/. | `False` |
| `--samples` | integer range (`1` and above) | Samples per task in a smoke (default 2). | None |
| `--cap` | integer range (`0` and above) | Wall-clock minutes a smoke may take, 0 for none (default 15). | None |
| `--accept` | choice (`context_window` \| `reasoning` \| `reasoning_api` \| `scan_coverage`) | Record a smoke check as waived rather than failing on it. Repeatable. | None |
| `--no-break-claim` | boolean | Refuse if another command is wedged, rather than killing it and taking the claim. | `False` |
| `--json` | boolean | Output the delta and the first turn as JSON. | `False` |
| `--log-format` | value | Log file format, overriding the definition’s. Also STEWARD_LOG_FORMAT, INSPECT_LOG_FORMAT, INSPECT_EVAL_LOG_FORMAT. | None |
| `--log-samples` | value | Whether to log individual samples, overriding the definition’s. Also STEWARD_LOG_SAMPLES, INSPECT_EVAL_NO_LOG_SAMPLES. | None |
| `--log-realtime` | value | Whether to log sample events in realtime, overriding the definition’s. Also STEWARD_LOG_REALTIME, INSPECT_EVAL_NO_LOG_REALTIME. | None |
| `--log-images` | value | Whether to log base64-encoded images, overriding the definition’s. Also STEWARD_LOG_IMAGES. | None |
| `--log-model-api` | value | Whether to log model API calls, overriding the definition’s. Also STEWARD_LOG_MODEL_API, INSPECT_EVAL_LOG_MODEL_API. | None |
| `--log-refusals` | value | Whether to log model refusals, overriding the definition’s. Also STEWARD_LOG_REFUSALS, INSPECT_EVAL_LOG_REFUSALS. | None |
| `--log-buffer` | value | Samples to buffer before writing, overriding the definition’s. Also STEWARD_LOG_BUFFER, INSPECT_EVAL_LOG_BUFFER. | None |
| `--log-shared` | value | Whether (and how often) to sync logs to a shared filesystem, overriding the definition’s. Also STEWARD_LOG_SHARED, INSPECT_LOG_SHARED, INSPECT_EVAL_LOG_SHARED. | None |
| `--log-level` | value | Console log level, overriding the definition’s. Also STEWARD_LOG_LEVEL, INSPECT_LOG_LEVEL. | None |
| `--log-level-transcript` | value | Transcript log level, overriding the definition’s. Also STEWARD_LOG_LEVEL_TRANSCRIPT, INSPECT_LOG_LEVEL_TRANSCRIPT. | None |
| `--limit` | value | Dataset slice, overriding the definition’s: a sample count, or a `(start, end)` range. Also STEWARD_LIMIT, INSPECT_EVAL_LIMIT. | None |
| `--sample-id` | value | Specific sample id(s) to run, overriding the definition’s. Also STEWARD_SAMPLE_ID, INSPECT_EVAL_SAMPLE_ID. | None |
| `--sample-shuffle` | value | Whether to shuffle the dataset (optionally with a seed), overriding the definition’s. Also STEWARD_SAMPLE_SHUFFLE, INSPECT_EVAL_SAMPLE_SHUFFLE. | None |
| `--epochs` | value | Epochs to repeat samples over, overriding the definition’s. Also STEWARD_EPOCHS, INSPECT_EVAL_EPOCHS. | None |
| `--max-samples` | value | Sample concurrency, overriding the definition’s. Also STEWARD_MAX_SAMPLES, INSPECT_EVAL_MAX_SAMPLES. | None |
| `--max-tasks` | value | Task concurrency, overriding the definition’s. Also STEWARD_MAX_TASKS, INSPECT_EVAL_MAX_TASKS. | None |
| `--max-subprocesses` | value | Subprocess concurrency, overriding the definition’s. Also STEWARD_MAX_SUBPROCESSES, INSPECT_EVAL_MAX_SUBPROCESSES. | None |
| `--max-sandboxes` | value | Sandbox concurrency, overriding the definition’s. Also STEWARD_MAX_SANDBOXES, INSPECT_EVAL_MAX_SANDBOXES. | None |
| `--max-dataset-memory` | value | Maximum MiB of dataset sample data to hold in memory per task, overriding the definition’s. Zero pages every sample to disk. Also STEWARD_MAX_DATASET_MEMORY, INSPECT_EVAL_MAX_DATASET_MEMORY. | None |
| `--generate-config` | value | Model transport settings, overriding the definition’s. Also STEWARD_GENERATE_CONFIG. | None |
| `--model-base-url` | value | Base URL for model API requests, overriding the definition’s. Also STEWARD_MODEL_BASE_URL. | None |
| `--model-cost-config` | value | Model pricing table (or a path to one), overriding the definition’s. Also STEWARD_MODEL_COST_CONFIG, INSPECT_EVAL_MODEL_COST_CONFIG. | None |
| `--sandbox` | value | Sandbox environment, overriding the definition’s. Also STEWARD_SANDBOX, INSPECT_EVAL_SANDBOX. | None |
| `--sandbox-cleanup` | value | Whether to clean up sandboxes after a task, overriding the definition’s. Also STEWARD_SANDBOX_CLEANUP, INSPECT_EVAL_NO_SANDBOX_CLEANUP. | None |
| `--sandbox-prebuilt` | value | Whether sandbox images are prebuilt, overriding the definition’s. Also STEWARD_SANDBOX_PREBUILT, INSPECT_EVAL_SANDBOX_PREBUILT. | None |
| `--checkpoint` | value | Sample checkpointing, overriding the definition’s. Also STEWARD_CHECKPOINT, INSPECT_EVAL_CHECKPOINT. | None |
| `--approval` | value | Approval policy (or a path to one), overriding the definition’s. Also STEWARD_APPROVAL, INSPECT_EVAL_APPROVAL. | None |
| `--retry-on-error` | value | Sample-level retries before an error is recorded, overriding the definition’s. Also STEWARD_RETRY_ON_ERROR, INSPECT_EVAL_RETRY_ON_ERROR. | None |
| `--score-on-error` | value | Whether to score samples that errored, overriding the definition’s. Also STEWARD_SCORE_ON_ERROR, INSPECT_EVAL_SCORE_ON_ERROR. | None |
| `--debug-errors` | value | Whether to raise sample errors rather than recording them, overriding the definition’s. Also STEWARD_DEBUG_ERRORS, INSPECT_DEBUG_ERRORS. | None |
| `--score` | value | Whether to score the run, overriding the definition’s. Also STEWARD_SCORE, INSPECT_EVAL_NO_SCORE. | None |
| `--score-display` | value | Whether to display scoring metrics, overriding the definition’s. Also STEWARD_SCORE_DISPLAY, INSPECT_EVAL_SCORE_DISPLAY. | None |
| `--tags` | value | Tags to stamp into the logs, overriding the definition’s. Also STEWARD_TAGS, INSPECT_EVAL_TAGS. | None |
| `--metadata` | value | Metadata to stamp into the logs, overriding the definition’s. Also STEWARD_METADATA, INSPECT_EVAL_METADATA. | None |
| `--display` | value | Display type, overriding the definition’s. Also STEWARD_DISPLAY, INSPECT_DISPLAY. | None |
| `--trace` | value | Whether to trace message interactions to the console, overriding the definition’s. Also STEWARD_TRACE, INSPECT_EVAL_TRACE. | None |
| `--help` | boolean | Show this message and exit. | `False` |

## steward notify

Post MESSAGE to this run’s notification channel.

MESSAGE is the title — the line that stands alone in a phone notification, so make it the thing you would want read if nothing else was. Everything else goes in –detail.

The channel is the run’s own: `notification` in \_steward.yaml, STEWARD_NOTIFICATION, or INSPECT_EVAL_NOTIFICATION, whichever is set.

#### Usage

``` text
steward notify [OPTIONS] MESSAGE
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--kind` | choice (`attention` \| `stopped`) | Why you are sending this. `attention` is worth knowing and work continues; `stopped` means nothing progresses until a person answers. | `attention` |
| `--detail` | text | A supporting line, under the message. Repeatable — one per thing you want the reader to see without opening anything. | None |
| `--help` | boolean | Show this message and exit. | `False` |

## steward pause

Stop scheduling new work, leaving what is running to finish.

Every later turn reports the run as paused and spawns nothing. Workers already in flight are left alone: stopping one is not a mechanical act, and it is not what pausing means.

Recorded in the journal rather than in `.steward/`, which is disposable — a pause that a cleared cache silently undid would resume an expensive run with nobody watching.

#### Usage

``` text
steward pause [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--reason` | text | Why the run is being held. Recorded in the journal, and the only account of the decision that survives. | \_required |
| `--by` | choice (`human` \| `agent`) | Who decided. An agent relaying a person’s instruction records `human`. | `human` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward propose

Propose one disposition for one or more anomaly classes.

`CLASSES` are open class keys, or unambiguous prefixes. The proposal becomes one consolidated item for its owner, answered whole or in part by `steward rule --proposal ID`.

#### Usage

``` text
steward propose [OPTIONS] CLASSES...
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--action` | choice (`rerun` \| `exclude` \| `zero` \| `score` \| `accept` \| `dismiss`) | The one disposition this proposal asks for. Classes wanting different answers are different proposals. | \_required |
| `--reason` | text | Why these classes are one decision — what the investigation found. | \_required |
| `--by` | text | Who is proposing. | `agent` |
| `--json` | boolean | Output the proposal as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward raise

Record that an item is now with the person who can decide it.

`ITEM` is a **human-owned** item’s id, or any unambiguous prefix of one — ids are printed beside each item by `steward status`, under the heading that says whose it is. An item the agent owns is its own to investigate and then `steward ack --by agent`; raising one would take it out of the agent’s queue with nobody else looking at it.

The item stays open and stays in `status`: only a ruling closes it. What changes is that `steward collect` stops offering it as work, so the agent is not shown the same decision every time it looks. It returns if the condition changes in a way that matters, because an item’s id is chosen so that it does.

#### Usage

``` text
steward raise [OPTIONS] ITEM
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--note` | text | What was done to surface it — where it was asked, and of whom. Optional: handing a decision off does not owe the account that disposing of one does. | \``| |`–json`| boolean | Output the hand-off as JSON. |`False`| |`–help`| boolean | Show this message and exit. |`False\` |

## steward ramp

Hold or resume the tuning loop’s climb.

#### Usage

``` text
steward ramp [OPTIONS] COMMAND [ARGS]...
```

#### Subcommands

|  |  |
|----|----|
| [hold](#steward-ramp-hold) | Stop the tuning loop climbing, leaving the levels where they are. |
| [resume](#steward-ramp-resume) | Let the tuning loop climb again. |

### steward ramp hold

Stop the tuning loop climbing, leaving the levels where they are.

With IDENTIFIER (a task identifier, from `steward tasks`), holds that one arm and leaves the others climbing; bare, holds the fleet. Ramp-downs stay active either way — a hold is a brake on growth, never on the cut that exits a retry storm.

#### Usage

``` text
steward ramp hold [OPTIONS] [IDENTIFIER]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--reason` | text | Why the climb is being held. Recorded in the journal, and what the next reader of the tuning block sees. | \_required |
| `--by` | choice (`human` \| `agent`) | Who decided. Defaults to the agent, because holding on its own judgement is exactly what this verb exists for. | `agent` |
| `--help` | boolean | Show this message and exit. | `False` |

### steward ramp resume

Let the tuning loop climb again.

With IDENTIFIER, releases that task’s hold; bare, releases everything — the fleet-wide hold and every per-task one, because the bare verb means *ramp freely again* rather than *ramp except where I have forgotten I said otherwise*.

#### Usage

``` text
steward ramp resume [OPTIONS] [IDENTIFIER]
```

#### Options

| Name     | Type    | Description                 | Default |
|----------|---------|-----------------------------|---------|
| `--help` | boolean | Show this message and exit. | `False` |

## steward resume

Start scheduling again.

The next tend converges from whatever it finds, which is not necessarily where the run was when it was paused — logs landed, workers exited, and the definition may have been relaunched. That is the ordinary behaviour of the loop rather than a caveat about pausing.

#### Usage

``` text
steward resume [OPTIONS]
```

#### Options

| Name     | Type    | Description                 | Default |
|----------|---------|-----------------------------|---------|
| `--help` | boolean | Show this message and exit. | `False` |

## steward rule

Rule on anomaly classes: what the failures mean, and what happens to the data.

`CLASSES` are class keys as `steward status` prints them, or any unambiguous prefix. A ruling closes the class’s window — every open generation of it — and recurrence afterwards opens a new one carrying this decision as precedent.

#### Usage

``` text
steward rule [OPTIONS] [CLASSES]...
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--proposal` | text | Answer a proposal by id. Alone, rules every class it covers that still awaits one; with CLASS arguments, rules just those — a partial answer, and the remainder stays proposed. | None |
| `--disposition` | choice (`rerun` \| `exclude` \| `zero` \| `score` \| `accept` \| `dismiss`) | The answer. Required unless `--proposal` supplies it; given with one, it overrides for the named classes. | None |
| `--reason` | text | Why. Recorded in the journal, attached as precedent to any recurrence, and the only account of the decision that survives. | \_required |
| `--by` | text | Who decided — a name, never a role. An agent relaying a person’s decision records the person. | \_required |
| `--effect` | text | The sentence the report carries for a disposition that marks the data. Composed automatically for exclude/zero/score, required for accept, refused for rerun and dismiss — which mark nothing. | None |
| `--json` | boolean | Output the rulings as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward runbook

Print the agent runbook: how Steward works.

The runbook ships with the package rather than living in the workspace, so an agent can never follow last year’s instructions against this year’s CLI. It is *mechanics*; `_steward.yaml` in the workspace is what a particular human wants.

#### Usage

``` text
steward runbook [OPTIONS]
```

#### Options

| Name     | Type    | Description                 | Default |
|----------|---------|-----------------------------|---------|
| `--help` | boolean | Show this message and exit. | `False` |

## steward signoff

Attest that these results are accepted, and end the run.

Runs a final turn, refuses with every blocker at once if anything is still unnamed, moves superseded attempts into `logs-archive/`, records who signed and what they signed over, and takes the timer down. It does not commit the journal — that stays yours.

A person decides this. An agent may prompt for it and may run it once they answer, recording their name, which is why the signer is recorded rather than the process.

#### Usage

``` text
steward signoff [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--by` | text | Who is accepting these results — a name, never a role. An agent relaying a person’s decision records the person. | \_required |
| `--note` | text | What you want said about the acceptance. Optional: the account of every decision is already in the journal. | None |
| `--again` | boolean | Record a second signature over a run whose first one still stands. | `False` |
| `--no-break-claim` | boolean | Refuse if another command is wedged, rather than killing it and taking the claim. | `False` |
| `--json` | boolean | Output the signature, or the blockers, as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward status

Report where the run stands, and what the next turn would do.

`tend --dry-run`: the same reads and the same decision, with the actions discarded. Read-only — it spawns nothing, moves nothing, writes nothing, and does not take the run claim, so it is safe to run as often as you like while a tend is in flight.

#### Usage

``` text
steward status [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--max-workers` | integer range (`1` and above) | Worker processes, or unset for a process per task. Overrides `max_workers` in `_steward.yaml` and `STEWARD_MAX_WORKERS`. | None |
| `--stall-after` | integer range (`1` and above) | Fruitless respawns before a task is given up on. Overrides `stall_after` in `_steward.yaml` and `STEWARD_STALL_AFTER`. | None |
| `--samples-ramp` | value | Range to discover sample concurrency in, e.g. `[40, 300]`, or `false` to fix it. Overrides `samples_ramp` in `_steward.yaml` and `STEWARD_SAMPLES_RAMP`. | None |
| `--stuck-after` | value | Quiet time before a running sample is reported stuck, with a unit, e.g. `5h`. Overrides `stuck_after` in `_steward.yaml` and `STEWARD_STUCK_AFTER`. | None |
| `--preauthorized` | value | Rulings granted in advance: class patterns to dispositions, e.g. `{'error:ReadTimeout@*': rerun}`, or `false` to decline every standing grant for this turn. Overrides `preauthorized` in `_steward.yaml` and `STEWARD_PREAUTHORIZED`. | None |
| `--format` | choice (`text` \| `md`) | `text` for a terminal; `md` for an agent relaying this to somebody, which is what agent.md asks it to do verbatim. | `text` |
| `--json` | boolean | Output the state as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward tasks

Enumerate the tasks defined by an eval set definition.

DEFINITION is a Python file culminating in an eval_set() call, an Inspect Flow spec (Python or YAML), or a Hawk eval set config (YAML).

#### Usage

``` text
steward tasks [OPTIONS] DEFINITION
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--arg`, `-A` | text | Argument for the definition (flow spec function args only). Can be specified multiple times. | None |
| `--type` | choice (`evalset` \| `flow` \| `hawk`) | Definition type (auto-detected by default). | None |
| `--json` | boolean | Output the full manifest as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward tend

Run one turn of the supervision loop.

Reconciles the log directory against the committed manifest: spawns what should be running, records what died, archives what the definition no longer asks for, then rewrites status.md and appends to the journal. Never blocks — everything long-running is a detached child that a later turn observes.

Safe to call as often as you like. A repeated turn is a no-op, and an interrupted one is reconciled by the next.

#### Usage

``` text
steward tend [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--max-workers` | integer range (`1` and above) | Worker processes, or unset for a process per task. Overrides `max_workers` in `_steward.yaml` and `STEWARD_MAX_WORKERS`. | None |
| `--stall-after` | integer range (`1` and above) | Fruitless respawns before a task is given up on. Overrides `stall_after` in `_steward.yaml` and `STEWARD_STALL_AFTER`. | None |
| `--samples-ramp` | value | Range to discover sample concurrency in, e.g. `[40, 300]`, or `false` to fix it. Overrides `samples_ramp` in `_steward.yaml` and `STEWARD_SAMPLES_RAMP`. | None |
| `--stuck-after` | value | Quiet time before a running sample is reported stuck, with a unit, e.g. `5h`. Overrides `stuck_after` in `_steward.yaml` and `STEWARD_STUCK_AFTER`. | None |
| `--preauthorized` | value | Rulings granted in advance: class patterns to dispositions, e.g. `{'error:ReadTimeout@*': rerun}`, or `false` to decline every standing grant for this turn. Overrides `preauthorized` in `_steward.yaml` and `STEWARD_PREAUTHORIZED`. | None |
| `--sync` | value | Where to mirror this workspace’s own files. Defaults to the run’s log directory, so results and what explains them sit together. Overrides `sync` in `_steward.yaml` and `STEWARD_SYNC`. | None |
| `--no-sync` | boolean | Leave the workspace on this machine, whatever this project configured. | `False` |
| `--no-break-claim` | boolean | Refuse if another tend is wedged, rather than killing it and taking the claim. | `False` |
| `--json` | boolean | Output the turn as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |

## steward timer

Arm, disarm, and inspect the timer that tends this run.

#### Usage

``` text
steward timer [OPTIONS] COMMAND [ARGS]...
```

#### Subcommands

|  |  |
|----|----|
| [arm](#steward-timer-arm) | Install a timer that tends this workspace on a schedule. |
| [disarm](#steward-timer-disarm) | Remove this workspace’s timer. |
| [status](#steward-timer-status) | Say what is armed, and check that it is really there. |

### steward timer arm

Install a timer that tends this workspace on a schedule.

Idempotent: an existing timer is removed first, so re-arming at a new interval or under a different scheduler leaves exactly one.

#### Usage

``` text
steward timer arm [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--tend-interval` | value | How often a scheduled tend runs, with a unit, e.g. `10m`. Overrides `tend_interval` in `_steward.yaml` and `STEWARD_TEND_INTERVAL`. | None |
| `--scheduler` | choice (`launchd` \| `systemd` \| `cron`) | Which scheduler to use. Detected when not given, preferring one that survives a reboot. | None |
| `--no-env-check` | boolean | Arm even though a scheduled tend would not inherit this shell’s credentials. | `True` |
| `--help` | boolean | Show this message and exit. | `False` |

### steward timer disarm

Remove this workspace’s timer.

Nothing else stops: workers in flight finish, and `steward tend` still works by hand. What ends is anything happening without somebody typing it, which every later `status` then reports.

#### Usage

``` text
steward timer disarm [OPTIONS]
```

#### Options

| Name     | Type    | Description                 | Default |
|----------|---------|-----------------------------|---------|
| `--help` | boolean | Show this message and exit. | `False` |

### steward timer status

Say what is armed, and check that it is really there.

The one command that asks the scheduler rather than the journal. Every other reader — a tend, a `status`, the item projection — goes on what arming recorded, because they run every few minutes and this costs a subprocess.

#### Usage

``` text
steward timer status [OPTIONS]
```

#### Options

| Name     | Type    | Description                       | Default |
|----------|---------|-----------------------------------|---------|
| `--json` | boolean | Output the timer’s state as JSON. | `False` |
| `--help` | boolean | Show this message and exit.       | `False` |
