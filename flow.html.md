# Inspect Flow – Inspect Steward

## Overview

``` bash
pip install "inspect-steward[flow]"
```

[Inspect Flow](https://github.com/meridianlabs-ai/inspect_flow) specs are fully supported as Steward eval set definitions. Name the spec `config.py` in your workspace directory and the rest of the documentation applies unchanged.

## Worker Overrides

Steward runs `flow run` with three options set:

| Override | Why |
|----|----|
| `--set execution_type=inproc` | One process per task is Steward’s own isolation model, and a venv per worker would put the eval in a grandchild process that Steward cannot track. |
| `--log-dir <scratch>` | Flow writes artifacts and scans for prior logs before [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) is reached. Each worker gets its own scratch directory for that; the run’s log directory is set at the [eval_set()](https://inspect.aisi.org.uk/reference/inspect_ai.html#eval_set) call. |
| `--no-store-read --no-store-write` | Flow’s reuse store would race under fan-out, and buys nothing, since a worker runs the task it was given either way. |

These are all options `flow run` already ships.

## Declarations That Do Not Survive

> **IMPORTANT: Importantexecution_type: venv loses dependencies and python_version**
>
> Flow applies those declarations when it builds the virtualenv. Steward forces `inproc`, so the environment Steward runs in is the environment the eval runs in, and provisioning it is up to you.
>
> A spec that builds its `FlowSpec` in code loses them silently, because Steward reads the definition without executing it. The warning Steward prints for this covers only a top-level declaration in a YAML spec.

> **NOTE: Noteflow run --resume will not resume a Steward run**
>
> Every `flow run` records its log directory as a global pointer, and Steward hands Flow a scratch directory, so a bare `flow run --resume` would resume nothing and start over into a directory nobody is watching.
>
> Under Steward, the resume path is `steward launch` against the workspace, which is also the amend path.

## Scanners

A spec carrying `scanner:` cannot be run by Steward yet. This is refused when the definition is read, rather than discovered when every worker fails.
