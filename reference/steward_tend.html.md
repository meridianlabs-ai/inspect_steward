# steward_tend – Inspect Steward

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
