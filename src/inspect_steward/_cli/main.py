import click

from .. import __version__
from .init import init_command
from .tasks import tasks_command


@click.group()
@click.version_option(version=__version__, prog_name="steward")
def steward() -> None:
    """Steward CLI - supervise evaluations."""


steward.add_command(init_command)
steward.add_command(tasks_command)


def main() -> None:
    steward()


if __name__ == "__main__":
    main()
