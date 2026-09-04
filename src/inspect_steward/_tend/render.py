"""`status.md` and `steward collect` — one turn, two readers.

**Two projections of one fold, not two renderers.** `status_markdown` is what an operator reads: the file the timer regenerates, and what `steward status --format md` prints for an agent to relay. `collect_markdown` is what the agent reads for itself. They are cut from the same `TendResult` and share every sentence they both carry — the verdict, the task table's cells, the by-task anomalies table — so they can differ in what they show and never in what they say.

**The operator's page is short on purpose.** On a machine with no git and sometimes no internet, an object store is the only observability channel there is, and this is the file somebody reads from another system to find out whether the night is going well (workflow.md, *Syncing the workspace out*). It answers four questions in order: what state is the run in and how old is this, where does each task stand, what is waiting on me, and what did not take the normal course. Windows, proposals, precedent, standing rules and the history are the agent's to work, and an operator who wants them asks the agent. The item lines carry the summary alone — an id is the argument to a verb the operator is not running.

**Its age is load-bearing rather than a courtesy.** A remote reader detects a stopped timer, a crashed tend, or a broken sync in exactly one way: by noticing this file is old. So the two ages sit on the first line beside the verdict — *tended* is the timer, *collected* is the agent — and either going stale is its own signal (agent.md §2.2).

**The agent's page is complete rather than short.** Every open item with the id its verb takes, the run's states, the table with its errored and scanned columns, every open window with its exemplar and precedent, the rules in force, and the history since the cursor. What it sets aside — decisions already raised, history already collected — it counts, because an agent can read a label but cannot reason about what it was never shown.
"""

from typing import TYPE_CHECKING

from .._anomaly.model import Anomalies, Anomaly, AnomalyState
from .._evalset.cost import fleet_width, projection
from .._evalset.observe import TaskState
from .._schedule import Summary
from .._util.duration import format_duration
from .._util.jsonl import utc_now
from .anomalies_md import caveat_line, outcomes_table
from .coverage import TaskCoverage
from .items import (
    HEADINGS,
    Owner,
    by_owner,
    precedent_line,
    verdict_line,
    waiting_to_land,
)
from .progress import LIVE_ONLY, TaskProgress, compact, short_keys
from .table import resources_table

if TYPE_CHECKING:
    # the turn imports this module to write its file, so the type it passes can
    # only be named here at type-check time
    from .turn import TendResult

_HEADER = "<!-- Written by `steward tend`. Regenerated every turn; edits are lost. -->"


def status_markdown(result: "TendResult", *, header: bool = True) -> str:
    """Render a turn for an operator: `status.md`, and `steward status --format md`.

    Args:
        result: The turn that just ran.
        header: Include the generated-file comment. `status.md` wants it; `steward status --format md` does not, since nothing there is a file anybody could edit by mistake.

    Returns:
        The complete body.
    """
    lines: list[str] = []
    if header:
        lines += [_HEADER, ""]
    lines += [status_headline(result), ""]
    lines += _signature(result)
    lines += _tasks(result)
    lines += _operator(result)
    lines += _outcomes(result)
    lines += _resources(result)
    if result.log_dir is not None:
        # in full rather than shortened, because the audience for this line is
        # somebody about to paste it into `samples_df` or `inspect view` -- and
        # because it is frequently not under the workspace at all, which is the
        # case that made it worth a line (`TendResult.log_dir`). Last, because
        # it is the one line that never changes
        lines += [f"**Logs** `{result.log_dir}`", ""]
    return "\n".join(lines)


