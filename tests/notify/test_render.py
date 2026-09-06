"""One post, four dialects, and the constructs that do not survive the trip.

A post carries its body as blocks (`_notify.block`) and the dialect is chosen when
it is rendered. Slack's `mrkdwn` is a different dialect rather than a subset:
headings, pipe tables, `**bold**` and `[text](url)` all arrive as literal
characters. So the assertions are about absence as much as presence — what must
*not* reach a Slack body — plus the constructs that have to survive everywhere: a
fenced table carrying its columns, and a bulleted list of what changed.
"""

import pytest
from inspect_steward._notify import (
    Bullets,
    Dialect,
    Field,
    Kind,
    Post,
    Table,
    Text,
    body_format,
    render,
)

ITEMS = (
    "a sample is waiting on an approval — inspect acp",
    "the definition has changed since it was captured — steward launch",
)

POST = Post(
    kind=Kind.ATTENTION,
    title="⚠️ 2 need an operator",
    blocks=(
        Bullets(ITEMS),
        Text(("4/4 samples · 100%",)),
        Table(
            (
                "task      samples  done  score",
                "addition      4/4  100%   1.00",
            )
        ),
        Field("Logs", "`s3://bucket/run`"),
    ),
)


@pytest.mark.parametrize("dialect", list(Dialect))
def test_every_dialect_carries_the_whole_post(dialect: Dialect) -> None:
    body = render(POST, dialect)

    for item in ITEMS:
        assert item in body
    assert "4/4  100%" in body


@pytest.mark.parametrize("dialect", list(Dialect))
def test_the_title_is_not_in_the_body(dialect: Dialect) -> None:
    # it is passed to Apprise separately and every plugin renders it itself --
    # Slack as an attachment heading, mail as the subject -- so a body opening
    # with it arrives as the same line twice
    assert "2 need an operator" not in render(POST, dialect)


@pytest.mark.parametrize("dialect", [Dialect.TEXT, Dialect.MARKDOWN, Dialect.MRKDWN])
def test_the_table_arrives_as_a_block(dialect: Dialect) -> None:
    # the one rich construct that behaves in all three, which is why the table
    # rides in a fence rather than in the pipe table `status.md` uses
    body = render(POST, dialect)

    assert "4/4  100%" in body
    if dialect is not Dialect.TEXT:
        assert body.count("```") == 2


def test_the_table_is_the_same_width_for_everyone() -> None:
    # the post is the phone surface whatever the target, so its table arrives at
    # one width for every dialect -- the rows are padded once when the post is
    # built, not trimmed per dialect on the way out
    row = "addition      4/4  100%   1.00"
    assert row in render(POST, Dialect.MARKDOWN)
    assert row in render(POST, Dialect.MRKDWN)
    assert row in render(POST, Dialect.TEXT)


def test_a_heading_takes_the_dialect_it_can_show() -> None:
    # no headings in Slack -- `### h` arrives as those characters -- so it takes
    # the one emphasis the dialect has instead
    post = Post(
        kind=Kind.HEARTBEAT, title="x", blocks=(Text(("up",), heading="resources"),)
    )

    assert "### resources" in render(post, Dialect.MARKDOWN)
    assert "*resources*" in render(post, Dialect.MRKDWN)
    assert "###" not in render(post, Dialect.MRKDWN)


def test_a_field_takes_the_bold_the_dialect_has() -> None:
    post = Post(kind=Kind.HEARTBEAT, title="x", blocks=(Field("Logs", "`/tmp/run`"),))

    assert "**Logs** `/tmp/run`" in render(post, Dialect.MARKDOWN)
    assert "*Logs* `/tmp/run`" in render(post, Dialect.MRKDWN)
    assert "**" not in render(post, Dialect.MRKDWN)


ABSENT = [
    ("a heading", "##"),
    ("a pipe table", "| --- |"),
    ("commonmark bold", "**"),
    ("a commonmark link", "]("),
]


@pytest.mark.parametrize(
    ("construct", "text"), ABSENT, ids=[case for case, _ in ABSENT]
)
def test_what_slack_would_render_literally_never_reaches_it(
    construct: str, text: str
) -> None:
    assert text not in render(POST, Dialect.MRKDWN)


def test_slack_leads_with_what_changed_rather_than_with_markup() -> None:
    # the heading Slack shows is the title it was handed, so the body starts at
    # the first thing the title does not already say
    assert render(POST, Dialect.MRKDWN).startswith("• a sample is waiting")


def test_html_is_the_text_rendering_wrapped() -> None:
    # `<pre>` around escaped plain text is the one construct that keeps a column
    # aligned in every mail client. Apprise's own markdown->HTML renders a fence
    # as `<code>` inside `<p>`, which collapses the table into one line
    body = render(POST, Dialect.HTML)

    assert body.startswith("<pre>") and body.endswith("</pre>")
    assert "4/4  100%" in body


def test_html_escapes_what_a_task_name_might_contain() -> None:
    post = Post(kind=Kind.PROGRESS, title="finished", blocks=(Bullets(("<script>&",)),))

    assert "&lt;script&gt;&amp;" in render(post, Dialect.HTML)


FORMATS = [
    (Dialect.TEXT, "text"),
    (Dialect.MARKDOWN, "markdown"),
    # mrkdwn claims markdown, which is what the Slack plugin declares -- so the
    # conversion layer sees input and output agreeing and passes the body
    # through untouched, which is the whole point of rendering per dialect
    (Dialect.MRKDWN, "markdown"),
    (Dialect.HTML, "html"),
]


@pytest.mark.parametrize(("dialect", "declared"), FORMATS)
def test_apprise_is_told_the_body_is_already_what_it_is(
    dialect: Dialect, declared: str
) -> None:
    assert body_format(dialect) == declared


def test_a_post_with_nothing_but_a_title_renders_as_one() -> None:
    # what `steward notify` sends most often -- and the one place the title is
    # spelled in a body, because Apprise refuses a notification with none and a
    # post that would not go out is worse than a line shown twice
    post = Post(kind=Kind.ATTENTION, title="the grader is failing on 4 samples")

    for dialect in Dialect:
        assert "the grader is failing on 4 samples" in render(post, dialect)


@pytest.mark.parametrize("dialect", [Dialect.MARKDOWN, Dialect.MRKDWN])
def test_a_post_ends_on_the_logs_field(dialect: Dialect) -> None:
    # the operator's page ends on where the logs are, and a post carries the same
    # content -- so the reader who wants the location has it, and it lands last
    # because it is the one line that never changes
    body = render(POST, dialect)

    assert "Logs" in body
    assert body.rstrip().endswith("`s3://bucket/run`")
