"""The progress table, as a terminal renders it.

One line per task, columns right-aligned on their own widths so the numbers stack. The shape follows the one this replaced:

```
⚙ sec_bench_pro[default]@openai/gpt-5   37/183  20%  83r  63q  2e  52/80c  115/300t
```

Read left to right it is: what state the task is in, which task, how much of it is done, how much is moving right now, how much is still to come, how much has errored, how hard the model pool is working, how much of what landed the scanners have reached, how far into its budget a typical sample is, and what it scored — final for a finished task, interim over the samples scored so far for a running one. Every column is omitted when it has nothing to say — a finished task has no running samples and nothing left to queue, an errored count of zero is the ordinary case, a fully scanned run has no gap, a task with no declared limit has no budget column — so a settled campaign renders as a quiet list rather than a field of zeroes.

The model-pool figure has since moved out of these tables into the `conn` column of the `**resources**` table (`resources_cells`), where it stands beside the memory and CPU a reader scanning what a worker costs is already looking for; the task cell here is now the display key alone.

**Widths are computed per render rather than fixed.** Display keys vary from `addition` to a sweep entry with three arguments and a model, and a column padded for the worst case wastes the terminal on every other line.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._evalset.display import ShortKeys
from .._evalset.observe import TaskState
from .._util.size import format_bytes
from .coverage import TaskCoverage
from .progress import (
    Progress,
    RuntimeProgress,
    RuntimeState,
    TaskProgress,
    fleet_totals,
    short_keys,
)

if TYPE_CHECKING:
    # `task_table_cells` reads a whole turn; the type it takes can only be named
    # here at type-check time, since `turn` imports this module to render itself
    from .turn import TendResult

GLYPH = {
    TaskState.COMPLETE: "✓",
    TaskState.INCOMPLETE: "⚙",
    TaskState.MISSING: "·",
    TaskState.ORPHANED: "⌫",
}
"""One character per state. `·` for a task not yet started reads as *nothing here yet* rather than as a problem, which is what it is."""

RUNTIME_GLYPH = {
    RuntimeState.RUNNING: "⚙",
    RuntimeState.SUCCEEDED: "✓",
    RuntimeState.ERRORED: "✗",
    RuntimeState.DEAD: "✗",
    RuntimeState.ORPHANED: "⌫",
}
"""One character per state of a task created at runtime, sharing `GLYPH`'s where the meaning is the same, so a column of glyphs reads alike down both kinds of row. A dead task gets the errored one: neither will produce its result, and Steward acts on neither."""


@dataclass(frozen=True)
class TableRow:
    """One row of a task table, whichever kind of task it is: the cells every task table draws, decided once (`table_rows`).

    One shape for a manifest task's row and a runtime task's, so no renderer branches on which it has: a cell that only a steered task has — the budget — is simply empty in a runtime task's row.
    """

    key: str
    """What the turn's folds file the task under: a manifest task's identifier, or a runtime task's task id."""

    glyph: str
    name: str
    """The task cell: a manifest task's display key, or a runtime task's name, clipped to the table's width."""

    completed: int
    total: int
    running: int
    queued: int
    errored: int
    live: bool

    budget: str
    """Usage against the per-sample limit, abbreviated — `45/100t` — or empty."""

    score: str
    scanned: TaskCoverage | None

    created_by: str
    """The display key of the task that created a runtime task, clipped like `name`; empty for a manifest task's row."""

    @property
    def fraction(self) -> float:
        return self.completed / self.total if self.total else 0.0


def table_rows(progress: Progress, short: ShortKeys, width: int) -> list[TableRow]:
    """Every row of a task table: the manifest's tasks under their display keys, then the tasks created at runtime under their names.

    A runtime task's creator is named by the key its own row is shown under, so the two can be matched by eye.

    Args:
        progress: The rows to draw.
        short: The display keys of `progress.rows`, as `short_keys` shortened them.
        width: Clip names to this many characters, or 0 for whole.

    Returns:
        One row per task, manifest rows first.
    """
    named = {
        row.identifier: key for row, key in zip(progress.rows, short.keys, strict=True)
    }
    named.update({row.task_id: row.name for row in progress.runtime})
    return [
        *(_task_row(row, clip(named[row.identifier], width)) for row in progress.rows),
        *(
            _runtime_row(
                row,
                clip(row.name, width),
                clip(named[row.created_by], width) if row.created_by else "",
            )
            for row in progress.runtime
        ),
    ]


