"""The progress table, as a terminal renders it.

One line per task, columns right-aligned on their own widths so the numbers stack. The shape follows the one this replaced:

```
⚙ sec_bench_pro[default]@openai/gpt-5   37/183  20%  83r  63q  2e  52/80c  115/300t
```

Read left to right it is: what state the task is in, which task, how much of it is done, how much is moving right now, how much is still to come, how much has errored, how hard the model pool is working, how much of what landed the scanners have reached, how far into its budget a typical sample is, and what it scored — final for a finished task, interim over the samples scored so far for a running one. Every column is omitted when it has nothing to say — a finished task has no running samples and nothing left to queue, an errored count of zero is the ordinary case, a fully scanned run has no gap, a task with no declared limit has no budget column — so a settled campaign renders as a quiet list rather than a field of zeroes.

**Widths are computed per render rather than fixed.** Display keys vary from `addition` to a sweep entry with three arguments and a model, and a column padded for the worst case wastes the terminal on every other line.
"""

from .._evalset.observe import TaskState
from .._util.size import format_bytes
from .progress import Progress, TaskProgress, fleet_totals, short_keys

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
        row.budget.text if row.budget is not None else "",
        score_cell(row, digits=2),
    )


def clip(key: str, width: int) -> str:
    """A display key cut to `width` characters with its middle elided, or whole where `width` is 0 or it already fits.

    The one rule every table with a task column applies, so a phone-width post, a terminal and the operator's page never disagree about how a name is shortened.

    **The middle goes, not the end.** A sweep's keys share their head and differ at the tail — `cais_swebenchpro@anthropic/claude-haiku-4-5-20251001` beside `cais_swebenchpro@openai/gpt-5.6-luna` — so a key cut at the end left two rows telling apart by nothing, and at phone width lost the model altogether. `cais_swebenchpr…iku-4-5-20251001` still hints both halves: which benchmark, and which model ran it.
    """
    if not width or len(key) <= width:
        return key
    head = (width - 1) // 2
    tail = width - 1 - head
    return f"{key[:head]}…{key[len(key) - tail :]}"


def score_cell(row: TaskProgress, *, digits: int) -> str:
    """The score column's cell: the headline to `digits`, final or interim alike.

    **Two columns now, budget and score, because a running row has both.** The budget is usage against a limit and exists exactly while a task runs; the score used to exist exactly once it had finished, which is what let the two share a column. A running task's interim figure ended that: it is a score with a budget beside it. `progress_table` still drops a column empty in every row, so a settled campaign pays for the score alone and a run whose tasks declare no limit pays for nothing it did not use.

    An interim figure is not marked. A reader of a row that says `10/30` beside a score knows the score is over the ten, and a marker would be telling them what the row already says.
    """
    if row.headline is None:
        return ""
    return f"{row.headline:.{digits}f}" if digits > 0 else f"{row.headline:.3g}"


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
    totals = fleet_totals(progress)
    if totals is None:
        # a single-task run: the totals would restate its one row
        return f"  {parts[0]}" if parts else None
    parts.append(totals)
    return "  " + " · ".join(parts)


def glyph(row: TaskProgress) -> str:
    """The state character a row leads with."""
    return GLYPH.get(row.state, "?")


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
    """A padded plain table: the first column left-aligned, every other right-aligned under its heading, two spaces between columns.

    The one table layout, for a terminal, a post's fenced block, and a markdown document's — so no two surfaces disagree about a cell, only about what is drawn around it. Padded because these documents are read in an editor at least as often as they are rendered, and a column of numbers that lines up is a table before anything renders it.
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


def pipe_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """A GitHub pipe table with its cells padded to their columns: the first column left-aligned, every other right-aligned, and the delimiter row carrying that alignment.

    The same padding `plain_table` gives a fenced block, for the one table a reader meets rendered as often as raw. A pipe table wraps its cells when a renderer draws it and stacks its numbers when an editor shows the source, so a status.md read as plain text lines up its columns without a renderer to line them up.
    """
    widths = [
        max(3, max(len(row[n]) for row in (header, *rows))) for n in range(len(header))
    ]
    return [
        _pipe_row(header, widths),
        _pipe_delimiter(widths),
        *(_pipe_row(row, widths) for row in rows),
    ]


def _pipe_delimiter(widths: list[int]) -> str:
    cells = (
        "-" * widths[0],
        *("-" * (width - 1) + ":" for width in widths[1:]),
    )
    return "| " + " | ".join(cells) + " |"


def _pipe_row(cells: tuple[str, ...], widths: list[int]) -> str:
    name, *rest = cells
    padded = [
        name.ljust(widths[0]),
        *(cell.rjust(width) for cell, width in zip(rest, widths[1:], strict=True)),
    ]
    return "| " + " | ".join(padded) + " |"
