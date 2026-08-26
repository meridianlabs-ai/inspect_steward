"""`status.md` — the snapshot a turn leaves behind for whoever arrives next.

The one artifact that reaches a human who is not in a session. On a machine with no git and sometimes no internet, an object store is the only observability channel there is, and this is the file somebody reads from another system to find out whether the night is going well (workflow.md, *Syncing the workspace out*).

**It states its own age, and that is load-bearing rather than a courtesy.** A remote reader detects a stopped timer, a crashed tend, or a broken sync in exactly one way: by noticing this file is old. A timestamp buried among the numbers gets skimmed past, so it goes at the top, on its own, before anything that could be mistaken for current.

**Deliberately minimal.** What a turn *says* — the attention list a human reads, the work list an agent drains, the verdict glyph over both — is its own design problem and its own step. What is here is the snapshot that makes the loop legible while that is built: where the tasks are, what is running, and what nothing mechanical is going to fix.
"""

from typing import TYPE_CHECKING

from .._evalset.observe import TaskState
from .._util.jsonl import utc_now

if TYPE_CHECKING:
    # the turn imports this module to write its file, so the type it passes can
    # only be named here at type-check time
    from .turn import TendResult

_HEADER = "<!-- Written by `steward tend`. Regenerated every turn; edits are lost. -->"


def status_markdown(result: "TendResult") -> str:
    """Render a turn as `status.md`.

    Args:
        result: The turn that just ran.

    Returns:
        The complete file body.
    """
    summary = result.summary
    lines = [
        "# status",
        "",
        _HEADER,
        "",
        f"**As of** `{utc_now()}`",
        "",
        "## tasks",
        "",
        "| state | tasks |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {state.value} | {summary.states.get(state.value, 0)} |"
        for state in TaskState
    )

    counts = [
        f"{summary.tasks} tasks",
        f"{summary.running} running",
        f"{summary.queued} queued",
        f"ceiling {summary.max_workers}",
    ]
    if result.executed:
        counts.insert(2, f"{len(result.spawned)} spawned this turn")
    else:
        counts.insert(2, f"{summary.spawning} would be spawned")
    lines.extend(["", " · ".join(counts), ""])
    lines.extend(_progress(result))
    lines.extend(["## attention", ""])

    attention = _attention(result)
    lines.extend(attention if attention else ["Nothing needs attention."])
    lines.append("")
    return "\n".join(lines)


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
    for row in rows:
        cells = [
            f"`{row.key}`",
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

    if scored:
        named = {row.headline_name for row in rows if row.headline_name is not None}
        # the convention has to be legible, because nothing in a log marks a
        # metric as primary and a reader who cannot see which one was picked
        # cannot tell a convention from a guess (roadmap.md §5, item 14)
        lines += ["", f"Score is {' / '.join(sorted(named))}."]

    return lines + [""]


def _attention(result: "TendResult") -> list[str]:
    """Everything a reader should not have to infer from the counts above.

    Ordered by who has to act: what has stopped, then what someone must apply, then what the machinery could not do. A run with nothing here is a run nobody needs to look at, which is the whole promise.
    """
    summary = result.summary
    items: list[str] = []

    if summary.stalled:
        items.append(
            f"- **{len(summary.stalled)} "
            f"{'task has' if len(summary.stalled) == 1 else 'tasks have'} stopped "
            f"making progress** and will not be respawned again — they need a look"
        )
    if summary.orphans_running:
        items.append(
            f"- {len(summary.orphans_running)} running "
            f"{'worker is' if len(summary.orphans_running) == 1 else 'workers are'} "
            f"running work the definition no longer asks for"
        )
    if result.drift:
        items.append(
            "- **the definition has changed** since it was captured — run "
            "`steward launch` to apply it"
        )
    if result.degraded is not None:
        items.append(
            f"- `_steward.md` could not be read, so this turn ran on the settings "
            f"the last one recorded ({result.degraded})"
        )
    if summary.unreadable:
        items.append(
            f"- {summary.unreadable} "
            f"{'file' if summary.unreadable == 1 else 'files'} in the log directory "
            f"could not be read as logs"
        )
    if result.failures:
        items.append(f"- {len(result.failures)} actions could not be carried out:")
        items.extend(f"  - {failure}" for failure in result.failures)
    if summary.paused:
        items.append("- the run is **paused**; nothing new is being scheduled")

    return items
