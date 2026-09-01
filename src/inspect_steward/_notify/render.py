"""One post, written in whichever markup its target understands.

Three renderings and four dialects, because HTML is the text rendering wrapped rather than a document of its own — `<pre>` around escaped plain text is the one construct that keeps a column aligned in every mail client, and it is a line of code rather than a renderer.

**The body never repeats the title.** It is passed to Apprise as a title, and every plugin renders it itself — Slack as an attachment heading, mail as the subject — so a body opening with it arrives as the same line twice. The one plugin family that cannot show a title is handled upstream: Apprise prepends it to the body itself when `title_maxlen` is zero, in the notify format the target declared. So there is nothing here to compensate for, and a renderer that spelled the title anyway would be duplicating it everywhere to serve a case that is already covered.

**`status_markdown` is not reused, and the reason is structural.** That renderer is built from headings, pipe tables, `**bold**` and `[text](url)` — the four constructs that do not survive the trip. What is shared instead is everything below markup: `verdict_line()`, `by_owner()`, and `progress_table()`, so a post and a `status.md` cannot disagree about what a turn found, only about how it is spelled.
"""

from html import escape

from .dialect import Dialect
from .post import Post


def render(post: Post, dialect: Dialect) -> str:
    """The post's body, in one dialect.

    Args:
        post: What to say.
        dialect: What the target understands.

    Returns:
        The body, which never carries the title — that is passed separately and rendered by the target. A post with nothing under its title is the exception: Apprise refuses an empty body, and one line is a body.
    """
    if dialect is Dialect.MRKDWN:
        return _mrkdwn(post)
    if dialect is Dialect.MARKDOWN:
        return _markdown(post)
    if dialect is Dialect.HTML:
        # the text rendering, escaped and wrapped: Apprise will convert
        # markdown to HTML but renders a fenced block as `<code>` inside `<p>`,
        # which collapses the table into one run-on line (`dialect` docstring)
        return f"<pre>{escape(_text(post))}</pre>"
    return _text(post)


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


def _text(post: Post) -> str:
    """No markup at all, and the fallback for anything unrecognised."""
    parts: list[str] = []
    if post.lines:
        parts.append("\n".join(f"  {line}" for line in post.lines))
    if table := post.monospace(narrow=False):
        parts.append("\n".join(f"  {row}" for row in table))
    return _joined(parts, post)


def _markdown(post: Post) -> str:
    """CommonMark: a bullet per line and a fenced table."""
    parts: list[str] = []
    if post.lines:
        parts.append("\n".join(f"- {line}" for line in post.lines))
    if table := post.monospace(narrow=False):
        parts.append("```\n" + "\n".join(table) + "\n```")
    return _joined(parts, post)


def _mrkdwn(post: Post) -> str:
    """Slack's dialect.

    No headings at all — `##` arrives as two characters — and one asterisk for bold where anything is bold. The fenced block is the one rich construct that behaves, which is what carries the table.
    """
    parts: list[str] = []
    if post.lines:
        parts.append("\n".join(f"• {line}" for line in post.lines))
    if table := post.monospace(narrow=True):
        parts.append("```\n" + "\n".join(table) + "\n```")
    return _joined(parts, post)


def _joined(parts: list[str], post: Post) -> str:
    """The parts as a body, or the title where there are none.

    **A post can be nothing but its title**, which is what `steward notify` sends most often — and Apprise refuses a notification with no body at all, so that post would not go out. The title stands in, which is the one place it is spelled in a body and the one place doing so repeats nothing: a target showing its own title beside this is showing a single line twice, and a single line is the whole message either way.
    """
    return "\n\n".join(parts) if parts else post.title


__all__ = ["body_format", "render"]
