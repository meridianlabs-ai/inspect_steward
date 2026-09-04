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
from inspect_steward._cli import main as cli
from inspect_steward._cli.turn import turn_json
from inspect_steward._notify import (
    COMMAND_LINE,
    DIRECTIVES,
    INSPECT_NOTIFICATION,
    STEWARD_NOTIFICATION,
    describe_channel,
    establish_channel,
)
from inspect_steward._tend import status, status_markdown
from inspect_steward._workspace import (
    Directives,
    DirectivesError,
    Workspace,
    declared_notification,
    read_directives,
)

from .._logs import SynthTask
from ..conftest import CHANNELS
from ..schedule.test_tend import prepared

SLACK = "slack://tok-a/tok-b/tok-c"
DISCORD = "discord://1234/abcd"


def workspace_at(root: Path, text: str | None = None) -> Workspace:
    workspace = Workspace.at(root)
    workspace.root.mkdir(parents=True, exist_ok=True)
    if text is not None:
        workspace.directives.write_text(text, encoding="utf-8")
    return workspace


# the ambient channel is cleared by `conftest.no_ambient_settings`, which this
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
    `no_ambient_settings` had removed, so an in-process CLI test running inside
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


def test_a_tests_own_undo_cannot_revoke_the_ambient_channel_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard that keeps a developer's real channel out of the suite must not be revocable by the tests it guards.

    `no_ambient_settings` and the `monkeypatch` a test asks for used to be the same object — the fixture is function-scoped and shared with everything autouse around it — so a test that called `undo()` to drop a stub of its own also dropped the guard, and the next thing it did that resolved a channel posted to a real Slack workspace. It holds its own `MonkeyPatch` now, which is what this asserts: the test's `undo()` reverts the test's patch and reaches nothing else.
    """
    guarded = cli.init_dotenv
    monkeypatch.setattr(cli, "init_dotenv", lambda: None)

    monkeypatch.undo()

    assert cli.init_dotenv is guarded
    # and the variables it cleared at setup are still clear, so the reload every
    # CLI invocation performs cannot put a channel back underneath the test
    cli.init_dotenv()
    assert all(os.environ.get(name) is None for name in CHANNELS)


# --- what the snapshot may say about all of the above -------------------------
#
# The resolution above is correct and was, until this point, unreportable: every
# spelling of it lives somewhere an outside reader cannot see. The one that
# carries a channel most often is a `.env` at or above the workspace — loaded
# into Steward's own process by `init_dotenv()` and into no shell an agent
# holds — so an agent that opened `_steward.yaml`, found the key commented out,
# and told its operator the run could reach nobody was right about everything it
# had looked at and wrong about the run. These are the snapshot being able to
# say otherwise, and being unable to say the URL while it does.

REACHABLE = "json://localhost/"
"""A URL Apprise actually builds a target out of.

Not `SLACK`, which every test above uses and none of them builds: its tokens
are shaped like tokens and are not ones, so Apprise declines the plugin and the
instance comes back holding nothing. That is the right answer — it is what
`usable_channel` is for — and it makes the constant useless for the one thing
these tests need, which is a channel that reaches somewhere.
"""

SPELLINGS: list[tuple[str, dict[str, str], str, str]] = [
    ("the file", {}, f"notification: {REACHABLE}\n", DIRECTIVES),
    ("steward's variable", {STEWARD_NOTIFICATION: REACHABLE}, "", STEWARD_NOTIFICATION),
    ("inspect's variable", {INSPECT_NOTIFICATION: REACHABLE}, "", INSPECT_NOTIFICATION),
]


@pytest.mark.parametrize(
    ("environ", "text", "source"),
    [(environ, text, source) for _, environ, text, source in SPELLINGS],
    ids=[case for case, _, _, _ in SPELLINGS],
)
def test_the_snapshot_names_the_spelling_that_configured_the_channel(
    environ: dict[str, str],
    text: str,
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel is reported however it was configured, under the name it was configured by.

    The name is the point rather than a nicety: *configured* on its own leaves a
    reader unable to tell a committed file from a variable this shell happens to
    hold, and those two facts want different actions from them.
    """
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    workspace.directives.write_text(text, encoding="utf-8")

    channel = status(workspace).notification

    assert channel is not None
    assert channel.source == source
    assert channel.reaches
    assert source in channel.description


def test_a_channel_nobody_configured_is_reported_as_absent(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])

    channel = status(workspace).notification

    assert channel is not None
    assert channel.source is None
    assert not channel.reaches
    assert "none configured" in channel.description


def test_a_channel_that_resolves_to_nothing_is_not_reported_as_reaching_anybody(
    tmp_path: Path,
) -> None:
    """The worse of the two absences, since the operator has already done the thing they would be told to do.

    An Apprise config file that has been moved reads as empty, which is a
    non-empty setting that reaches nobody — and *configured* over one would be
    the reassurance without the capability.
    """
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    workspace.directives.write_text("notification: gone.yml\n", encoding="utf-8")

    channel = status(workspace).notification

    assert channel is not None
    assert channel.source == DIRECTIVES
    assert channel.targets == 0
    assert not channel.reaches
    assert "no usable targets" in channel.description


def test_a_workspace_that_declined_is_reported_as_silent_throughout(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    workspace.directives.write_text("notification: false\n", encoding="utf-8")

    channel = status(workspace).notification

    assert channel is not None
    assert channel.declined and not channel.fleet
    assert channel.description == "declined — nothing posts anywhere"


def test_a_flag_declining_over_a_configured_workspace_says_the_fleet_still_posts() -> (
    None
):
    """The two silences are different runs to be reading about at 2am.

    A worker's notification is a sample stopped on an approval and waiting, so a
    fleet that can still reach somebody is materially unlike one that cannot —
    and `--no-notification` beside a `notification:` produces exactly that pair.
    """
    channel = describe_channel(
        target=None,
        notification=False,
        channel=REACHABLE,
        environ={INSPECT_NOTIFICATION: REACHABLE},
    )

    assert channel.declined and channel.fleet
    assert (
        channel.description == "declined — Steward posts nowhere; the fleet still posts"
    )


def test_a_flag_is_named_as_the_flag_rather_than_as_the_workspace() -> None:
    """It lasts as long as the invocation, and a reader who took it for the workspace's own answer would be reading a channel that is gone by the next turn."""
    channel = describe_channel(
        target=DISCORD, notification=DISCORD, channel=SLACK, environ={}
    )

    assert channel.source == COMMAND_LINE


def test_no_spelling_of_the_channel_reaches_the_snapshot(tmp_path: Path) -> None:
    """An Apprise URL is a bearer token with a scheme in front of it, and this document is written to a file, printed to a terminal, and synced to an object store."""
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    workspace.directives.write_text(f"notification: {REACHABLE}\n", encoding="utf-8")

    result = status(workspace)

    assert result.notification is not None
    assert result.notification.reaches
    assert REACHABLE not in result.notification.description
    assert REACHABLE not in status_markdown(result, header=False)
    assert REACHABLE not in turn_json(result)
