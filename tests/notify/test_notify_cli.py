"""The three surfaces a person types at, and the one they never see.

Thin, like every CLI suite here: resolution is `test_channel.py`'s subject and
the triggers are `test_triggers.py`'s. What is only true at this layer is the
shell contract — that `steward notify` refuses the four kinds it does not own,
that a launch with no channel says so **once**, and that a tend which cannot run
at all still reaches somebody, which is the case that is otherwise silent
forever.
"""

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._evalset.manifest import read_manifest
from inspect_steward._notify import (
    INSPECT_NOTIFICATION,
    Delivery,
    Kind,
    Post,
)
from inspect_steward._tend import notify_failure
from inspect_steward._workspace import (
    NOTIFIED,
    Workspace,
    create_workspace,
    read_journal,
    read_notified,
)

from .._logs import SynthTask, write_log
from ..launch._fake import fake_capture
from ..schedule.test_tend import prepared, turn

CHANNEL = "slack://xoxb-1234567890-1234567890-abcdefghij/#general"


# the ambient channel is cleared by `conftest.no_ambient_settings`, which this
# file used to shadow with a weaker copy — and this is the file that actually
# sends, so it was the worst place in the suite to be running without the guard


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    create_workspace(tmp_path, git=False)
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    monkeypatch.chdir(workspace.root)
    return workspace


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch, workspace: Workspace) -> None:
    """A capture agreeing with what is committed, so a launch changes nothing.

    The hint is what these cases are about, and a real capture would spend a
    subprocess to reach the same launch.
    """
    fake_capture(monkeypatch, read_manifest(workspace.manifest))


def recording(monkeypatch: pytest.MonkeyPatch, target: str) -> list[Post]:
    """Intercept the send, keeping what would have gone out.

    The channel itself is `test_send.py`'s subject; what these cases are about
    is whether anything was posted at all, and how many times.
    """
    sent: list[Post] = []

    def capture(instance: Any, post: Post, log: Path) -> Delivery:
        sent.append(post)
        return Delivery(landed=1)

    monkeypatch.setattr(target, capture)
    return sent


def run(*argv: str) -> tuple[int, str]:
    result = CliRunner().invoke(steward, list(argv))
    return result.exit_code, result.output


# --- steward notify -----------------------------------------------------


def test_the_agents_verb_offers_only_the_kinds_it_owns() -> None:
    code, output = run("notify", "--help")

    assert code == 0, output
    assert "[attention|stopped]" in output


@pytest.mark.parametrize("kind", ["progress", "clear", "gate", "signed_off"])
def test_stewards_own_kinds_are_refused_rather_than_undocumented(
    kind: str, workspace: Workspace
) -> None:
    # a hand-sent `gate` is a claim about the run nobody computed, and a
    # hand-sent `signed_off` is a claim that a human adjudicated
    code, output = run("notify", "--kind", kind, "something happened")

    assert code != 0
    assert kind in output


def test_posting_with_no_channel_is_an_error_rather_than_a_shrug(
    workspace: Workspace,
) -> None:
    # the agent asked to tell somebody and nobody was told; exiting zero would
    # let a scaffold record that it had escalated when it had not
    code, output = run("notify", "the grader is failing on 4 samples")

    assert code != 0
    assert "no notification channel" in output


