"""Where Steward posts, and how the fleet comes to agree with it.

The failure this is all guarding is silent by construction: a fleet notifying
somewhere nobody reads, or not at all, while `status.md` says everything is
fine. So the assertions come in pairs — what `establish_channel` returns for
*Steward*, and what it leaves in the environment for the *workers* — because a
resolution that is right about one and wrong about the other is exactly the
divergence the whole carve-out exists to prevent.
"""

import os
from pathlib import Path

import pytest
from inspect_steward._notify import INSPECT_NOTIFICATION, establish_channel
from inspect_steward._workspace import (
    Directives,
    DirectivesError,
    Workspace,
    declared_notification,
    read_directives,
)

SLACK = "slack://tok-a/tok-b/tok-c"
DISCORD = "discord://1234/abcd"


def workspace_at(root: Path, text: str | None = None) -> Workspace:
    workspace = Workspace.at(root)
    workspace.root.mkdir(parents=True, exist_ok=True)
    if text is not None:
        workspace.directives.write_text(text, encoding="utf-8")
    return workspace


# the ambient channel is cleared by `conftest.no_ambient_channel`, which this
# file used to shadow with a local copy of the same idea. The copy was the
# weaker of the two — it cleared the variables and nothing else — and shadowing
# is silent, so the module about channels was the one module the suite's own
# guard against posting to a real one did not cover

ACCEPTED = [
    ("one url", SLACK),
    ("several urls", f"{SLACK},{DISCORD}"),
]


@pytest.mark.parametrize(
    ("text", "target"), ACCEPTED, ids=[case for case, _ in ACCEPTED]
)
def test_the_file_names_the_channel_for_steward_and_the_fleet_alike(
    text: str, target: str, tmp_path: Path
) -> None:
    workspace = workspace_at(tmp_path, f"notification: {target}\n")

    settled = establish_channel(workspace, read_directives(workspace.directives))

    assert settled == target
    # the half a return value cannot buy: `_worker.spawn` spreads the
    # environment into every worker, and this is what puts the channel in it
    assert os.environ[INSPECT_NOTIFICATION] == target


def test_a_config_file_is_made_absolute_against_the_workspace(tmp_path: Path) -> None:
    # a worker's cwd is the workspace root, but nothing says the process that
    # resolved this was standing there -- a scheduled tend certainly is not
    workspace = workspace_at(tmp_path, "notification: apprise.yml\n")

    settled = establish_channel(workspace, read_directives(workspace.directives))

    assert settled == str(tmp_path / "apprise.yml")


def test_a_url_is_left_exactly_as_it_is(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path, f"notification: {SLACK}\n")

    assert establish_channel(workspace, read_directives(workspace.directives)) == SLACK


def test_inspects_variable_answers_where_steward_has_no_opinion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # declaring the channel inspect's way is enough: the relationship is
    # reflexive, and this is the direction that needs no export
    monkeypatch.setenv(INSPECT_NOTIFICATION, SLACK)
    workspace = workspace_at(tmp_path, "max_workers: 2\n")

    assert establish_channel(workspace, read_directives(workspace.directives)) == SLACK


def test_stewards_spelling_wins_over_inspects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the narrower name winning is the rule `read_overrides` applies to every
    # other pair, and here it is load-bearing: leaving a differing variable in
    # place would have Steward post to one channel and its fleet to another
    monkeypatch.setenv(INSPECT_NOTIFICATION, DISCORD)
    workspace = workspace_at(tmp_path, f"notification: {SLACK}\n")

    settled = establish_channel(workspace, read_directives(workspace.directives))

    assert settled == SLACK
    assert os.environ[INSPECT_NOTIFICATION] == SLACK


def test_the_variable_beats_the_file_and_the_flag_beats_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = workspace_at(tmp_path, f"notification: {SLACK}\n")
    monkeypatch.setenv("STEWARD_NOTIFICATION", DISCORD)
    directives = read_directives(workspace.directives)

    assert establish_channel(workspace, directives) == DISCORD
    assert (
        establish_channel(workspace, directives, notification="json://host")
        == "json://host"
    )


