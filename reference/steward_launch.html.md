# steward_launch – Inspect Steward

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
| `--max-workers` | integer range (`1` and above) | Worker processes, or unset for a process per task. Overrides `max_workers` in `_steward.yaml` and `STEWARD_MAX_WORKERS`. | None |
| `--stall-after` | integer range (`1` and above) | Fruitless respawns before a task is given up on. Overrides `stall_after` in `_steward.yaml` and `STEWARD_STALL_AFTER`. | None |
| `--samples-ramp` | value | Range to discover sample concurrency in, e.g. `[40, 300]`, or `false` to fix it. Overrides `samples_ramp` in `_steward.yaml` and `STEWARD_SAMPLES_RAMP`. | None |
| `--tend-interval` | value | How often a scheduled tend runs, with a unit, e.g. `10m`. Overrides `tend_interval` in `_steward.yaml` and `STEWARD_TEND_INTERVAL`. | None |
| `--sync` | value | Where to mirror this workspace’s own files. Defaults to the run’s log directory, so results and what explains them sit together. Overrides `sync` in `_steward.yaml` and `STEWARD_SYNC`. | None |
| `--no-sync` | boolean | Leave the workspace on this machine, whatever this project configured. | `False` |
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