def test_a_post_that_lands_says_which_kind_it_was(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    recording(monkeypatch, "inspect_steward._cli.notify.send_post")

    code, output = run("notify", "--kind", "stopped", "the grader is down")

    assert code == 0, output
    assert "sent (stopped)" in output


AGENT_GLYPHS = [("attention", "⚠️"), ("stopped", "🛑")]


@pytest.mark.parametrize(("kind", "glyph"), AGENT_GLYPHS)
def test_the_agents_post_leads_with_the_same_glyph_stewards_do(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, kind: str, glyph: str
) -> None:
    # a reader scanning a channel sorts on the first character, and an agent
    # saying a run is stopped is the same news as the run being stopped -- so
    # the two arrive looking alike rather than one of them arriving bare
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = recording(monkeypatch, "inspect_steward._cli.notify.send_post")

    code, output = run("notify", "--kind", kind, "the grader is down")

    assert code == 0, output
    assert sent[0].heading == f"{glyph} {workspace.root.name}: the grader is down"


# --- the launch hint ----------------------------------------------------


def test_a_launch_with_no_channel_says_so_once(
    workspace: Workspace, capture: None
) -> None:
    # the feature most costly to miss, and the one whose absence is silent by
    # construction: a run with no channel behaves exactly like a run with one
    # until the night it needs somebody
    code, output = run("launch", "--no-timer")

    assert code == 0, output
    assert output.count("nothing will reach you") == 1
    assert "STEWARD_NOTIFICATION" in output


def test_a_launch_with_a_channel_says_nothing_about_it(
    workspace: Workspace, capture: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEWARD_NOTIFICATION", CHANNEL)

    code, output = run("launch", "--no-timer")

    assert code == 0, output
    assert "nothing will reach you" not in output


UNUSABLE = [
    ("a scheme apprise does not know", "flurble://team/general"),
    ("a config file that is not there", "apprise.yml"),
]


@pytest.mark.parametrize(
    "declared", [value for _, value in UNUSABLE], ids=[case for case, _ in UNUSABLE]
)
def test_a_channel_that_resolves_to_nothing_says_so_at_launch(
    workspace: Workspace, capture: None, declared: str
) -> None:
    # the same failure as no channel at all, arriving with a setting that says
    # otherwise -- which is the worse of the two, since the operator has
    # already done the thing the other message would tell them to do
    workspace.directives.write_text(f"notification: {declared}\n", encoding="utf-8")

    code, output = run("launch", "--no-timer")

    assert code == 0, output
    assert "no usable targets" in output
    # and not the other line, which would tell them to set what they have set
    assert "nothing will reach you if this run needs a person — set" not in output


def test_a_workspace_that_declined_is_not_nagged(
    workspace: Workspace, capture: None
) -> None:
    # nagging somebody who explicitly said no is the overdone version of the
    # same idea
    workspace.directives.write_text("notification: false\n", encoding="utf-8")

    code, output = run("launch", "--no-timer")

    assert code == 0, output
    assert "nothing will reach you" not in output


def test_a_flag_only_channel_says_it_lasts_one_launch(
    workspace: Workspace, capture: None
) -> None:
    """The hint is about a *scheduled* tend, so the flag does not answer it.

    `--notification` shapes this launch's own turn and nothing after it: a
    timer inherits no environment. Suppressing the line because the flag was
    given would leave every later turn silent with nobody told — and asking
    `establish_channel` afterwards would find what the flag itself exported.
    """
    code, output = run("launch", "--no-timer", "--notification", CHANNEL)

    assert code == 0, output
    assert "applies to this launch only" in output


def test_a_flag_alongside_a_durable_channel_says_nothing(
    workspace: Workspace, capture: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEWARD_NOTIFICATION", CHANNEL)

    code, output = run("launch", "--no-timer", "--notification", CHANNEL)

    assert code == 0, output
    assert "applies to this launch only" not in output
    assert "nothing will reach you" not in output


def test_the_flags_cannot_both_be_given(workspace: Workspace) -> None:
    code, output = run(
        "launch", "--no-timer", "--no-notification", "--notification", CHANNEL
    )

    assert code != 0
    assert "Pass whichever you meant" in output


# --- a turn that cannot run at all --------------------------------------


def test_a_failing_tend_posts_once_per_distinct_reason(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that is otherwise silent forever.

    A malformed `_steward.yaml` fails identically every interval, writes
    nothing anybody reads, and leaves `status.md` frozen at whatever the last
    good turn said. The latch is what keeps that from being forty identical
    messages before morning.
    """
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = recording(monkeypatch, "inspect_steward._tend.notify.send_post")

    notify_failure(workspace, "_steward.yaml is not valid YAML")
    notify_failure(workspace, "_steward.yaml is not valid YAML")

    assert len(sent) == 1
    assert sent[0].heading.startswith("🛑 ")
    assert read_notified(read_journal(workspace.journal).events) == {
        "_steward.yaml is not valid YAML"
    }


def test_a_failure_that_could_not_be_delivered_is_not_latched(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a notifier that was briefly unreachable would otherwise silence every
    # later attempt at the same persistent failure -- two outages compounding
    # into exactly the silence this exists to prevent
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    attempts: list[Post] = []

    def refuse(instance: Any, post: Post, log: Path) -> Delivery:
        attempts.append(post)
        return Delivery(failures=["could not post to the mrkdwn channel: timed out"])

    monkeypatch.setattr("inspect_steward._tend.notify.send_post", refuse)

    notify_failure(workspace, "_steward.yaml is not valid YAML")
    notify_failure(workspace, "_steward.yaml is not valid YAML")

    assert len(attempts) == 2
    assert read_notified(read_journal(workspace.journal).events) == set()


def test_a_failure_reaches_a_channel_named_only_in_the_file(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn can fail long before it settles a channel.

    A missing manifest raises in the first few lines, so a caller handing over
    what it happened to have would leave a workspace whose channel is only in
    `_steward.yaml` unable to report the one condition it most needs to.
    """
    workspace.directives.write_text(f"notification: {CHANNEL}\n", encoding="utf-8")
    sent = recording(monkeypatch, "inspect_steward._tend.notify.send_post")

    notify_failure(workspace, "there is no committed manifest")

    assert len(sent) == 1


def test_a_different_failure_is_still_heard(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = recording(monkeypatch, "inspect_steward._tend.notify.send_post")

    notify_failure(workspace, "the log directory could not be read")
    notify_failure(workspace, "_steward.yaml is not valid YAML")

    assert len(sent) == 2


def test_a_turn_that_runs_again_re_arms_the_latch(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a run that broke, was fixed, and broke again the same way is news both
    # times -- a latch that silenced the second is worse than none
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = recording(monkeypatch, "inspect_steward._tend.notify.send_post")

    notify_failure(workspace, "the same failure")
    turn(workspace)
    notify_failure(workspace, "the same failure")

    assert len([post for post in sent if post.kind is Kind.STOPPED]) == 2


def test_a_failing_tend_with_no_channel_writes_nothing(workspace: Workspace) -> None:
    notify_failure(workspace, "_steward.yaml is not valid YAML")

    types = {event.type for event in read_journal(workspace.journal).events}
    assert NOTIFIED not in types


def test_the_command_reports_the_failure_it_could_not_run(
    workspace: Workspace,
) -> None:
    workspace.directives.write_text("max_workers: [8\n", encoding="utf-8")

    code, output = run("tend")

    assert code != 0
    assert "not valid YAML" in output
