# steward_ramp – Inspect Steward

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

## steward ramp hold

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

## steward ramp resume

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
