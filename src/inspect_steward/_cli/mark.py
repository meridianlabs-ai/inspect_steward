"""`steward _mark` — the marking runner's entry point.

Hidden, because nobody types it: the tend's executor spawns it with a run id (`_marks.spawn`), and everything it needs is in the runs record and the journal. It exists as a command rather than a function so that the work happens in a process of its own — recomputing a log's metrics can import the task's module, and a definition that calls `eval_set()` at module level would otherwise run the eval inside the tend (`_marks.run`).
"""

import sys

import click

from .._marks.run import run_mark
from .turn import find_workspace


@click.command("_mark", hidden=True)
@click.option(
    "--run",
    "run",
    required=True,
    help="The run to carry out, as `.steward/marks/runs.jsonl` names it.",
)
def mark_command(run: str) -> None:
    """Carry out one marking run. Internal: spawned by the tend."""
    sys.exit(run_mark(find_workspace(), run))