def collect_markdown(result: "TendResult", *, since: int = 0) -> str:
    """Render a turn for the agent: `steward collect`.

    Args:
        result: The turn that just ran.
        since: Show only history after this journal position, counting what that leaves out.

    Returns:
        The complete body.
    """
    summary = result.summary
    lines = [
        verdict_line(result.verdict, result.items),
        "",
        f"**As of** `{utc_now()}`{_ages(result)}{_qualified(result)}",
        "",
    ]
    if result.log_dir is not None:
        lines += [f"**Logs** `{result.log_dir}`", ""]
    lines += _notification(result)
    lines += _items(result)
    lines += ["## the run", "", "| state | tasks |", "| --- | ---: |"]
    lines += [
        f"| {state.value} | {summary.states.get(state.value, 0)} |"
        for state in TaskState
    ]
    counts = [
        f"{summary.tasks} tasks",
        f"{summary.running} running{_shape(summary)}",
        f"{summary.queued} queued",
    ]
    if result.executed:
        counts.insert(2, f"{len(result.spawned)} spawned this turn")
    else:
        counts.insert(2, f"{summary.spawning} would be spawned")
    lines += ["", " · ".join(counts), ""]
    lines += _progress(result)
    lines += _anomalies(result)
    lines += _live(result)
    lines += _tuning(result)
    lines += _policies(result)
    lines += _happened(result, since=since)
    return "\n".join(lines)


def status_headline(result: "TendResult") -> str:
    """The operator's first line: the verdict as it concerns them, then the two ages.

    `⚠️ 1 needs an operator · tended 4m ago · agent: 4 open items, collected 12m ago`. The verdict counts the operator's own items, and the agent's queue is a count on the right — what an operator wants to know about the agent is that it is working and how long since it looked, not what it is working on. Shared by the terminal, so the two cannot open differently.
    """
    theirs = [item for item in result.items if item.owner is Owner.OPERATOR]
    parts = [verdict_line(result.verdict, theirs), *_tended(result), _agent(result)]
    return " · ".join(parts) + _qualified(result)


def _tended(result: "TendResult") -> list[str]:
    """`tended 4m ago`, or nothing on a workspace no tend has recorded an age for yet."""
    supervision = result.supervision
    if result.executed:
        # **the turn writing this file is the tend**, so the recorded age is the
        # *previous* one — this document would otherwise be stamped `as of now`
        # and `tended 10m ago` on the same line, one of them wrong, and on a
        # first turn would omit the age entirely for a run being tended as the
        # reader looks. A `status` renders the recorded age, which is correct
        # there for exactly the same reason
        return ["tended just now"]
    if supervision is not None and supervision.since_tend is not None:
        return [f"tended {format_duration(int(supervision.since_tend))} ago"]
    return []


def _agent(result: "TendResult") -> str:
    """`agent: 4 open items, collected 12m ago` — the agent's queue as a count, and how long since it read anything."""
    open_ = sum(1 for item in result.items if item.owner is Owner.AGENT)
    items = (
        "no open items"
        if not open_
        else f"{open_} open item{'' if open_ == 1 else 's'}"
    )
    if result.collected is None:
        # never, rather than long ago -- a workspace no agent has attached to is
        # not one whose agent has gone quiet, and the two want different answers
        looked = "never collected"
    elif result.since_collected is not None:
        looked = f"collected {format_duration(int(result.since_collected))} ago"
    else:
        looked = "collected"
    return f"agent: {items}, {looked}"


def _signature(result: "TendResult") -> list[str]:
    """Who accepted the results and what they said, while a signature stands.

    On the page rather than only in the journal, because the run never tends again after this turn: the snapshot a remote reader has is the last one written, and *signed off* without a name is a state, not a record.
    """
    signature = result.signature
    if signature is None or not result.signed:
        return []
    note = f" — {signature.note}" if signature.note else ""
    return [f"**Signed off** by {signature.by} at `{signature.ts}`{note}", ""]


