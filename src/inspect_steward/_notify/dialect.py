"""What markup a target actually understands.

**Slack's `mrkdwn` is a different dialect, not a subset of markdown.** No headings, so `## the run` arrives as those two characters. No tables, so a pipe table arrives as pipes. `*bold*` rather than `**bold**`, and `<url|text>` rather than `[text](url)`. Steward's `status.md` is built from exactly those four constructs, which is why the file renderer is not reused for a post.

**The dialect comes off the built Apprise instance rather than off configuration**, because configuration cannot answer it: the channel may have been named by `INSPECT_EVAL_NOTIFICATION`, which Steward never inspects as a value. Each plugin declares a `notify_format`, and that declaration is the answer — with one override, for the family that declares markdown and means mrkdwn. Reading a plugin's declared output format is not reading a credential, which is what keeps this on the right side of the reference-only rule it sits next to.

**Every dialect is rendered directly and Apprise converts nothing**, which is a decision taken against measurement rather than taste. Apprise will convert markdown to HTML, but its conversion turns a fenced block into `<code>` inside `<p>` — no `<pre>`, so an HTML mail collapses the progress table into one run-on line. Text to HTML keeps the alignment (`&nbsp;` and `<br/>`) and is what the HTML dialect is built from. So each target is handed the format it declared, `body_format` says so, and the conversion layer is never entered.
"""

from enum import StrEnum
from typing import Any

SLACK_FAMILY = frozenset({"NotifySlack", "NotifyRocketChat"})
"""Plugins that declare markdown and mean Slack's `mrkdwn`.

Named by class rather than by scheme because a plugin answers to several schemes and one name. Mattermost is deliberately absent: it declares `text`, so the general rule already routes it somewhere safe, and adding it here would send it markup it never asked for.
"""


class Dialect(StrEnum):
    """The markup a post is written in."""

    TEXT = "text"
    """No markup at all. The progress table is indented rather than fenced."""

    MARKDOWN = "markdown"
    """CommonMark. Headings, `**bold**`, and a fenced table."""

    MRKDWN = "mrkdwn"
    """Slack's dialect: `*bold*`, `<url|text>`, a fenced table, and no headings."""

    HTML = "html"
    """The text rendering escaped inside `<pre>`, which is the one wrapping that keeps a column aligned in a mail client."""


def dialect_of(server: Any) -> Dialect:
    """The dialect one Apprise target speaks.

    Args:
        server: An Apprise plugin instance, as `Apprise.servers` yields them.

    Returns:
        Its dialect. Anything unrecognised is `TEXT`, which every target renders acceptably and none renders as markup it did not ask for.
    """
    if type(server).__name__ in SLACK_FAMILY:
        return Dialect.MRKDWN
    # compared rather than `str()`-ed, because apprise's `NotifyFormat` is a
    # `(str, Enum)` and not a `StrEnum`: `str(NotifyFormat.HTML)` is
    # `'NotifyFormat.HTML'`, which matches nothing and would quietly route every
    # target to text — the failure that looks like a design decision
    declared = getattr(server, "notify_format", None)
    if not isinstance(declared, str):
        return Dialect.TEXT
    if declared == Dialect.MARKDOWN:
        return Dialect.MARKDOWN
    if declared == Dialect.HTML:
        return Dialect.HTML
    return Dialect.TEXT


def by_dialect(servers: list[Any]) -> dict[Dialect, list[Any]]:
    """Group targets by what they understand.

    **Partitioned rather than reduced to the worst of them.** A workspace posting to Slack and to mail wants both rendered properly, and the alternative — one body in the dialect every target survives — would cost the Slack reader their formatting to spare an inbox that was not going to see it anyway. Sending twice is one loop.

    Args:
        servers: The instance's targets.

    Returns:
        Targets by dialect, empty groups absent.
    """
    grouped: dict[Dialect, list[Any]] = {}
    for server in servers:
        grouped.setdefault(dialect_of(server), []).append(server)
    return grouped


__all__ = ["SLACK_FAMILY", "Dialect", "by_dialect", "dialect_of"]