def test_declining_silences_steward_and_leaves_the_fleets_channel_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a worker's notifications are blocking human-in-the-loop moments, and
    # silencing one hangs a sample with nobody told
    monkeypatch.setenv(INSPECT_NOTIFICATION, SLACK)
    workspace = workspace_at(tmp_path, "notification: false\n")

    assert establish_channel(workspace, read_directives(workspace.directives)) is None
    assert os.environ[INSPECT_NOTIFICATION] == SLACK


def test_the_flag_can_decline_what_the_file_configured(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path, f"notification: {SLACK}\n")

    settled = establish_channel(
        workspace, read_directives(workspace.directives), notification=False
    )

    assert settled is None


def test_nothing_configured_anywhere_is_no_channel(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path, "")

    assert establish_channel(workspace, read_directives(workspace.directives)) is None


def test_a_dotenv_beside_a_command_cannot_restore_a_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole the suite's own isolation had, pinned where it can be seen.

    Every `steward` invocation reads `.env` from the cwd upward — deliberately,
    because a scheduled tend runs under a stripped environment and needs to see
    what its workers see. In a test that put back exactly what `conftest`'s
    `no_ambient_channel` had removed, so an in-process CLI test running inside
    a repository with a channel in its `.env` had a live one underneath it and
    would have posted to it for real.
    """
    from click.testing import CliRunner
    from inspect_steward._cli.main import steward

    (tmp_path / ".env").write_text(
        "STEWARD_NOTIFICATION=slack://real/channel\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    CliRunner().invoke(steward, ["status"])

    assert os.environ.get("STEWARD_NOTIFICATION") is None


REFUSED = [
    ("says nothing about where", "notification: true\n", "says nothing about"),
    ("reads as a target called none", "notification: none\n", "is `false` now"),
    ("empty", 'notification: ""\n', "not an empty value"),
]


@pytest.mark.parametrize(
    ("text", "says"),
    [(text, says) for _, text, says in REFUSED],
    ids=[case for case, _, _ in REFUSED],
)
def test_a_channel_that_says_nothing_is_refused(
    text: str, says: str, tmp_path: Path
) -> None:
    workspace = workspace_at(tmp_path, text)

    with pytest.raises(DirectivesError, match=says):
        read_directives(workspace.directives)


def test_the_channel_survives_a_file_that_will_not_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a malformed `_steward.yaml` is among the conditions most worth notifying
    # about, and it is also where the channel would normally be read from
    workspace = workspace_at(tmp_path, "max_workers: [8\n")
    with pytest.raises(DirectivesError):
        read_directives(workspace.directives)

    assert establish_channel(workspace, None) is None

    monkeypatch.setenv("STEWARD_NOTIFICATION", SLACK)
    assert establish_channel(workspace, None) == SLACK


ENVIRONMENT = [
    ("a url", SLACK, SLACK),
    ("declining", "false", False),
    ("unset", None, None),
    ("exported empty", "   ", None),
    # unusable rather than raising: this path is reached because something
    # already failed, and a second failure on the way to reporting the first
    # is how a broken workspace goes silent
    ("a value the field refuses", "true", None),
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(value, expected) for _, value, expected in ENVIRONMENT],
    ids=[case for case, _, _ in ENVIRONMENT],
)
def test_the_variable_read_on_its_own(
    value: str | None, expected: str | bool | None
) -> None:
    environ = {} if value is None else {"STEWARD_NOTIFICATION": value}

    assert declared_notification(environ) == expected


def test_the_key_is_stewards_rather_than_an_override_alias() -> None:
    # `STEWARD_NOTIFICATION` used to mean `eval_set(notification=…)` for one
    # run. Excluded from the alias table so that it reaches the settings instead
    assert Directives.model_validate({"notification": SLACK}).notification == SLACK