def _tasks(result: "TendResult") -> list[str]:
    """The operator's task table: where each task stands, and nothing it would have to ask about.

    Samples rather than task states, because *how is the run going* is a question about samples. No errored or scanned column: what errored is in the by-task table below, and coverage is the agent's to read aloud at signoff. Connections ride in the task cell, `(8/16)`, because they exist only while the task runs and a column for them is empty for most of a sweep. Every column is present or absent for the whole table rather than per row.
    """
    rows = result.progress.rows
    if not rows:
        return []
    short = short_keys(rows)
    live = any(row.live for row in rows)
    queued = any(row.queued for row in rows)
    budgeted = any(row.budget is not None for row in rows)
    scored = any(row.headline is not None for row in rows)

    header = ["task", "samples", "done"]
    if live:
        header += ["running"]
    if queued:
        header += ["queued"]
    if budgeted:
        header += ["limit"]
    if scored:
        header += ["score"]
    lines = [
        "| " + " | ".join(header) + " |",
        "| --- | " + " | ".join("---:" for _ in header[1:]) + " |",
    ]
    for row, key in zip(rows, short.keys, strict=True):
        cells = [
            f"`{_named(row, key)}`",
            f"{row.completed}/{row.total}",
            f"{round(row.fraction * 100)}%",
        ]
        if live:
            cells += [str(row.running) if row.running else ""]
        if queued:
            cells += [str(row.queued) if row.queued else ""]
        if budgeted:
            cells += [_budget_cell(row)]
        if scored:
            cells += [f"{row.headline:.3g}" if row.headline is not None else ""]
        lines.append("| " + " | ".join(cells) + " |")
    if short.model is not None:
        lines += ["", f"Every task runs `{short.model}`."]
    return lines + [""]


def _named(row: TaskProgress, key: str) -> str:
    """The task cell: the display key, and its connections in use while it runs — `swe_bench@gpt-5 (8/16)`."""
    if row.connections is None:
        return key
    in_use, limit = row.connections
    return f"{key} ({in_use}/{limit})" if limit is not None else f"{key} ({in_use})"


def _budget_cell(row: TaskProgress) -> str:
    budget = row.budget
    if budget is None:
        return ""
    return f"{compact(budget.used)}/{compact(budget.limit)} {budget.name}"


def _operator(result: "TendResult") -> list[str]:
    """What is waiting on the operator, one summary per line, most costly first.

    The summary alone: no id, no verb. The agent puts each of these to them as a question and runs the verb with their answer; a `steward ack` argument on an operator's page is a line they learn to skip. Absent rather than empty when nothing is waiting — the headline already says so.
    """
    for owner, group in by_owner(result.items):
        if owner is Owner.OPERATOR:
            return ["### operator", "", *(f"- {item.summary}" for item in group), ""]
    return []


def _outcomes(result: "TendResult") -> list[str]:
    """By task, the samples that did not take the normal course — the table `anomalies.md` opens on, verbatim. Absent where every sample took it."""
    table = outcomes_table(result.dispositions.outcomes, result.progress)
    return ["### anomalies", "", *table, ""] if table else []


def _resources(result: "TendResult") -> list[str]:
    """Per running task, what it has met and what it is costing. Absent while no worker is answering."""
    table = resources_table(result.progress)
    return ["### resources", "", *table, ""] if table else []


def _live(result: "TendResult") -> list[str]:
    """Under the table: what the running processes cost, or what starting them would.

    **One or the other, never both and never neither.** While something runs, the measured figures are the answer; before anything does, the capture's startup ceiling is — a bound is the useful number when there is no actual, and the actual is the useful number once there is (agent.md §4.2).
    """
    if (live := result.progress.live) is not None:
        return [f"**Running now** · {live.figures}", "", f"_{LIVE_ONLY}_", ""]
    summary = result.summary
    bound = projection(
        summary.capture_rss,
        fleet_width(
            summary.tasks,
            max_workers=summary.max_workers,
            max_tasks=summary.max_tasks,
        ),
    )
    # silent where nothing measured it, which is every manifest committed before
    # the measurement existed -- and a reader should see nothing rather than a zero
    return [bound[0].upper() + bound[1:], ""] if bound is not None else []


def _tuning(result: "TendResult") -> list[str]:
    """The tuning loop's account of the window, when a ramp is configured.

    One source for these lines (`TuningPlan.lines`), for the reason `Live.figures` is: the terminal and this document must not disagree about what a window supported. On a `status` the moves shown are a preview — the next tend takes them if the window holds — which is the same tense every other number in a status already speaks.
    """
    lines = result.tuning.lines
    if not lines:
        return []
    out = [f"**Tuning** · {lines[0]}", ""]
    if len(lines) > 1:
        out.extend(f"- {line}" for line in lines[1:])
        out.append("")
    return out


