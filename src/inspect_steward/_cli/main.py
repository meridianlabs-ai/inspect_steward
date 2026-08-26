import click
from inspect_ai._util.dotenv import init_dotenv

from .. import __version__
from .ack import ack_command
from .init import init_command
from .pause import pause_command, resume_command
from .runbook import runbook_command
from .status import status_command
from .tasks import tasks_command
from .tend import tend_command
from .timer import timer_command


@click.group()
@click.version_option(version=__version__, prog_name="steward")
def steward() -> None:
    """Steward CLI - supervise evaluations."""
    # a scheduled tend runs under a stripped environment, and its workers pick
    # up `.env` for free -- inspect searches up from their cwd, which is the
    # workspace -- while this process does not. An S3 `log_dir` under cron needs
    # credentials to list, so the tend has to see what its workers see
    init_dotenv()


steward.add_command(ack_command)
steward.add_command(init_command)
steward.add_command(pause_command)
steward.add_command(resume_command)
steward.add_command(runbook_command)
steward.add_command(status_command)
steward.add_command(tasks_command)
steward.add_command(tend_command)
steward.add_command(timer_command)


def main() -> None:
    steward()


if __name__ == "__main__":
    main()
