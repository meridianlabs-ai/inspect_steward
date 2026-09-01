"""One post, four dialects, and the constructs that do not survive the trip.

The whole reason `status_markdown` is not reused is that Slack's `mrkdwn` is a
different dialect rather than a subset: headings, pipe tables, `**bold**` and
`[text](url)` all arrive as literal characters. So the assertions are about
absence as much as presence — what must *not* reach a Slack body — plus the one
construct that has to survive everywhere, which is the fenced progress table
carrying the columns.
"""

import pytest
from inspect_steward._notify import Dialect, Kind, Post, body_format, render

POST = Post(
    kind=Kind.ATTENTION,
    title="⚠️ 2 need a person",
    lines=[
        "a sample is waiting on an approval — inspect acp",
        "the definition has changed since it was captured — steward launch",
    ],
    table=["✓ addition@mockllm/model  4/4  100%", "  4/4 samples · 100%"],
    narrow=["✓ addition  4/4  100%", "  4/4 samples · 100%"],
)


@pytest.mark.parametrize("dialect", list(Dialect))
def test_every_dialect_carries_the_whole_post(dialect: Dialect) -> None:
    body = render(POST, dialect)

    for line in POST.lines:
        assert line in body
    assert "4/4  100%" in body


@pytest.mark.parametrize("dialect", list(Dialect))
def test_the_title_is_not_in_the_body(dialect: Dialect) -> None:
    # it is passed to Apprise separately and every plugin renders it itself --
    # Slack as an attachment heading, mail as the subject -- so a body opening
    # with it arrives as the same line twice
    assert "2 need a person" not in render(POST, dialect)


@pytest.mark.parametrize("dialect", [Dialect.TEXT, Dialect.MARKDOWN, Dialect.MRKDWN])
def test_the_table_arrives_as_a_block(dialect: Dialect) -> None:
    # the one rich construct that behaves in all three, which is why the table
    # rides in a fence rather than in the pipe table `status.md` uses
    body = render(POST, dialect)

    assert "4/4  100%" in body
    if dialect is not Dialect.TEXT:
        assert body.count("```") == 2


def test_slack_gets_the_narrow_table_and_nobody_else_does() -> None:
    # a wide monospace block side-scrolls on a phone, which is where a 3am post
    # is read -- and the rows arrive already padded, so the width is chosen when
    # the table is built rather than trimmed on the way out
    assert "addition@mockllm/model" in render(POST, Dialect.MARKDOWN)
    assert "addition@mockllm/model" not in render(POST, Dialect.MRKDWN)
    assert "✓ addition  4/4" in render(POST, Dialect.MRKDWN)


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
    post = Post(kind=Kind.PROGRESS, title="finished", lines=["<script>&"])

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
def test_a_post_ends_on_the_table_rather_than_a_path(dialect: Dialect) -> None:
    # a post carried a `logs:` line for a reader who could not look the
    # location up. It is the one line nobody reads on a phone, it is long
    # enough to wrap on every one of them, and the location is in `status.md`
    # for the reader who actually wants it
    body = render(POST, dialect)

    assert "logs:" not in body
    assert body.rstrip().endswith("```")