def _notification(result: "TendResult") -> list[str]:
    """Whether anything reaches an operator, beside where the logs are.

    In the header block for the reason `**Logs**` is: it is a fact about the deployment that a reader cannot get anywhere else, and the alternative to printing it is every reader guessing. The guess this replaces was always the same one — `notification` is commented out in `_steward.yaml`, therefore nobody can be reached — and it is wrong on any machine whose `.env` sets the variable, which is the arrangement the template itself recommends.

    **No remedy here.** `launch` gives that once, where somebody is watching; a document rewritten every ten minutes that ends in advice is how a reader learns to stop reading the bottom of it.

    Silent only on a result assembled by hand, which settled no channel and should claim nothing about one.
    """
    if (channel := result.notification) is None:
        return []
    return [f"**Notifications** {channel.description}", ""]


def _policies(result: "TendResult") -> list[str]:
    """This human's standing rules, as they are actually in force.

    Reported because the file is no longer the only place they can come from: `STEWARD_POLICIES` carries them too, so an agent told to *read `_steward.yaml`* would be reading half of them on a machine that sets one. Steward never interprets these — they appear here exactly as written, and what to do about them is the reader's judgement.

    Silent where there are none, which is the default workspace. An empty section under a heading reads as *there are rules and you cannot see them*, which is worse than no heading at all.
    """
    if not result.policies:
        return []
    return ["## standing rules", "", *(f"- {rule}" for rule in result.policies), ""]


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
    """The agent's task table, as markdown.

    The operator's columns (`_tasks`) plus the two the agent works from: errored, split by ruling, and scanned. Every column is present or absent for the whole table rather than per row, so a settled campaign renders four columns instead of eight.
    """
    rows = result.progress.rows
    if not rows:
        return []

    short = short_keys(rows)
    live = any(row.live for row in rows)
    # not gated on `live` with the other two: what is still to run is known
    # whether or not a worker is answering, and most of a sweep has none
    queued = any(row.queued for row in rows)
    # nor is this: an errored count is the log's to report, it is the number
    # the anomaly queue exists for (step 23), and it is per task -- the totals
    # line already carries the sum and cannot say which tasks it came from
    errored = any(row.errored for row in rows)
    scored = any(row.headline is not None for row in rows)
    budgeted = any(row.budget is not None for row in rows)
    # a run that scans nothing has no column; a run that scanned everything has
    # one saying so, unlike the terminal's, because this document is what a
    # reader quotes and *48 of 48 scanned* is the sentence they need to quote
    scanned = any(row.scanned is not None for row in rows)

    header = ["task", "samples", "done"]
    align = ["---", "---:", "---:"]
    if live:
        header += ["running"]
        align += ["---:"]
    if queued:
        header += ["queued"]
        align += ["---:"]
    if errored:
        header += ["errored"]
        align += ["---:"]
    if scanned:
        header += ["scanned"]
        align += ["---:"]
    if budgeted:
        header += ["limit"]
        align += ["---:"]
    if scored:
        header += ["score"]
        align += ["---:"]

    # a sub-heading rather than a section of its own, because the document has
    # exactly three sections: a fourth `##` would read as a fourth thing to
    # attend to
    lines = [
        "### tasks",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(align) + " |",
    ]
    for row, key in zip(rows, short.keys, strict=True):
        cells = [
            f"`{_named(row, key)}`",
            f"{row.completed}/{row.total}",
            f"{round(row.fraction * 100)}%",
        ]
        if live:
            cells += [str(row.running) if row.running else ""]
        if queued:
            cells += [str(row.queued) if row.queued else ""]
        if errored:
            cells += [
                _errored_cell(
                    row.errored, result.dispositions.by_task.get(row.identifier)
                )
            ]
        if scanned:
            cells += [_scanned_cell(row.scanned)]
        if budgeted:
            cells += [_budget_cell(row)]
        if scored:
            cells += [f"{row.headline:.3g}" if row.headline is not None else ""]
        lines.append("| " + " | ".join(cells) + " |")

    notes: list[str] = []
    if short.model is not None:
        notes.append(f"All tasks ran against `{short.model}`.")
    if scored:
        named = {row.headline_name for row in rows if row.headline_name is not None}
        # which metric it is has to be legible whether the task declared one or
        # fell back to the first of the first score: a bare number in a column
        # is not self-describing, and two tasks can land on different metrics
        notes.append(f"Score is {' / '.join(sorted(named))}.")
    marks = marks_note(result)
    if marks:
        notes.append(marks)
    gap = coverage_note(result)
    if gap:
        notes.append(gap)
    if notes:
        lines += ["", " ".join(notes)]

    return lines + [""]


