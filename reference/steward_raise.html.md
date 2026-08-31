# steward_raise – Inspect Steward

Record that an item is now with the person who can decide it.

`ITEM` is a **human-owned** item’s id, or any unambiguous prefix of one — ids are printed beside each item by `steward status`, under the heading that says whose it is. An item the agent owns is its own to investigate and then `steward ack --by agent`; raising one would take it out of the agent’s queue with nobody else looking at it.

The item stays open and stays in `status`: only a ruling closes it. What changes is that `steward collect` stops offering it as work, so the agent is not shown the same decision every time it looks. It returns if the condition changes in a way that matters, because an item’s id is chosen so that it does.

#### Usage

``` text
steward raise [OPTIONS] ITEM
```

#### Options

| Name | Type | Description | Default |
|----|----|----|----|
| `--note` | text | What was done to surface it — where it was asked, and of whom. Optional: handing a decision off does not owe the account that disposing of one does. | \``| |`–json`| boolean | Output the hand-off as JSON. |`False`| |`–help`| boolean | Show this message and exit. |`False\` |