def _task_row(row: TaskProgress, name: str) -> TableRow:
    return TableRow(
        key=row.identifier,
        glyph=GLYPH.get(row.state, "?"),
        name=name,
        completed=row.completed,
        total=row.total,
        running=row.running,
        queued=row.queued,
        errored=row.errored,
        live=row.live,
        budget=row.budget.text if row.budget is not None else "",
        score=score_cell(row.headline),
        scanned=row.scanned,
        created_by="",
    )


def _runtime_row(row: RuntimeProgress, name: str, created_by: str) -> TableRow:
    return TableRow(
        key=row.task_id,
        glyph=RUNTIME_GLYPH[row.state],
        name=name,
        completed=row.completed,
        total=row.total,
        running=row.running,
        queued=row.queued,
        errored=row.errored,
        live=row.live,
        budget="",
        score=score_cell(row.headline),
        scanned=row.scanned,
        created_by=created_by,
    )


TEXT_COLUMNS = frozenset({"", "task", "created by"})
"""The headings of a task table's columns that hold text and are left-aligned; every other column holds a figure and is right-aligned. By heading rather than by position, so a column added anywhere keeps every other column's alignment."""


def text_columns(header: tuple[str, ...]) -> frozenset[int]:
    """The indexes of `header`'s text columns (`TEXT_COLUMNS`), for `plain_table` and `pipe_table`."""
    return frozenset(n for n, heading in enumerate(header) if heading in TEXT_COLUMNS)


def progress_table(progress: Progress, *, width: int = 0) -> list[str]:
    """Render the rows.

    A runtime task's row ends with who created it — `by generator` — after every other field, so a manifest row, which has no creator, renders as it would in a run with no runtime tasks.

    Args:
        progress: The rows to render.
        width: Truncate display keys to this many characters, or 0 for whatever the widest needs.

    Returns:
        One string per row, plus a totals line when there is more than one row.
    """
    if not progress.rows and not progress.runtime:
        return []

    short = short_keys(progress.rows)
    rows = table_rows(progress, short, width)
    cells = [_cells(row) for row in rows]
    # a column empty in every row is dropped rather than padded: a settled
    # campaign has no running samples, no queue, and no budget in flight, and
    # holding their width open leaves the score stranded across a gap
    keep = [n for n in range(len(cells[0])) if any(cell[n] for cell in cells)]
    cells = [tuple(cell[n] for n in keep) for cell in cells]
    widths = [max(len(cell[n]) for cell in cells) for n in range(len(cells[0]))]

    lines = [
        _line(cell, widths, row.created_by)
        for cell, row in zip(cells, rows, strict=True)
    ]
    if (footer := _footer(progress, short.model)) is not None:
        lines.append(footer)
    return lines


