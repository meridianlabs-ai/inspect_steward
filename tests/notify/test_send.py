"""Getting a post out, and what each target is actually handed.

Real Apprise plugin objects throughout, with only the final `send` intercepted:
the dialect is read off the plugin's own declaration and the conversion layer is
Apprise's, so a stub of either would be testing this test's opinion of upstream
rather than upstream. What is asserted is the two things Steward decides —
**which body each family gets**, and **that a broken channel costs a line in
`steward.log` rather than the turn**.
"""

from pathlib import Path
from typing import Any

import apprise
import pytest
from inspect_steward._notify import (
    Dialect,
    Kind,
    Post,
    by_dialect,
    dialect_of,
    send_post,
)
from inspect_steward._workspace import Workspace

SLACK = "slack://xoxb-1234567890-1234567890-abcdefghij/#general"
MAIL = "mailtos://user:pass@gmail.com"
JSON = "json://localhost/steward"

POST = Post(
    kind=Kind.PROGRESS,
    title="✅ nothing needs you",
    lines=["finished addition@mockllm/model"],
    table=["✓ addition@mockllm/model  4/4  100%"],
    narrow=["✓ addition  4/4  100%"],
)


def servers(instance: Any) -> list[Any]:
    """An instance's targets, typed. Apprise ships no annotations."""
    return list(instance.servers)


class Sent:
    """What each plugin was handed, after Apprise's own conversion layer."""

    def __init__(self) -> None:
        self.bodies: dict[str, str] = {}

    def intercept(self, instance: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        for server in servers(instance):
            name: str = type(server).__name__

            def send(body: str, name: str = name, **kwargs: Any) -> bool:
                self.bodies[name] = body
                return True

            monkeypatch.setattr(server, "send", send)


def workspace_at(root: Path) -> Workspace:
    workspace = Workspace.at(root)
    workspace.root.mkdir(parents=True, exist_ok=True)
    return workspace


FAMILIES = [
    ("slack", SLACK, Dialect.MRKDWN),
    ("email", MAIL, Dialect.HTML),
    ("a json webhook", JSON, Dialect.TEXT),
]


@pytest.mark.parametrize(
    ("url", "dialect"),
    [(url, dialect) for _, url, dialect in FAMILIES],
    ids=[case for case, _, _ in FAMILIES],
)
def test_a_target_declares_its_own_dialect(url: str, dialect: Dialect) -> None:
    # off the built instance rather than off configuration, because the channel
    # may have been named by a variable Steward never inspects as a value
    instance = apprise.Apprise(url)

    assert dialect_of(servers(instance)[0]) is dialect


def test_targets_that_disagree_are_partitioned_rather_than_reduced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # one body for all of them would cost the Slack reader their formatting to
    # spare an inbox that was never going to see it
    instance = apprise.Apprise([SLACK, MAIL, JSON])
    sent = Sent()
    sent.intercept(instance, monkeypatch)

    assert send_post(instance, POST, workspace_at(tmp_path).log).failures == []

    assert len(by_dialect(servers(instance))) == 3
    # the title went as a title, and Slack renders that itself
    assert "nothing needs you" not in sent.bodies["NotifySlack"]
    assert sent.bodies["NotifySlack"].startswith("• finished addition@mockllm/model")
    # the narrowing is the table's, not the whole post's: a phone side-scrolls a
    # monospace block and reads a bullet fine
    assert "```\n✓ addition  4/4  100%\n```" in sent.bodies["NotifySlack"]
    assert sent.bodies["NotifyEmail"].startswith("<pre>")
    assert "```" not in sent.bodies["NotifyJSON"]


def test_nothing_is_converted_on_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `body_format` tells Apprise the body already is what the target declared,
    # so the conversion layer sees input and output agreeing. Its markdown->HTML
    # renders a fence as `<code>` inside `<p>`, which would collapse the table
    instance = apprise.Apprise(MAIL)
    sent = Sent()
    sent.intercept(instance, monkeypatch)

    send_post(instance, POST, workspace_at(tmp_path).log)

    assert sent.bodies["NotifyEmail"].count("<pre>") == 1
    assert "<code>" not in sent.bodies["NotifyEmail"]


def test_a_channel_that_will_not_send_costs_a_line_and_not_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a post is the last thing a turn does and the least important
    workspace = workspace_at(tmp_path)
    instance = apprise.Apprise(SLACK)

    def refuse(*args: Any, **kwargs: Any) -> bool:
        raise OSError("no")

    for server in servers(instance):
        monkeypatch.setattr(server, "send", refuse)

    delivery = send_post(instance, POST, workspace.log)

    assert delivery.reached_nobody
    assert delivery.failures and "mrkdwn" in delivery.failures[0]
    assert "mrkdwn" in workspace.log.read_text(encoding="utf-8")


def test_an_instance_that_cannot_even_be_read_is_reported(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path)

    delivery = send_post(object(), POST, workspace.log)

    assert delivery.reached_nobody
    assert delivery.failures and "notification targets" in delivery.failures[0]
    assert workspace.log.exists()


def test_a_channel_with_no_usable_targets_is_a_failure(tmp_path: Path) -> None:
    # `build_apprise` answers with an empty instance for a URL Apprise cannot
    # parse and for a config file that names nothing. Reporting that as
    # delivered would latch a failure notification that reached nobody
    workspace = workspace_at(tmp_path)

    delivery = send_post(apprise.Apprise(), POST, workspace.log)

    assert delivery.reached_nobody
    assert delivery.failures and "no usable targets" in delivery.failures[0]
    assert "no usable targets" in workspace.log.read_text(encoding="utf-8")
