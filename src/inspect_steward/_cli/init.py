import click


@click.command("init")
def init_command() -> None:
    """Initialise a steward workspace in the current directory."""
    click.echo("Initialised steward workspace.")
