"""`steward timer` and `steward pause`, as a shell meets them.

Thin where the layers below are already covered: what is only true here is the
shell contract. Arming records something a later turn reads, refusing is a
message rather than a traceback, and pausing is one append that a tend in flight
cannot lose.

Cron is the backend throughout, driven by an in-memory crontab (`_fake`), so
nothing here installs anything on the machine running the tests. It is chosen
over the other two because cron's whole interface is one file — which means a
fake of it is a fake of a file rather than of a scheduler, and an `arm` can still
be followed by asking whether it took.
"""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._workspace import (
    Claim,
    Workspace,
    acquire,
    create_workspace,
    read_armed,
    read_journal,
    read_pause,
)

from .._logs import SynthTask, write_log
from ..schedule.test_tend import prepared
from ._fake import FakeCrontab, clear_credentials, fake_cron

TASK = SynthTask("probe", samples=4)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A settled run, entered as a shell would enter it."""
    create_workspace(tmp_path, git=False)
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    monkeypatch.chdir(workspace.root)
    return workspace


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm from a shell holding nothing worth losing (`_fake`)."""
    clear_credentials(monkeypatch)


@pytest.fixture(autouse=True)
def crontab(monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    """Cron, with its one file in memory rather than on the machine.

    Patched rather than passed in, because the point of these tests is the path
    the shell actually takes and the CLI does not thread a runner.
    """
    return fake_cron(monkeypatch)


@pytest.fixture
def pending(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Workspace:
    """A run with work left, which is the only kind supervision matters to.

    The module's default workspace is settled on purpose — most of these cases
    are about a command rather than about a turn — but a finished run needs no
    timer, so anything asserting on supervision has to have something to
    supervise.
    """
    unfinished, _ = prepared(tmp_path, [TASK, SynthTask("waiting", samples=4)])
    write_log(unfinished.logs, TASK)
    monkeypatch.chdir(unfinished.root)
    return unfinished


def run(*argv: str) -> tuple[int, str]:
    result = CliRunner().invoke(steward, list(argv))
    return result.exit_code, result.output


def armed(workspace: Workspace) -> object:
    return read_armed(read_journal(workspace.journal).events)


# --- arming -------------------------------------------------------------


def test_arming_records_what_it_installed(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    code, output = run("timer", "arm", "--interval", "30m", "--scheduler", "cron")

    assert code == 0, output
    assert "every 30m" in output
    recorded = read_armed(read_journal(workspace.journal).events)
    assert recorded is not None
    assert (recorded.scheduler, recorded.interval) == ("cron", 1800)
    # and the machine really was asked, rather than only the journal written
    assert crontab.text is not None and "*/30 * * * *" in crontab.text


def test_the_interval_comes_from_steward_md_when_no_flag_says_otherwise(
    workspace: Workspace,
) -> None:
    workspace.directives.write_text("---\ntend_interval: 1h\n---\n", encoding="utf-8")

    code, output = run("timer", "arm", "--scheduler", "cron")

    assert code == 0, output
    recorded = read_armed(read_journal(workspace.journal).events)
    assert recorded is not None and recorded.interval == 3600


def test_arming_twice_records_one_timer(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")
    run("timer", "arm", "--interval", "1h", "--scheduler", "cron")

    recorded = read_armed(read_journal(workspace.journal).events)
    assert recorded is not None and recorded.interval == 3600
    _, output = run("timer", "status")
    assert "1h" in output
    # one block rather than two that both fire, which is the half of
    # "idempotent" the journal cannot show
    assert crontab.text is not None
    assert crontab.text.count("# >>>") == 1
    assert "*/30" not in crontab.text


def test_a_scheduler_that_cannot_run_here_is_refused_rather_than_substituted(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # somebody who asked for systemd wants to know it is absent, not to find out
    # three days later that something else was installed
    monkeypatch.setattr("inspect_steward._timer.systemd.sys.platform", "darwin")

    code, output = run("timer", "arm", "--scheduler", "systemd")

    assert code == 1
    assert "systemd cannot run a timer here" in output
    assert "Traceback" not in output
    assert armed(workspace) is None


def test_an_interval_without_a_unit_is_refused(workspace: Workspace) -> None:
    code, output = run("timer", "arm", "--interval", "10")

    assert code == 1
    assert "unit" in output
    assert armed(workspace) is None


# --- the environment check ----------------------------------------------


def test_arming_refuses_a_timer_that_would_lose_this_shell_s_credentials(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    code, output = run("timer", "arm", "--scheduler", "cron")

    assert code == 1
    assert "ANTHROPIC_API_KEY" in output
    assert str(workspace.env) in output
    # and the secret itself is nowhere in it
    assert "sk-ant-secret" not in output
    assert armed(workspace) is None


def test_a_dotenv_holding_the_key_arms_normally(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    workspace.env.write_text("ANTHROPIC_API_KEY=sk-ant-secret\n", encoding="utf-8")

    code, output = run("timer", "arm", "--scheduler", "cron")

    assert code == 0, output


def test_the_check_can_be_declined(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a timer meant to run without them is a real thing to want
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    code, output = run("timer", "arm", "--scheduler", "cron", "--no-env-check")

    assert code == 0, output
    assert armed(workspace) is not None


def test_the_workspace_dotenv_is_ignored_by_git(workspace: Workspace) -> None:
    # the check tells people to write one, so init has to have made that safe
    assert ".env" in (workspace.root / ".gitignore").read_text(encoding="utf-8")


def test_arming_an_older_workspace_closes_the_gitignore_gap(
    workspace: Workspace,
) -> None:
    """A workspace created before `.env` was an entry, which no re-`init` reaches.

    `init` gained the entry; existing workspaces did not, and nothing re-runs
    `init`. So the command that tells somebody to put their API keys in `.env`
    is the one that has to make sure git will ignore it — the alternative is
    advice that leaks credentials into a commit, which is worse than no advice.
    """
    ignore = workspace.root / ".gitignore"
    ignore.write_text("logs/\nlogs-archive/\n.steward/\n", encoding="utf-8")

    code, output = run("timer", "arm", "--interval", "30m", "--scheduler", "cron")

    assert code == 0, output
    assert ".env" in output
    assert ".env" in ignore.read_text(encoding="utf-8")


# --- when the journal is the thing that is broken -----------------------


def test_disarming_will_not_claim_nothing_is_armed_when_it_cannot_tell(
    workspace: Workspace,
) -> None:
    """An unreadable journal is an unanswered question, not an empty history.

    This fold is the only record of which backend holds the entry, so reading the
    error as *nothing was armed* would print exactly that while the timer goes on
    tending every interval — and leave nothing able to find it again.
    """
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")
    workspace.journal.unlink()
    workspace.journal.mkdir()

    code, output = run("timer", "disarm")

    assert code == 1
    assert "no timer was armed" not in output
    assert "journal could not be read" in output
    assert "Traceback" not in output


def test_a_timer_the_journal_cannot_record_is_removed_again(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    """An installed timer that nothing recorded is one nothing can remove.

    `disarm` looks the backend up in the journal, so an arming whose event did
    not land leaves the machine tending every interval with no way to stop it
    short of editing a crontab by hand. Undoing the arming is the only exit that
    leaves the two halves agreeing.
    """
    workspace.journal.chmod(0o444)

    code, output = run("timer", "arm", "--interval", "30m", "--scheduler", "cron")

    assert code == 1
    assert "journal" in output
    assert crontab.text is not None and "inspect_steward" not in crontab.text


# --- what a timer needs to still be there tomorrow ----------------------


def test_the_timer_log_outlives_a_cleared_cache(workspace: Workspace) -> None:
    """Why a scheduled tend's output does not live under `.steward/`.

    A scheduler is handed one absolute output path, at arming, and never asked
    again. `.steward/` is documented as safe to delete — but a launchd job whose
    `StandardOutPath` directory has gone does not run at all, so putting the
    timer log there would mean clearing a cache silently disables supervision.
    That is the one direction this must not fail in, and unlike a pause it is not
    even visible afterwards.
    """
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")
    shutil.rmtree(workspace.state)

    assert workspace.timer_log.parent.exists()
    # and it is still not something git should see, now that it is at the root
    ignored = (workspace.root / ".gitignore").read_text(encoding="utf-8")
    assert workspace.timer_log.name in ignored


# --- disarming and status -----------------------------------------------


def test_disarming_removes_what_arming_recorded(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")

    code, output = run("timer", "disarm")

    assert code == 0, output
    assert "disarmed cron" in output
    assert armed(workspace) is None
    assert crontab.text is not None and "inspect_steward" not in crontab.text


def test_disarming_nothing_says_so_rather_than_failing(workspace: Workspace) -> None:
    code, output = run("timer", "disarm")

    assert code == 0
    assert "no timer was armed" in output


def test_status_on_an_unarmed_workspace_names_the_next_command(
    workspace: Workspace,
) -> None:
    code, output = run("timer", "status")

    assert code == 0
    assert "no timer is armed" in output
    assert "steward timer arm" in output


def test_status_reports_a_timer_the_scheduler_no_longer_holds(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    # the journal is what every turn trusts, so this is the run believing it is
    # supervised while it is not
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")
    crontab.text = ""  # somebody ran `crontab -e` and cleared it

    code, output = run("timer", "status")

    assert code == 0
    assert "has no entry for it" in output
    assert "steward timer arm" in output


def test_status_reports_an_interval_the_workspace_no_longer_asks_for(
    workspace: Workspace,
) -> None:
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")
    workspace.directives.write_text("---\ntend_interval: 5m\n---\n", encoding="utf-8")

    code, output = run("timer", "status")

    assert code == 0
    assert "now asks for 5m" in output


def test_status_as_json_is_a_document(workspace: Workspace) -> None:
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")

    code, output = run("timer", "status", "--json")

    assert code == 0
    payload = json.loads(output)
    assert payload["scheduler"] == "cron"
    assert payload["interval"] == 1800
    assert payload["present"] is True


# --- pause and resume ---------------------------------------------------


def test_pausing_records_who_and_why(workspace: Workspace) -> None:
    code, output = run("pause", "--reason", "waiting on a quota increase")

    assert code == 0, output
    assert "⏸" in output
    paused = read_pause(read_journal(workspace.journal).events)
    assert paused is not None
    assert (paused.by, paused.reason) == ("human", "waiting on a quota increase")


def test_pausing_refuses_without_a_reason(workspace: Workspace) -> None:
    # a later reader has to be able to tell a deliberate hold from a forgotten one
    code, output = run("pause")

    assert code != 0
    assert "--reason" in output


def test_pausing_twice_says_who_did_it_first(workspace: Workspace) -> None:
    run("pause", "--reason", "the first reason")

    code, output = run("pause", "--reason", "the second reason")

    assert code != 0
    assert "already paused" in output
    assert "the first reason" in output


def test_resuming_clears_it(workspace: Workspace) -> None:
    run("pause", "--reason", "briefly")

    code, output = run("resume")

    assert code == 0, output
    assert read_pause(read_journal(workspace.journal).events) is None


def test_resuming_a_run_that_is_not_paused_says_so(workspace: Workspace) -> None:
    code, output = run("resume")

    assert code != 0
    assert "not paused" in output


def test_pausing_does_not_take_the_claim(workspace: Workspace) -> None:
    # the moment somebody most wants to pause is the moment a tend is in flight
    # spawning the workers they want stopped
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)

    with outcome:
        code, output = run("pause", "--reason", "mid-tend")

    assert code == 0, output


def test_pausing_survives_a_cleared_cache(workspace: Workspace, tmp_path: Path) -> None:
    """The reason this is a journal event and not a file under `.steward/`.

    That directory is documented as safe to delete, so a pause living there
    would mean clearing a cache silently resumes an expensive run — the one
    direction this must not fail in. The manifest is re-committed afterwards
    because it lives there too and a real recovery would relaunch; what is
    being checked is that the *pause* did not go with it.
    """
    run("pause", "--reason", "hold everything")
    shutil.rmtree(workspace.state)
    prepared(tmp_path, [TASK])

    assert read_pause(read_journal(workspace.journal).events) is not None
    assert "⏸" in run("status")[1]


def test_a_paused_run_does_not_blame_the_ceiling(pending: Workspace) -> None:
    # everything queues while paused, and naming the ceiling as the reason
    # points a reader at the one thing they might go and change
    run("pause", "--reason", "hold everything")

    _, output = run("status")

    assert "1 waiting on a resume" in output
    assert "ceiling" not in output


def test_the_unsupervised_item_can_be_acknowledged(pending: Workspace) -> None:
    # "I am driving this one by hand" is a real answer, and without it a run
    # somebody deliberately unarmed reports itself every time they look
    run("timer", "arm", "--interval", "30m", "--scheduler", "cron")
    run("timer", "disarm")
    assert "no timer is armed" in run("status")[1]

    code, output = run("ack", "unsupervised", "--reason", "driving this by hand")

    assert code == 0, output
    assert "no timer is armed" not in run("status")[1]
