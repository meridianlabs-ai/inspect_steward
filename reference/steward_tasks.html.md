# steward_tasks – Inspect Steward

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
