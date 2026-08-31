# steward_ack – Inspect Steward

Accept an open item, so that nothing reports it again.

`ITEM` is an item id, or any unambiguous prefix of one — ids are printed beside each item by `steward status`.

The item leaves `status.md`, the tend summary, and the verdict; the journal keeps the record. It comes back only if the condition changes in a way that matters, because an item’s id is chosen so that it does: acknowledging one edit to a definition does not acknowledge the next one.

#### Usage

``` text
steward ack [OPTIONS] ITEM
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--reason` | text | Why this is being accepted. Recorded in the journal, and the only account of the decision that survives. | \_required |
| `--by` | choice (`human` \| `agent`) | Who decided. An agent relaying a person’s answer records `human`; one disposing of something on its own judgement records `agent`. | `human` |
| `--json` | boolean | Output the acknowledgment as JSON. | `False` |
| `--help` | boolean | Show this message and exit. | `False` |
