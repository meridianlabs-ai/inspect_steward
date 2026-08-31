# steward_timer – Inspect Steward

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

## steward timer arm

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

## steward timer disarm

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

## steward timer status

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
