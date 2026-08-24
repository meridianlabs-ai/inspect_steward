import click

from .. import __version__
from .init import init_command
from .runbook import runbook_command
from .status import status_command
from .tasks import tasks_command
from .tend import tend_command


@click.group()
@click.version_option(version=__version__, prog_name="steward")
def steward() -> None:
    """Steward CLI - supervise evaluations."""


steward.add_command(init_command)
steward.add_command(runbook_command)
steward.add_command(status_command)
steward.add_command(tasks_command)
steward.add_command(tend_command)


def main() -> None:
    steward()


if __name__ == "__main__":
    main()
