"""One post, written in whichever markup its target understands.

Four dialects over one list of blocks: HTML is the text rendering escaped and wrapped in `<pre>`, which is the one construct that keeps a column aligned in every mail client, so it is a line of code rather than a renderer of its own.

**The body never repeats the title.** It is passed to Apprise as a title, and every plugin renders it itself — Slack as an attachment heading, mail as the subject — so a body opening with it arrives as the same line twice. The one plugin family that cannot show a title is handled upstream: Apprise prepends it to the body itself when `title_maxlen` is zero, in the notify format the target declared. So there is nothing here to compensate for.

**`status.md` is not spelled here, but its content is.** The post carries the same blocks the operator's page is built from (`_tend.notify._page`), assembled from the same cell builders (`task_table_cells`, `outcomes_grid`, `resources_cells`), so a post and a `status.md` cannot disagree about what a turn found. What is left to this file is only how a block is spelled: a heading is `### h` in CommonMark and `*h*` in Slack, a table is a code fence in both and an indented block in plain text, and bold is `**` or `*` or nothing. `_tend` did the alignment — a `Table` arrives with its columns already padded — so nothing here decides a column, which is also what keeps `_notify` a leaf that imports nothing back from `_tend`.
"""

from html import escape

from .block import Block, Bullets, Field, Text
from .dialect import Dialect
from .post import Post


def render(post: Post, dialect: Dialect) -> str:
    """The post's body, in one dialect.

    Args:
        post: What to say.
        dialect: What the target understands.

    Returns:
        The body, which never carries the title — that is passed separately and rendered by the target. A post with nothing but its title renders as the title: Apprise refuses an empty body, and one line is a body.
    """
    if dialect is Dialect.HTML:
        # the text rendering, escaped and wrapped: Apprise will convert markdown
        # to HTML but renders a fenced block as `<code>` inside `<p>`, which
        # collapses the table into one run-on line (`dialect` docstring)
        return f"<pre>{escape(_body(post, Dialect.TEXT))}</pre>"
    return _body(post, dialect)


def body_format(dialect: Dialect) -> str:
    """What to tell Apprise the body already is, so that it converts nothing.

    Args:
        dialect: What was rendered.

    Returns:
        Apprise's name for that format. `mrkdwn` claims `markdown`, which is what the Slack plugin declares — so the conversion layer sees input and output agreeing and passes the body through untouched.
    """
    if dialect is Dialect.MRKDWN:
        return "markdown"
    return str(dialect)


def _body(post: Post, dialect: Dialect) -> str:
    """The blocks rendered and joined, or the title where there are none.

    **A post can be nothing but its title**, which is what `steward notify` sends most often — and Apprise refuses a notification with no body at all, so that post would not go out. The title stands in, which is the one place it is spelled in a body and the one place doing so repeats nothing: a target showing its own title beside this is showing a single line twice, and a single line is the whole message either way.
    """
    parts = [chunk for block in post.blocks if (chunk := _block(block, dialect))]
    return "\n\n".join(parts) if parts else post.title


def _block(block: Block, dialect: Dialect) -> str:
    """One block in one dialect, or the empty string where it has nothing to show."""
    if isinstance(block, Field):
        return _field(block, dialect)
    if isinstance(block, Text):
        body = _prose(block.lines, dialect)
    elif isinstance(block, Bullets):
        body = _bullets(block.items, dialect)
    else:
        body = _table(block.lines, dialect)
    if not body:
        return ""
    return "\n".join([*_heading(block.heading, dialect), *body])


def _heading(heading: str | None, dialect: Dialect) -> list[str]:
    """A block's heading, spelled for the dialect, or nothing where it has none.

    No headings in Slack — `### h` arrives as those characters — so it takes the one emphasis the dialect has instead; plain text drops the markup and keeps the word.
    """
    if heading is None:
        return []
    if dialect is Dialect.MRKDWN:
        return [f"*{heading}*"]
    if dialect is Dialect.MARKDOWN:
        return [f"### {heading}"]
    return [heading]


def _prose(lines: tuple[str, ...], dialect: Dialect) -> list[str]:
    """A paragraph's lines, indented in plain text and verbatim elsewhere."""
    return [f"  {line}" for line in lines] if dialect is Dialect.TEXT else list(lines)


def _bullets(items: tuple[str, ...], dialect: Dialect) -> list[str]:
    """A list, with the bullet each dialect draws — Slack's dot, CommonMark's dash, and an indent for text."""
    if dialect is Dialect.MRKDWN:
        return [f"• {item}" for item in items]
    if dialect is Dialect.MARKDOWN:
        return [f"- {item}" for item in items]
    return [f"  {item}" for item in items]


def _table(lines: tuple[str, ...], dialect: Dialect) -> list[str]:
    """A padded table, fenced where the dialect has a monospace block and indented in plain text.

    The lines arrive already aligned (`plain_table`), so a fence or an indent is the whole of what a dialect adds. Plain text takes the indent because a fence is markup it does not render — the columns line up either way.
    """
    if dialect is Dialect.TEXT:
        return [f"  {line}" for line in lines]
    return ["```", *lines, "```"]


def _field(block: Field, dialect: Dialect) -> str:
    """A labelled value, with the label emphasised the way the dialect emphasises anything."""
    if dialect is Dialect.MRKDWN:
        return f"*{block.label}* {block.value}"
    if dialect is Dialect.MARKDOWN:
        return f"**{block.label}** {block.value}"
    return f"  {block.label} {block.value}"


__all__ = ["body_format", "render"]
