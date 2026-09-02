# steward_init – Inspect Steward

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
