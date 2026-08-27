"""`status.md` — the snapshot a turn leaves behind for whoever arrives next.

The one artifact that reaches a human who is not in a session. On a machine with no git and sometimes no internet, an object store is the only observability channel there is, and this is the file somebody reads from another system to find out whether the night is going well (workflow.md, *Syncing the workspace out*).

**It states its own age, and that is load-bearing rather than a courtesy.** A remote reader detects a stopped timer, a crashed tend, or a broken sync in exactly one way: by noticing this file is old. A timestamp buried among the numbers gets skimmed past, so it goes at the top, on its own, before anything that could be mistaken for current.

**The verdict leads, and everything under it is elaboration.** A reader who takes one line from this file should have taken the true one. Below that the file answers three questions in the order they are asked: where the tasks are, how the samples are going, and what somebody has to do about it.

**Both this and the terminal render the same items.** They used to render two hand-written lists of the same conditions, and those lists had already drifted apart in what they reported — which is the argument for the item type rather than an anecdote about it.
"""

from typing import TYPE_CHECKING

from .._evalset.observe import TaskState
from .._schedule import Summary
from .._util.jsonl import utc_now
from .items import HEADINGS, by_owner, verdict_line
from .progress import short_keys

if TYPE_CHECKING:
    # the turn imports this module to write its file, so the type it passes can
    # only be named here at type-check time
    from .turn import TendResult

_HEADER = "<!-- Written by `steward tend`. Regenerated every turn; edits are lost. -->"


def status_markdown(result: "TendResult", *, header: bool = True) -> str:
    """Render a turn as markdown.

    Args:
        result: The turn that just ran.
        header: Include the generated-file comment. `status.md` wants it; `steward status --format md` does not, since nothing there is a file anybody could edit by mistake.

    Returns:
        The complete body.
    """
    summary = result.summary
    lines = ["# status", ""]
    if header:
        lines.extend([_HEADER, ""])
    lines.extend(
        [
            f"{verdict_line(result.verdict, result.items)}",
            "",
            f"**As of** `{utc_now()}`{_qualified(result)}",
            "",
            "## tasks",
            "",
            "| state | tasks |",
            "| --- | ---: |",
        ]
    )
    lines.extend(
        f"| {state.value} | {summary.states.get(state.value, 0)} |"
        for state in TaskState
    )

    counts = [
        f"{summary.tasks} tasks",
        f"{summary.running} running{_shape(summary)}",
        f"{summary.queued} queued",
    ]
    if result.executed:
        counts.insert(2, f"{len(result.spawned)} spawned this turn")
    else:
        counts.insert(2, f"{summary.spawning} would be spawned")
    lines.extend(["", " · ".join(counts), ""])
    lines.extend(_progress(result))
    lines.extend(_items(result))
    return "\n".join(lines)


def _shape(summary: Summary) -> str:
    """How the running tasks are divided, and against what the operator allowed.

    Silent where nothing is set and nothing is packed, which is the default run — the shape is then *everything, one process each* and saying so on every line would be noise. A limit or a packed process is a fact about why the number is what it is, and appears.
    """
    parts: list[str] = []
    if summary.max_tasks is not None:
        parts.append(f"of {summary.max_tasks}")
    if summary.workers != summary.running or summary.max_workers is not None:
        allowed = "" if summary.max_workers is None else f"/{summary.max_workers}"
        parts.append(f"in {summary.workers}{allowed} workers")
    return f" ({', '.join(parts)})" if parts else ""


def _qualified(result: "TendResult") -> str:
    """What qualifies this snapshot's freshness, beside the time it was taken.

    A claim is not an item — nobody resolves it, and it will be gone within seconds — but it is exactly the thing that makes an *as of* misleading, since a tend running right now is about to change every number below. `status` is required to report one rather than take it (execution.md, *`status` and `tend` are one function, two dispositions*), and the terminal has always said so; this is the markdown saying it too.
    """
    if (claim := result.claim) is not None:
        since = f" since `{claim.since}`" if claim.since else ""
        return f" · a {claim.command or 'command'} holds the claim{since}"
    if (broke := result.broke) is not None:
        return f" · cleared a wedged claim held by pid {broke.pid}"
    return ""


def _progress(result: "TendResult") -> list[str]:
    """The per-task table, as markdown.

    Samples rather than task states, because *how is the run going* is a
    question about samples and the table above it is not an answer to it. Every
    column is present or absent for the whole table rather than per row, so a
    settled campaign renders four columns instead of eight.
    """
    rows = result.progress.rows
    if not rows:
        return []

    short = short_keys(rows)
    live = any(row.live for row in rows)
    scored = any(row.headline is not None for row in rows)
    budgeted = any(row.budget is not None for row in rows)

    header = ["task", "samples", "done"]
    align = ["---", "---:", "---:"]
    if live:
        header += ["running", "queued", "connections"]
        align += ["---:", "---:", "---:"]
    if budgeted:
        header += ["limit"]
        align += ["---:"]
    if scored:
        header += ["score"]
        align += ["---:"]

    lines = [
        "## progress",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(align) + " |",
    ]
    for row, key in zip(rows, short.keys, strict=True):
        cells = [
            f"`{key}`",
            f"{row.completed}/{row.total}",
            f"{round(row.fraction * 100)}%",
        ]
        if live:
            connections = ""
            if row.connections is not None:
                in_use, limit = row.connections
                connections = f"{in_use}/{limit}" if limit is not None else str(in_use)
            cells += [
                str(row.running) if row.running else "",
                str(row.queued) if row.queued else "",
                connections,
            ]
        if budgeted:
            budget = row.budget
            cells += [
                f"{budget.used}/{budget.limit} {budget.name}" if budget else "",
            ]
        if scored:
            cells += [f"{row.headline:.3g}" if row.headline is not None else ""]
        lines.append("| " + " | ".join(cells) + " |")

    notes: list[str] = []
    if short.model is not None:
        notes.append(f"All tasks ran against `{short.model}`.")
    if scored:
        named = {row.headline_name for row in rows if row.headline_name is not None}
        # the convention has to be legible, because nothing in a log marks a
        # metric as primary and a reader who cannot see which one was picked
        # cannot tell a convention from a guess (roadmap.md §5, item 14)
        notes.append(f"Score is {' / '.join(sorted(named))}.")
    if notes:
        lines += ["", " ".join(notes)]

    return lines + [""]


def _items(result: "TendResult") -> list[str]:
    """Everything a reader should not have to infer from the counts above.

    Grouped by who has to act rather than by kind, because the first question a reader has is whether any of this is theirs. Each line carries its id, which is how it is disposed of once somebody has decided it is fine — `steward ack` takes any unambiguous prefix of one.
    """
    lines = ["## attention", ""]
    if not result.items:
        return lines + ["Nothing needs attention.", ""]

    for owner, group in by_owner(result.items):
        lines.extend([f"### {HEADINGS[owner]}", ""])
        for item in group:
            trailer = f"`{item.id}`" if item.acknowledgeable else "_transient_"
            if item.action is not None:
                trailer = f"`{item.action}` · {trailer}"
            lines.append(f"- {item.summary} — {trailer}")
        lines.append("")
    return lines
