# steward_runbook – Inspect Steward

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
