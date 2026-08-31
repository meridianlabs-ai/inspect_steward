"""The progress table, as a terminal renders it.

One line per task, columns right-aligned on their own widths so the numbers stack. The shape follows the one this replaced:

```
⚙ sec_bench_pro[default]@openai/gpt-5   37/183  20%  83r  63q  52/80c  115/300 t  0.65
```

Read left to right it is: what state the task is in, which task, how much of it is done, how much is moving right now, how much is still to come, how hard the model pool is working, how far into its budget a typical sample is, and what it is scoring. Every column is omitted when it has nothing to say — a finished task has no running samples and nothing left to queue, a task with no declared limit has no budget column — so a settled campaign renders as a quiet list rather than a field of zeroes.

**Widths are computed per render rather than fixed.** Display keys vary from `addition` to a sweep entry with three arguments and a model, and a column padded for the worst case wastes the terminal on every other line.
"""

from .._evalset.observe import TaskState
from .progress import Progress, TaskProgress, short_keys

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

    short = short_keys(progress.rows)
    cells = [
        _cells(row, key, width)
        for row, key in zip(progress.rows, short.keys, strict=True)
    ]
    # a column empty in every row is dropped rather than padded: a settled
    # campaign has no running samples, no queue, and no budget in flight, and
    # holding their width open leaves the score stranded across a gap
    keep = [n for n in range(len(cells[0])) if any(cell[n] for cell in cells)]
    cells = [tuple(cell[n] for n in keep) for cell in cells]
    widths = [max(len(cell[n]) for cell in cells) for n in range(len(cells[0]))]

    lines = [_line(cell, widths) for cell in cells]
    if (footer := _footer(progress, short.model)) is not None:
        lines.append(footer)
    return lines


def _cells(row: TaskProgress, key: str, width: int) -> tuple[str, ...]:
    """One row's columns, already formatted, before they are padded to a width."""
    name = key[:width] if width and len(key) > width else key
    return (
        f"{glyph(row)} {name}",
        f"{row.completed}/{row.total}",
        f"{round(row.fraction * 100)}%",
        f"{row.running}r" if row.running else "",
        f"{row.queued}q" if row.queued else "",
        _connections(row),
        _outcome(row),
    )


def _outcome(row: TaskProgress) -> str:
    """The last column: how far a running task is into its budget, or what a finished one scored.

    **One column, because no row ever has both.** A budget is usage against a limit and usage comes from a worker, so it exists exactly while a task is running; a headline metric is computed at scoring time, so it exists exactly once one has finished. Two columns for two states of the same row cost every line the width of whichever it is not in — which on the narrow table is the difference between a task name and a truncated one.

    Read down the column it is *where each task has got to*, which is the same question either way.
    """
    if row.budget is not None:
        return row.budget.text
    return f"{row.headline:.2f}" if row.headline is not None else ""


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


def _footer(progress: Progress, model: str | None) -> str | None:
    """The line under the table: what every row shares, then the run's totals.

    **The two halves are gated separately**, because only one of them is a total. Summing one row's samples restates the row, so the totals wait for a second row — but a model elided out of the keys has to be said *somewhere*, and a single-task run that shows neither the model in its key nor a footer to name it has simply lost it.
    """
    parts: list[str] = []
    if model is not None:
        # every row ran against it and no row shows it, so it is a fact about
        # the run rather than a column
        parts.append(model)
    if len(progress.rows) < 2:
        return f"  {parts[0]}" if parts else None

    parts.append(f"{progress.completed}/{progress.total} samples")
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
