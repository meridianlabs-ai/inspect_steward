from typing import Any

import click
import yaml

from .._evalset.detect import DefinitionType
from .._evalset.manifest import Manifest
from .._evalset.read import ReadEvalSetError, read_eval_set


@click.command("tasks")
@click.argument("definition", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--arg",
    "-A",
    "definition_args",
    multiple=True,
    metavar="KEY=VALUE",
    help="Argument for the definition (flow spec function args only). Can be specified multiple times.",
)
@click.option(
    "--type",
    "definition_type",
    type=click.Choice(["evalset", "flow"]),
    default=None,
    help="Definition type (auto-detected by default).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the full manifest as JSON.",
)
def tasks_command(
    definition: str,
    definition_args: tuple[str, ...],
    definition_type: DefinitionType | None,
    output_json: bool,
) -> None:
    """Enumerate the tasks defined by an eval set definition.

    DEFINITION is a Python file culminating in an eval_set() call, or an Inspect Flow spec (Python or YAML).
    """
    try:
        manifest = read_eval_set(
            definition,
            args=_parse_args(definition_args),
            type=definition_type,
        )
    except (ValueError, ReadEvalSetError) as ex:
        raise click.ClickException(str(ex)) from ex

    if output_json:
        click.echo(manifest.model_dump_json(indent=2))
    else:
        _print_tasks(manifest)


def _parse_args(args: tuple[str, ...]) -> dict[str, Any] | None:
    """Parse `-A KEY=VALUE` args with the same semantics as inspect_ai's `parse_cli_args` (YAML scalar coercion, comma-separated strings become lists, dashes in keys become underscores)."""
    if not args:
        return None
    parsed: dict[str, Any] = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep or not key:
            raise click.UsageError(f"--arg must be KEY=VALUE (got '{arg}').")
        loaded = yaml.safe_load(value)
        if isinstance(loaded, str):
            elements = value.split(",")
            loaded = elements if len(elements) > 1 else elements[0]
        parsed[key.replace("-", "_")] = loaded
    return parsed


def _print_tasks(manifest: Manifest) -> None:
    rows = [(task.key, str(task.samples), str(task.epochs)) for task in manifest.tasks]
    widths = [
        max(len(header), *(len(row[i]) for row in rows))
        for i, header in enumerate(("KEY", "SAMPLES", "EPOCHS"))
    ]
    click.echo(
        f"{'KEY':<{widths[0]}}  {'SAMPLES':>{widths[1]}}  {'EPOCHS':>{widths[2]}}"
    )
    for key, samples, epochs in rows:
        click.echo(f"{key:<{widths[0]}}  {samples:>{widths[1]}}  {epochs:>{widths[2]}}")
    total = sum(task.samples * task.epochs for task in manifest.tasks)
    click.echo(f"\n{len(manifest.tasks)} tasks, {total} total samples")