def _scanned_cell(scanned: TaskCoverage | None) -> str:
    """The scanned cell — `48/50`, or `?/50` where the numerator could not be established.

    A question mark rather than a zero, for the reason `TaskCoverage.known` exists: *nothing is known to be scanned* and *nothing was scanned* are two different problems, and a reader who quotes this table would carry the wrong one into their report.
    """
    if scanned is None:
        return ""
    if not scanned.known:
        return f"?/{scanned.landed}"
    return f"{scanned.scanned}/{scanned.landed}"


_BUCKET_ORDER = ("rerunning", "excluded", "zeroed", "scored", "accepted", "undecided")


def _errored_cell(count: int, split: dict[str, int] | None) -> str:
    """The errored cell, split by ruling where one is in force.

    `3 (2 excluded, 1 undecided)` — the count is still the log's number; the parenthetical is what has been decided about it. A wholly undecided task keeps the bare count.
    """
    if not count:
        return ""
    if not split or set(split) == {"undecided"}:
        return str(count)
    parts = [f"{split[name]} {name}" for name in _BUCKET_ORDER if split.get(name)]
    return f"{count} ({', '.join(parts)})"


def marks_note(result: "TendResult") -> str | None:
    """The scoring qualification when exclusions or zeroes are in force.

    Shared with `anomalies.md`, which opens on the same sentence: a reader quoting the numbers and a reader glancing at them must be given the same denominator, and two computations of it are two chances to differ.

    The qualification beside the number, never a recomputed number: headline scores still come off the log verbatim, and this line says what population they describe.
    """
    excluded = result.dispositions.excluded
    zeroed = result.dispositions.zeroed
    if not excluded and not zeroed:
        return None
    total = sum(row.total for row in result.progress.rows)
    parts = [
        f"{n} {name}" for n, name in ((excluded, "excluded"), (zeroed, "zeroed")) if n
    ]
    return (
        f"Scores are over {total - excluded} of {total} samples ({', '.join(parts)})."
    )


def coverage_note(result: "TendResult") -> str | None:
    """What the scanners have not reached, or `None` while they have reached everything.

    **Only when there is a gap**, because the column beside it already says the number and a sentence restating a complete coverage is a line a reader learns to skip. What the sentence adds is the reading: *these numbers are over the samples, and the scan findings are over fewer of them*.

    **A task whose coverage could not be checked gets its own sentence** rather than joining the gap. It is not a measured shortfall — it is a measurement that did not happen, and it is left out of the totals precisely so the gap stays a number somebody counted. A reader told *48 of 50* about a run where a third task was never checked has been given a true sentence and a false impression, so the second sentence says how many tasks the first one is not about.
    """
    coverage = result.coverage
    gap = coverage.gap
    unverified = len(coverage.unverified)
    if not gap and not unverified:
        return None
    notes: list[str] = []
    if gap:
        notes.append(
            f"Scan findings are over {coverage.scanned} of "
            f"{coverage.landed} samples ({gap} not yet scanned)."
        )
    if unverified:
        plural = "" if unverified == 1 else "s"
        notes.append(
            f"Coverage could not be checked for {unverified} task{plural} "
            f"(shown as `?`) — the current log would not read, and the counted "
            f"totals exclude {'it' if unverified == 1 else 'them'}."
        )
    return " ".join(notes)


def anomalies_line(anomalies: Anomalies) -> str | None:
    """One phrase for where the anomaly queue stands, or `None` while it is empty.

    Shared by the terminal and this document for the reason `verdict_line` is: two wordings of the same count are two chances to disagree.
    """
    if not anomalies.open:
        return None
    parts: list[str] = []
    for state, phrase in (
        (AnomalyState.INVESTIGATING, "investigating"),
        (AnomalyState.PROPOSED, "proposed"),
        (AnomalyState.RULED, "awaiting a re-run"),
    ):
        count = sum(1 for anomaly in anomalies.open if anomaly.state is state)
        if count:
            parts.append(f"{count} {phrase}")
    detail = f" ({', '.join(parts)})" if parts else ""
    total = len(anomalies.open)
    return f"anomalies: {total} open{detail}"


