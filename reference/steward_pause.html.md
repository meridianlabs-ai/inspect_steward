# steward_pause – Inspect Steward

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
