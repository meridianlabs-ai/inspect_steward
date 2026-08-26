"""The progress table, as a terminal renders it.

One line per task, columns right-aligned on their own widths so the numbers stack. The shape follows the one this replaced:

```
⚙ sec_bench_pro[default]@openai/gpt-5   37/183  20%  83r  63q  52/80c  115/300t  0.65
```

Read left to right it is: what state the task is in, which task, how much of it is done, how much is moving right now, how hard the model pool is working, how close the leading sample is to the limit that will stop it, and what it is scoring. Every column is omitted when it has nothing to say — a finished task has no running samples, a task with no declared limit has no budget column — so a settled campaign renders as a quiet list rather than a field of zeroes.

**Widths are computed per render rather than fixed.** Display keys vary from `addition` to a sweep entry with three arguments and a model, and a column padded for the worst case wastes the terminal on every other line.
"""

from .._evalset.observe import TaskState
from .progress import Progress, TaskProgress

GLYPH = {
    TaskState.COMPLETE: "✓",
    TaskState.INCOMPLETE: "⚙",
    TaskState.MISSING: "·",
    TaskState.ORPHANED: "⌫",
}
"""One character per state. `·` for a task not yet started reads as *nothing here yet* rather than as a problem, which is what it is."""


def progress_table(progress: Progress, *, width: int = 0) -> list[str]:
    """Render the rows.

    Args:
        progress: The rows to render.
        width: Truncate display keys to this many characters, or 0 for whatever the widest needs.

    Returns:
        One string per row, plus a totals line when there is more than one row.
    """
    if not progress.rows:
        return []

    cells = [_cells(row, width) for row in progress.rows]
    # a column empty in every row is dropped rather than padded: a settled
    # campaign has no running samples, no queue, and no budget in flight, and
    # holding their width open leaves the score stranded across a gap
    keep = [n for n in range(len(cells[0])) if any(cell[n] for cell in cells)]
    cells = [tuple(cell[n] for n in keep) for cell in cells]
    widths = [max(len(cell[n]) for cell in cells) for n in range(len(cells[0]))]

    lines = [_line(cell, widths) for cell in cells]
    if len(progress.rows) > 1:
        lines.append(_total(progress))
    return lines


def _cells(row: TaskProgress, width: int) -> tuple[str, ...]:
    """One row's columns, already formatted, before they are padded to a width."""
    budget = row.budget
    name = row.key[:width] if width else row.key
    return (
        f"{glyph(row)} {name}",
        f"{row.completed}/{row.total}",
        f"{round(row.fraction * 100)}%",
        f"{row.running}r" if row.running else "",
        f"{row.queued}q" if row.queued else "",
        _connections(row),
        f"{budget.used}/{budget.limit}{budget.suffix}" if budget else "",
        f"{row.headline:.2f}" if row.headline is not None else "",
    )


def _connections(row: TaskProgress) -> str:
    if row.connections is None:
        return ""
    in_use, limit = row.connections
    return f"{in_use}/{limit}c" if limit is not None else f"{in_use}c"


def _line(cells: tuple[str, ...], widths: list[int]) -> str:
    name, *rest = cells
    padded = "  ".join(
        cell.rjust(width) for cell, width in zip(rest, widths[1:], strict=True)
    )
    return f"{name.ljust(widths[0])}  {padded}".rstrip()


def _total(progress: Progress) -> str:
    parts = [f"{progress.completed}/{progress.total} samples"]
    if progress.total:
        parts.append(f"{round(progress.fraction * 100)}%")
    if progress.running:
        parts.append(f"{progress.running} running")
    if progress.queued:
        parts.append(f"{progress.queued} queued")
    if progress.errored:
        parts.append(f"{progress.errored} errored")
    return "  " + " · ".join(parts)


def glyph(row: TaskProgress) -> str:
    """The state character a row leads with."""
    return GLYPH.get(row.state, "?")