def _cells(row: TableRow) -> tuple[str, ...]:
    """One row's columns, already formatted, before they are padded to a width.

    The task cell is the state glyph and the name alone: connections stand in the `conn` column of the `**resources**` table (`resources_cells`), beside the memory and CPU a reader scanning resource figures is already looking for, rather than riding in the name cell of the progress table.
    """
    return (
        f"{row.glyph} {row.name}",
        f"{row.completed}/{row.total}",
        f"{round(row.fraction * 100)}%",
        f"{row.running}r" if row.running else "",
        f"{row.queued}q" if row.queued else "",
        row.budget,
        row.score,
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


def score_cell(headline: float | None) -> str:
    """The score column's cell: the headline to two decimals, final or interim alike.

    **Two columns now, budget and score, because a running row has both.** The budget is usage against a limit and exists exactly while a task runs; the score used to exist exactly once it had finished, which is what let the two share a column. A running task's interim figure ended that: it is a score with a budget beside it. `progress_table` still drops a column empty in every row, so a settled campaign pays for the score alone and a run whose tasks declare no limit pays for nothing it did not use.

    **Always two decimals, so a whole-number score reads as a score.** A bare `1` beside `20%` and `48/200` is a count until a reader stops to place it, where `1.00` is a fraction on sight — and a column that renders `1` for one task and `0.83` for the next is not aligned on the decimal point it is meant to be read down. The same fixed shape in every surface, so the terminal, the post and the operator's page never disagree about a cell.

    An interim figure is not marked. A reader of a row that says `10/30` beside a score knows the score is over the ten, and a marker would be telling them what the row already says.
    """
    if headline is None:
        return ""
    return f"{headline:.2f}"


def task_table_cells(
    result: "TendResult", *, width: int
) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """The operator task table's header and plain rows: where each task stands, and nothing a reader would have to ask about.

    **One builder for two surfaces**, so the operator's `status.md` and a notification cannot disagree about what a turn found — only about how the cells are drawn. `status.md` wraps the task column in backticks and lays the rows out as a pipe table; a post fences them as a plain table. Both start here.

    The state glyph leads every row in a column of its own, with an empty heading: the glyph says what it is. A `created by` column comes last where the run has tasks created at runtime, and is absent otherwise.

    Samples rather than task states, because *how is the run going* is a question about samples. No errored or scanned column: what errored is in the by-task table, and coverage is the agent's to read aloud at signoff. No connections either: they stand in the `**resources**` table's `conn` column (`resources_cells`), beside the memory and CPU a reader scanning resource figures is already looking for. Every column is present or absent for the whole table rather than per row.

    Args:
        result: The turn that just ran.
        width: Clip display keys to this many characters, or 0 for whole. The operator's page passes `KEY_WIDTH`; a post passes the narrower phone width.

    Returns:
        The header and one plain row per task — the task column, at index 1, unadorned for the caller to decorate in its own dialect — or `((), [])` where the run has no tasks. `text_columns` says which columns to left-align.
    """
    progress = result.progress
    if not progress.rows and not progress.runtime:
        return (), []
    rows = table_rows(progress, short_keys(progress.rows), width)
    live = any(row.live for row in rows)
    queued = any(row.queued for row in rows)
    budgeted = any(row.budget for row in rows)
    scored = any(row.score for row in rows)
    created = any(row.created_by for row in rows)

    header = ["", "task", "samples", "done"]
    if live:
        header += ["running"]
    if queued:
        header += ["queued"]
    if budgeted:
        header += ["limit"]
    if scored:
        header += ["score"]
    if created:
        header += ["created by"]

    body: list[tuple[str, ...]] = []
    for row in rows:
        cells = [
            row.glyph,
            row.name,
            f"{row.completed}/{row.total}",
            f"{round(row.fraction * 100)}%",
        ]
        if live:
            cells += [str(row.running) if row.running else ""]
        if queued:
            cells += [str(row.queued) if row.queued else ""]
        if budgeted:
            cells += [row.budget]
        if scored:
            cells += [row.score]
        if created:
            cells += [row.created_by]
        body.append(tuple(cells))
    return tuple(header), body


def _line(cells: tuple[str, ...], widths: list[int], created_by: str) -> str:
    name, *rest = cells
    padded = "  ".join(
        cell.rjust(width) for cell, width in zip(rest, widths[1:], strict=True)
    )
    # after the padding rather than a column of its own, so it starts at one
    # place down every runtime row and is left-aligned like the name it is
    by = f"  by {created_by}" if created_by else ""
    return f"{name.ljust(widths[0])}  {padded}{by}".rstrip()


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


RESOURCES_HEADER = ("task", "conn", "refusals", "retries", "memory", "cpu")
"""The `**resources**` table's columns, in reading order.

`conn` leads the figures: the model connections a task holds against its pool ceiling, `46/260`, which used to ride in the task cell of the tables above and now stands here beside the other things a running worker costs."""


def resources_cells(progress: Progress, *, width: int = 0) -> list[tuple[str, ...]]:
    """The `**resources**` rows: each running task's connections, refusals, HTTP retries, memory and CPU, in the task table's order.

    Per task rather than a fleet total, which is what lets the figures stand without a caveat: a finished task has no row, so nothing here falls to zero as the run completes. Connections are the model pool in use against its ceiling; memory and CPU are the task's even share of its process (`TaskResources`).

    Args:
        progress: The turn's rows, for the display keys, the connections, and the render order.
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
    connections = {row.identifier: row.connections for row in progress.rows}
    order = {row.identifier: n for n, row in enumerate(progress.rows)}
    rows = sorted(live.resources, key=lambda one: order.get(one.identifier, len(order)))
    return [
        (
            clip(named.get(one.identifier, one.identifier), width),
            _conn_cell(connections.get(one.identifier)),
            str(one.refusals),
            str(one.http_retries),
            format_bytes(one.rss),
            f"{one.cores:.1f}",
        )
        for one in rows
    ]


def _conn_cell(connections: tuple[int, int | None] | None) -> str:
    """The `conn` column: model connections in use against the pool ceiling — `46/260`, `46` where the pool is unbounded, empty where the task reported none."""
    if connections is None:
        return ""
    in_use, limit = connections
    return f"{in_use}/{limit}" if limit is not None else str(in_use)


def resources_table(progress: Progress, *, width: int = 0) -> list[str]:
    """`resources_cells` as a plain table inside a code fence, or nothing while no worker is answering.

    Fenced rather than a markdown table on purpose: the figures are the page's quietest, and a second ruled table beside the anomalies gives them the same weight. A fence renders lighter, and lands in Slack as a preformatted block with its columns still lined up.
    """
    cells = resources_cells(progress, width=width)
    return ["```", *plain_table(RESOURCES_HEADER, cells), "```"] if cells else []


def plain_table(
    header: tuple[str, ...],
    rows: list[tuple[str, ...]],
    *,
    indent: str = "",
    left: Collection[int] = (0,),
) -> list[str]:
    """A padded plain table: the columns at the indexes in `left` left-aligned, every other right-aligned under its heading, two spaces between columns.

    The one table layout, for a terminal, a post's fenced block, and a markdown document's — so no two surfaces disagree about a cell, only about what is drawn around it. Padded because these documents are read in an editor at least as often as they are rendered, and a column of numbers that lines up is a table before anything renders it.
    """
    widths = [max(len(row[n]) for row in (header, *rows)) for n in range(len(header))]
    return [
        (indent + "  ".join(_padded(row, widths, left))).rstrip()
        for row in (header, *rows)
    ]


def pipe_table(
    header: tuple[str, ...],
    rows: list[tuple[str, ...]],
    *,
    left: Collection[int] = (0,),
) -> list[str]:
    """A GitHub pipe table with its cells padded to their columns: the columns at the indexes in `left` left-aligned, every other right-aligned, and the delimiter row carrying that alignment.

    The same padding `plain_table` gives a fenced block, for the one table a reader meets rendered as often as raw. A pipe table wraps its cells when a renderer draws it and stacks its numbers when an editor shows the source, so a status.md read as plain text lines up its columns without a renderer to line them up.
    """
    # a left-aligned column may be as narrow as one hyphen, which is what lets
    # a glyph column stay one character wide; a right-aligned one keeps room
    # for its colon and two hyphens besides
    widths = [
        max(1 if n in left else 3, max(len(row[n]) for row in (header, *rows)))
        for n in range(len(header))
    ]
    delimiter = (
        "-" * width if n in left else "-" * (width - 1) + ":"
        for n, width in enumerate(widths)
    )
    return [
        _pipe_row(_padded(header, widths, left)),
        _pipe_row(list(delimiter)),
        *(_pipe_row(_padded(row, widths, left)) for row in rows),
    ]


def _padded(
    cells: tuple[str, ...], widths: list[int], left: Collection[int]
) -> list[str]:
    return [
        cell.ljust(width) if n in left else cell.rjust(width)
        for n, (cell, width) in enumerate(zip(cells, widths, strict=True))
    ]


def _pipe_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"