def _anomalies(result: "TendResult") -> list[str]:
    """The open windows, the live proposals, and the marks the record carries.

    A sub-heading beside `### tasks` rather than a section, for the same reason the table is: the document has three sections and an agent relays it whole. Silent while nothing is open and nothing is marked — a run with no anomalies should not carry an empty heading saying so.

    The marks stay after their windows settle, deliberately: an accepting ruling's effect is the report-facing account of what happened to the data ("2 samples excluded from scoring"), and a document that dropped it the moment the decision landed would show the decision only while it was still undecided.
    """
    anomalies = result.anomalies
    line = anomalies_line(anomalies)
    # the same caveats `anomalies.md` spells out, decided once by the turn -- so
    # a disposal that left a mark on the results opens this heading exactly as
    # an accepted window does, and one the other document drops cannot survive
    # as a line here
    marks = result.caveats
    # the same by-task table `anomalies.md` opens on and the operator's page
    # carries, so the agent reads the numbers it will be asked about
    table = outcomes_table(result.dispositions.outcomes, result.progress)
    if line is None and not marks and not table:
        return []
    lines = ["### anomalies", ""]
    if line is not None:
        lines += [line, ""]
    landed = _landed(result)
    for anomaly in anomalies.open:
        waiting = waiting_to_land(anomaly, landed)
        lines.append(f"- `{anomaly.class_key}` — {_window_line(anomaly, waiting)}")
        if anomaly.evidence.exemplar:
            lines.append(f"  - `{anomaly.evidence.exemplar}`")
        for ruling in anomaly.precedent:
            # verbatim, attached where the anomaly surfaces rather than looked
            # up (workflow.md §12.8)
            lines.append(f"  - precedent: {precedent_line(ruling, anomaly.class_key)}")
    for identifier, proposal in anomalies.proposals.items():
        covered = len(proposal.classes)
        lines.append(
            f"- {identifier} proposes {proposal.action.value} for {covered} "
            f"{'class' if covered == 1 else 'classes'} — "
            f"`steward rule --proposal {identifier}`"
        )
    # one line each here against the five fields there — two lengths of one
    # list, so the glance and the quotation cannot disagree about which windows
    # left a mark or what their effect sentence says
    for caveat in marks:
        lines.append(f"- {caveat_line(caveat)}")
    if table:
        lines += ["", *table]
    return lines + [""]


def _landed(result: "TendResult") -> frozenset[str]:
    """The tasks whose current log is complete — what a scan window waits on."""
    if result.observed is None:
        return frozenset()
    return frozenset(
        task.identifier for task in result.observed.by_state(TaskState.COMPLETE)
    )


def _window_line(anomaly: Anomaly, waiting: bool = False) -> str:
    count = anomaly.evidence.count
    plural = "" if count == 1 else "s"
    if waiting:
        # said in words: the queue is silent about this window on purpose, and
        # an agent reading "open" beside it would start the investigation the
        # runbook tells it to wait on
        return f"waiting for the task to land — {count} instance{plural}"
    if anomaly.state is AnomalyState.RULED and anomaly.ruling is not None:
        ruled = f"ruled {anomaly.ruling.disposition.value} by {anomaly.ruling.by}"
        if anomaly.failed_resolutions:
            line = (
                f"{ruled}; the re-run failed again "
                f"×{anomaly.failed_resolutions} — awaiting a fresh ruling"
            )
        else:
            line = f"{ruled}, awaiting the re-run"
    elif anomaly.state is AnomalyState.INVESTIGATING:
        note = f": {anomaly.note}" if anomaly.note else ""
        line = f"investigating{note} — {count} instance{plural}"
    elif anomaly.state is AnomalyState.PROPOSED:
        line = f"proposed under {anomaly.proposal} — {count} instance{plural}"
    else:
        line = f"open, {count} instance{plural}"
    if anomaly.generation > 1:
        line += f" (generation {anomaly.generation})"
    if anomaly.substrate:
        line += " — looks like the machinery under the run; verify storage before re-running"
    return line


