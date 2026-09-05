"""The progress table, as a terminal renders it.

One line per task, columns right-aligned on their own widths so the numbers stack. The shape follows the one this replaced:

```
⚙ sec_bench_pro[default]@openai/gpt-5   37/183  20%  83r  63q  2e  52/80c  115/300t
```

Read left to right it is: what state the task is in, which task, how much of it is done, how much is moving right now, how much is still to come, how much has errored, how hard the model pool is working, how much of what landed the scanners have reached, and how far into its budget a typical sample is — or, for a finished task, what it scored. Every column is omitted when it has nothing to say — a finished task has no running samples and nothing left to queue, an errored count of zero is the ordinary case, a fully scanned run has no gap, a task with no declared limit has no budget column — so a settled campaign renders as a quiet list rather than a field of zeroes.

**Widths are computed per render rather than fixed.** Display keys vary from `addition` to a sweep entry with three arguments and a model, and a column padded for the worst case wastes the terminal on every other line.
"""

from .._evalset.observe import TaskState
from .._util.size import format_bytes
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
    """One row's columns, already formatted, before they are padded to a width.

    Connections ride in the name cell as `(8/16)` rather than in a column of their own: they exist only while the task runs, and a reader scanning the numeric columns wants counts there, not a figure that is empty for most of the sweep.
    """
    name = clip(key, width)
    if row.connections is not None:
        in_use, limit = row.connections
        name = (
            f"{name} ({in_use}/{limit})" if limit is not None else f"{name} ({in_use})"
        )
    return (
        f"{glyph(row)} {name}",
        f"{row.completed}/{row.total}",
        f"{round(row.fraction * 100)}%",
        f"{row.running}r" if row.running else "",
        f"{row.queued}q" if row.queued else "",
        _outcome(row),
    )


def clip(key: str, width: int) -> str:
    """A display key cut to `width` characters, or whole where `width` is 0 — the one rule every table with a task column applies, so a phone-width post and a terminal never disagree about how a name is shortened."""
    return key[:width] if width and len(key) > width else key


def _outcome(row: TaskProgress) -> str:
    """The last column: how far a running task is into its budget, or what a finished one scored.

    **One column, because no row ever has both.** A budget is usage against a limit and usage comes from a worker, so it exists exactly while a task is running; a headline metric is computed at scoring time, so it exists exactly once one has finished. Two columns for two states of the same row cost every line the width of whichever it is not in — which on the narrow table is the difference between a task name and a truncated one.

    Read down the column it is *where each task has got to*, which is the same question either way.
    """
    if row.budget is not None:
        return row.budget.text
    return f"{row.headline:.2f}" if row.headline is not None else ""


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


def markdown_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """A markdown table padded in the source: the first column left-aligned, every other right-aligned under its heading.

    Padded because these documents are read in an editor at least as often as they are rendered, and a column of numbers that lines up is a table before anything renders it.
    """
    widths = [max(len(row[n]) for row in (header, *rows)) for n in range(len(header))]
    rule = "|".join(
        ["", "-" * (widths[0] + 2), *("-" * (w + 1) + ":" for w in widths[1:]), ""]
    )
    return [
        _markdown_row(header, widths),
        rule,
        *(_markdown_row(row, widths) for row in rows),
    ]


def _markdown_row(cells: tuple[str, ...], widths: list[int]) -> str:
    name, *rest = cells
    padded = [
        name.ljust(widths[0]),
        *(cell.rjust(width) for cell, width in zip(rest, widths[1:], strict=True)),
    ]
    return f"| {' | '.join(padded)} |"


RESOURCES_HEADER = ("task", "refusals", "retries", "memory", "cpu")
"""The `### resources` table's columns, in reading order."""


def resources_cells(progress: Progress, *, width: int = 0) -> list[tuple[str, ...]]:
    """The `### resources` rows: each running task's refusals, HTTP retries, memory and CPU, in the task table's order.

    Per task rather than a fleet total, which is what lets the figures stand without a caveat: a finished task has no row, so nothing here falls to zero as the run completes. Memory and CPU are the task's even share of its process (`TaskResources`).

    Args:
        progress: The turn's rows, for the display keys and the render order.
        width: Cut display keys to this many characters, or 0 for whole.

    Returns:
        One row per task a worker answered for; nothing while none did.
    """
    live = progress.live
    if live is None or not live.resources:
        return []
    short = short_keys(progress.rows)
    named = {
        row.identifier: key for row, key in zip(progress.rows, short.keys, strict=True)
    }
    order = {row.identifier: n for n, row in enumerate(progress.rows)}
    rows = sorted(live.resources, key=lambda one: order.get(one.identifier, len(order)))
    return [
        (
            clip(named.get(one.identifier, one.identifier), width),
            str(one.refusals),
            str(one.http_retries),
            format_bytes(one.rss),
            f"{one.cores:.1f}",
        )
        for one in rows
    ]


def resources_table(progress: Progress, *, width: int = 0) -> list[str]:
    """`resources_cells` as a plain table inside a code fence, or nothing while no worker is answering.

    Fenced rather than a markdown table on purpose: the figures are the page's quietest, and a second ruled table beside the anomalies gives them the same weight. A fence renders lighter, and lands in Slack as a preformatted block with its columns still lined up.
    """
    cells = resources_cells(progress, width=width)
    return ["```", *plain_table(RESOURCES_HEADER, cells), "```"] if cells else []


def plain_table(
    header: tuple[str, ...], rows: list[tuple[str, ...]], *, indent: str = ""
) -> list[str]:
    """The same columns as `markdown_table`, for a terminal: padded, two spaces between columns, no pipes or rule.

    One layout rule in two spellings, so the terminal and the document cannot disagree about a cell — only about what is drawn around it.
    """
    widths = [max(len(row[n]) for row in (header, *rows)) for n in range(len(header))]
    return [_plain_row(row, widths, indent) for row in (header, *rows)]


def _plain_row(cells: tuple[str, ...], widths: list[int], indent: str) -> str:
    name, *rest = cells
    padded = [
        name.ljust(widths[0]),
        *(cell.rjust(width) for cell, width in zip(rest, widths[1:], strict=True)),
    ]
    return (indent + "  ".join(padded)).rstrip()
