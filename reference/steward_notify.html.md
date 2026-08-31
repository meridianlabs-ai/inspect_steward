# steward_notify – Inspect Steward

Post MESSAGE to this run’s notification channel.

MESSAGE is the title — the line that stands alone in a phone notification, so make it the thing you would want read if nothing else was. Everything else goes in –detail.

The channel is the run’s own: `notification` in \_steward.yaml, STEWARD_NOTIFICATION, or INSPECT_EVAL_NOTIFICATION, whichever is set.

#### Usage

``` text
steward notify [OPTIONS] MESSAGE
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--kind` | choice (`attention` \| `stopped`) | Why you are sending this. `attention` is worth knowing and work continues; `stopped` means nothing progresses until a person answers. | `attention` |
| `--detail` | text | A supporting line, under the message. Repeatable — one per thing you want the reader to see without opening anything. | None |
| `--help` | boolean | Show this message and exit. | `False` |