def _raised(count: int) -> str:
    """How an omission is named. One phrase, so the two renderings cannot word it differently."""
    return (
        f"{count} raised, awaiting an operator"
        if count > 1
        else "1 raised, awaiting an operator"
    )


def _items(result: "TendResult") -> list[str]:
    """What somebody has to decide, as the agent needs it: grouped by owner, each with the id its verb takes.

    Grouped by who has to act, because the first question a reader has is whether any of this is theirs — and within a group by level, so that among an operator's own decisions the ones costing something now come first (`by_owner`).

    A raised item is set aside and counted rather than shown. It is still open and an operator still owes an answer; what raising records is that the *agent's* part is done, and an agent reading ten entries with seven marked *raised* still spends attention on all ten — which is the cost `raise` exists to remove.
    """
    shown = [item for item in result.items if not item.raised]
    set_aside = len(result.items) - len(shown)

    lines = ["## what needs a decision", ""]
    if not shown:
        note = "Nothing needs attention."
        if set_aside:
            note = f"Nothing for you. {_raised(set_aside)}."
        return lines + [note, ""]

    if set_aside:
        lines.extend([f"_{_raised(set_aside)}, not shown._", ""])

    for owner, group in by_owner(shown):
        lines.extend([f"### {HEADINGS[owner]}", ""])
        for item in group:
            trailer = f"`{item.id}`" if item.addressable else "_transient_"
            if item.action is not None:
                trailer = f"`{item.action}` · {trailer}"
            lines.append(f"- {item.summary} — {trailer}")
        lines.append("")
    return lines


def _happened(result: "TendResult", *, since: int = 0) -> list[str]:
    """What has been done to this run, oldest first.

    **Complete rather than a delta**, which is what keeps this document stateless — see `history.py` for the admission test that makes completeness affordable. `since` is for the agent's projection only; an operator's copy shows everything.

    **An omission is counted, never silent.** A reader who is shown a shortened list with nothing saying so concludes the list is the whole of it, which for a history is the difference between *the night was quiet* and *you were not shown the night*.
    """
    entries = result.happened.since(since)
    omitted = result.happened.before(since)
    lines = ["## what happened", ""]
    if not entries:
        note = "Nothing has been done to this run yet."
        if omitted:
            note = f"Nothing new. {omitted} earlier — `--since 0` for all."
        return lines + [note, ""]

    if omitted:
        lines.extend([f"_{omitted} earlier, not shown — `--since 0` for all._", ""])
    lines.extend(f"- `{entry.ts}` {entry.text}" for entry in entries)
    lines.append("")
    return lines


def _ages(result: "TendResult") -> str:
    """How old this information is, and how long since anyone read it.

    **Two ages rather than one**, because they fail differently and either alone is ambiguous: a stale tend age means the timer stopped, a stale collection age means nobody is exercising judgement (agent.md §2.2). A workspace tended four minutes ago and collected six hours ago is describing its own situation accurately, and neither number says that by itself.
    """
    parts: list[str] = []
    supervision = result.supervision
    if result.executed:
        # **the turn writing this file is the tend**, so the recorded age is the
        # *previous* one — this document would otherwise be stamped `as of now`
        # and `tended 10m ago` on the same line, one of them wrong, and on a
        # first turn would omit the age entirely for a run being tended as the
        # reader looks. A `status` renders the recorded age, which is correct
        # there for exactly the same reason
        parts.append("tended just now")
    elif supervision is not None and supervision.since_tend is not None:
        parts.append(f"tended {format_duration(int(supervision.since_tend))} ago")
    if result.collected is None:
        # never, rather than long ago -- a workspace no agent has attached to is
        # not one whose agent has gone quiet, and the two want different answers
        parts.append("never collected")
    elif result.since_collected is not None:
        parts.append(f"collected {format_duration(int(result.since_collected))} ago")
    return f" · {' · '.join(parts)}" if parts else ""
