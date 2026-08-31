# steward_collect – Inspect Steward

Read what has accumulated, and mark how far you have read.

The agent’s view of the run: the decisions that are still the agent’s to act on, where the run stands, and everything that has happened since the last collection. Whatever the filter sets aside is counted rather than dropped, so a shortened section never reads as an empty one.

Advancing the cursor is a bookmark, not a pop — the journal is append-only, nothing is consumed by being read, and an open item stays open until somebody acts on it. `--peek` leaves the cursor where it is.

#### Usage

``` text
steward collect [OPTIONS]
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--peek` | boolean | Read without advancing the cursor, so the next collection sees the same history again. | `False` |
| `--since` | integer range (`0` and above) | Show history from this journal position instead of the last collection. `--since 0` shows everything. | None |
| `--help` | boolean | Show this message and exit. | `False` |
