"""`steward schedule`, as a shell meets it.

The agent's own recurring collect, armed through the same backend machinery as
`steward timer` but as a separate entry: its own label, its own journal events,
and a command that runs `<agent> exec` rather than `steward tend`. What is only
true here is that the two schedules are independent — arming one leaves the other
alone — and that the installed command is the agent invocation it claims to be.

Cron is the backend throughout, driven by the in-memory crontab (`_fake`), and
`codex` is faked onto PATH so nothing here needs it installed.
"""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._workspace import (
    Workspace,
    create_workspace,
    read_agent_armed,
    read_armed,
    read_journal,
)

from .._logs import SynthTask, write_log
from ..schedule.test_tend import prepared
from ._fake import FakeCrontab, fake_cron

TASK = SynthTask("probe", samples=4)
FAKE_CODEX = "/usr/local/bin/codex"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    create_workspace(tmp_path, git=False)
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    monkeypatch.chdir(workspace.root)
    return workspace


@pytest.fixture(autouse=True)
def crontab(monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    return fake_cron(monkeypatch)


@pytest.fixture(autouse=True)
def codex_on_path(monkeypatch: pytest.MonkeyPatch, crontab: FakeCrontab) -> None:
    """`codex` resolvable, so `_command` finds an absolute path without it installed.

    `shutil` is one shared module, so this delegates to whatever `which` the
    `crontab` fixture already installed (it depends on it for the order) rather
    than replacing it — otherwise cron would stop being found and every arm here
    would read as *cron cannot run here*.
    """
    real = shutil.which

    def which(cmd: str, *args: object, **kwargs: object) -> str | None:
        return FAKE_CODEX if cmd == "codex" else real(cmd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("inspect_steward._cli.schedule.shutil.which", which)


def run(*argv: str) -> tuple[int, str]:
    result = CliRunner().invoke(steward, list(argv))
    return result.exit_code, result.output


def scheduled(workspace: Workspace) -> object:
    return read_agent_armed(read_journal(workspace.journal).events)


# --- arming -------------------------------------------------------------


def test_arming_records_and_installs_the_agent_command(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    code, output = run(
        "schedule",
        "arm",
        "--agent",
        "codex",
        "--tend-interval",
        "30m",
        "--scheduler",
        "cron",
    )

    assert code == 0, output
    assert "codex collects every 30m" in output
    recorded = read_agent_armed(read_journal(workspace.journal).events)
    assert recorded is not None
    assert (recorded.scheduler, recorded.interval) == ("cron", 1800)
    # the installed command is the agent invocation, not a tend
    assert crontab.text is not None
    assert FAKE_CODEX in crontab.text
    assert "exec" in crontab.text and "workspace-write" in crontab.text
    assert "steward collect" in crontab.text
    assert "*/30 * * * *" in crontab.text


def test_the_schedule_entry_is_labelled_apart_from_the_tend_timer(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    # both armed at once: two blocks, neither disarming the other, and the
    # collect one carries the -collect suffix so a disarm hits only its own
    run("timer", "arm", "--tend-interval", "30m", "--scheduler", "cron")
    run(
        "schedule",
        "arm",
        "--agent",
        "codex",
        "--tend-interval",
        "30m",
        "--scheduler",
        "cron",
    )

    assert read_armed(read_journal(workspace.journal).events) is not None
    assert read_agent_armed(read_journal(workspace.journal).events) is not None
    assert crontab.text is not None
    assert crontab.text.count("# >>>") == 2
    assert crontab.text.count("-collect >>>") == 1


def test_arming_the_collect_leaves_the_tend_timer_alone(workspace: Workspace) -> None:
    run("schedule", "arm", "--agent", "codex", "--scheduler", "cron")

    # the tend timer's own fold sees nothing: the two are independent state
    assert read_armed(read_journal(workspace.journal).events) is None
    assert read_agent_armed(read_journal(workspace.journal).events) is not None


def test_re_arming_the_collect_leaves_one(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    run(
        "schedule",
        "arm",
        "--agent",
        "codex",
        "--tend-interval",
        "30m",
        "--scheduler",
        "cron",
    )
    run(
        "schedule",
        "arm",
        "--agent",
        "codex",
        "--tend-interval",
        "1h",
        "--scheduler",
        "cron",
    )

    recorded = read_agent_armed(read_journal(workspace.journal).events)
    assert recorded is not None and recorded.interval == 3600
    assert crontab.text is not None
    assert crontab.text.count("-collect >>>") == 1
    assert "*/30" not in crontab.text


def test_a_missing_agent_binary_is_refused_rather_than_scheduled(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a schedule that could never run its harness is worse than none: refuse at
    # arm time instead of failing silently every interval
    def missing(cmd: str, *args: object, **kwargs: object) -> str | None:
        return None

    monkeypatch.setattr("inspect_steward._cli.schedule.shutil.which", missing)

    code, output = run("schedule", "arm", "--agent", "codex", "--scheduler", "cron")

    assert code == 1
    assert "not on PATH" in output
    assert "Traceback" not in output
    assert scheduled(workspace) is None


def test_the_interval_comes_from_steward_md_when_no_flag_says_otherwise(
    workspace: Workspace,
) -> None:
    workspace.directives.write_text("tend_interval: 1h\n", encoding="utf-8")

    code, output = run("schedule", "arm", "--agent", "codex", "--scheduler", "cron")

    assert code == 0, output
    recorded = read_agent_armed(read_journal(workspace.journal).events)
    assert recorded is not None and recorded.interval == 3600


# --- disarming and status -----------------------------------------------


def test_disarming_removes_only_the_collect(
    workspace: Workspace, crontab: FakeCrontab
) -> None:
    run("timer", "arm", "--tend-interval", "30m", "--scheduler", "cron")
    run(
        "schedule",
        "arm",
        "--agent",
        "codex",
        "--tend-interval",
        "30m",
        "--scheduler",
        "cron",
    )

    code, output = run("schedule", "disarm")

    assert code == 0, output
    assert "disarmed cron" in output
    assert read_agent_armed(read_journal(workspace.journal).events) is None
    # the tend timer is untouched
    assert read_armed(read_journal(workspace.journal).events) is not None
    assert crontab.text is not None
    assert crontab.text.count("# >>>") == 1
    assert "-collect" not in crontab.text


def test_disarming_nothing_says_so_rather_than_failing(workspace: Workspace) -> None:
    code, output = run("schedule", "disarm")

    assert code == 0
    assert "no collect was scheduled" in output


def test_status_on_an_unscheduled_workspace_names_the_next_command(
    workspace: Workspace,
) -> None:
    code, output = run("schedule", "status")

    assert code == 0
    assert "no collect is scheduled" in output
    assert "steward schedule arm" in output


def test_status_as_json_is_a_document(workspace: Workspace) -> None:
    run(
        "schedule",
        "arm",
        "--agent",
        "codex",
        "--tend-interval",
        "30m",
        "--scheduler",
        "cron",
    )

    code, output = run("schedule", "status", "--json")

    assert code == 0
    payload = json.loads(output)
    assert payload["scheduler"] == "cron"
    assert payload["interval"] == 1800
    assert payload["present"] is True
