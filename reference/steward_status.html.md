# steward_status – Inspect Steward

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
| `--format` | choice (`text` \| `md`) | `text` for a terminal; `md` for an agent relaying this to somebody, which is what agent.md asks it to do verbatim. | `text` |
| `--json` | boolean | Output the state as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |
