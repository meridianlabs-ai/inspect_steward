"""The body of a post, as structure rather than markup.

A post is one snapshot spelled four ways — Slack `mrkdwn`, CommonMark, plain text, and the text form wrapped in `<pre>` for mail. So its body is carried as an ordered list of blocks and the dialect is chosen when it is rendered, not when it is built (`render`). This is what lets a single `Post` reach a Slack channel and an inbox in the same send, each in the markup it declared.

**A block carries data, never dialect.** A `Table` in particular carries the padded plain-text lines its builder already produced — `plain_table`'s output — and not a header and rows. That is deliberate and load-bearing: `_notify` is a leaf, imported by `_tend` and importing nothing back from it, so the alignment that needs a `Progress` is done on the `_tend` side and the aligned lines travel here as strings. A renderer here fences or indents them and decides nothing about their columns.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Text:
    """A paragraph of plain lines, optionally under a heading.

    The lines are rendered verbatim in every dialect — a fleet total, a pause reason, the note under a shortened table. Any markup they carry is markup they carry in all four, so the callers keep them plain.
    """

    lines: tuple[str, ...]
    heading: str | None = None


@dataclass(frozen=True)
class Field:
    """A labelled value on one line — `**Signed off** by …`.

    The label is the one thing a dialect emphasises: `**bold**` in markdown, `*bold*` in Slack, and neither in text. Carried as label and value so the renderer decides which.
    """

    label: str
    value: str


@dataclass(frozen=True)
class Bullets:
    """A bulleted list, optionally under a heading — the items a turn is worth waking somebody for."""

    items: tuple[str, ...]
    heading: str | None = None


@dataclass(frozen=True)
class Table:
    """A padded plain-text table, optionally under a heading.

    The lines are `plain_table` output — header and rows already aligned to their columns. A dialect that has a monospace construct fences them; text indents them; nothing re-lays them out.
    """

    lines: tuple[str, ...]
    heading: str | None = None


Block = Text | Field | Bullets | Table


__all__ = ["Block", "Bullets", "Field", "Table", "Text"]
