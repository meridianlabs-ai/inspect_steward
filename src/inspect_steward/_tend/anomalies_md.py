"""`anomalies.md` — the caveats that reached the final data, and the one definition of a caveat.

The journal answers *was this run conducted properly*. A different reader asks a different question — *what caveats apply to these numbers* — and that reader is writing up the results, or reading the write-up, and may never open the journal at all (workflow.md §14). They need something short, and they need it to be honest.

**The filter is whether an anomaly left a mark on the final data**, and the state machine already draws that line: a `resolved` window is not in the data (47 rate-limit failures, invalidated, re-ran clean), an `accepted` one is (2 samples re-ran twice, still failed, accepted as errored). So this file is a **fold over the journal** — no new state, no second record, and it cannot disagree with the journal because it is derived from it. A run with four hundred journal events may have three entries here, and that brevity is the point: it is quotable as footnotes.

**One definition of a caveat, two renderings of it.** `status.md` carries the same accepted windows as one-line marks under its anomalies heading, and this document carries them as five-field entries — the same facts at two verbosities, for a reader glancing at a run and a reader quoting it. So `caveats()` decides *what a caveat is* exactly once and both documents render what it returns: they cannot disagree about which windows qualify, whose reason is attached, or what the effect sentence says. The alternative — two renderers reading the fold separately — is how the errored-cell split and the marks note would have drifted apart, and it is the same argument the item type itself rests on.

**An acknowledgment is the other way in, and it has to be.** An `acknowledged` item leaves every surface and nothing asks about it again, which is what stops an attention list going unread — and that is safe only because the disposal lands here when it touched the data. An acknowledgment whose subject left a mark on the results is `accepted`, and reaches this file exactly as a ruling would. One with no such mark — a drift nobody applied, a propagation that stopped — ends in the journal, because there is no caveat to carry. Without that split, *removed from the surface* would quietly mean *removed from the record*, and the difference between those two is the whole of this document.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from .._anomaly.model import SAMPLE_SHAPED, Anomalies, Anomaly, Ruling
from .._evalset.instances import InstanceBatch, in_results
from .._workspace.journal import Ack
from .items import STALLED, UNREADABLE
from .progress import Progress, short_keys
from .table import clip, markdown_table

HEADER = "<!-- Written by `steward tend`. Regenerated every turn; edits are lost. -->"
"""The same banner `status.md` carries, and this document needs it more.

It reads as prose about the results, so the reader most likely to open it is the one writing those results up — and the first instinct on finding a caveat worded awkwardly is to fix the wording. It is regenerable machine state, and the edit would be gone by the next turn with nothing to say it had been made.
"""

MARKED = frozenset({STALLED, UNREADABLE})
"""Item kinds whose acknowledgment left a mark on the results, and therefore becomes a caveat.

An allow-list rather than a deny-list, and one notch stronger than `history._ADMITTED`'s reason: the item vocabulary grows, and the default for a kind this document has never heard of must be *not a caveat* — a caveat list that grows by accident is a caveat list nobody trusts.

Two entries. Acknowledging a **stalled** task says *this will not be run again and the results stand without it*, which is a hole in the data with a name on it. Acknowledging an **unreadable** log says *the numbers are over what could be read*, which moves the denominator. Everything else in the vocabulary is machinery or a decision with no residue: a drift nobody applied, a propagation that stopped, a claim holder that was killed, a tuning proposal, a park, a stuck sample. `anomaly` is absent by construction — an anomaly closes through a ruling and an ack of one is refused, precisely so that no anomaly reaches this file except through a decision with a disposition on it. A scanner that could not scan is absent for the same reason: it is a `scanerror:` window now, and it reaches this file through the ruling that closes it.
"""

OUTCOME_COLUMNS = (
    ("zeroed", "zero"),
    ("excluded", "nan"),
    ("errored", "error"),
    ("scored_early", "early"),
    ("terminated", "term"),
)
"""The by-task table's columns in reading order: the `rulings.OUTCOMES` cell, and its heading. Short on purpose — the same table goes into a phone-width Slack post — and one set everywhere rather than a long form for the file and a short one for the channel. `nan` is what an excluded sample's score becomes. Every column is always shown, so the table has one shape across runs and a reader learns where to look."""

EMPTY = "·"
"""An empty cell. A glyph rather than a blank, so that a column of nothing still reads as a column in a source file, and zero is never confused with unrendered."""

SAMPLES_NAMED = 50
"""Member samples named per entry before the rest become a count.

Higher than the journal's own cap, because this is the record somebody quotes: *which samples exactly* is the question an entry exists to answer, and twenty is a sample rather than an answer. Not unbounded, because a class with four hundred members is a paragraph nobody reads and its count is the honest summary.
"""


@dataclass(frozen=True)
class Caveat:
    """One thing that happened to the data, with everything a footnote needs.

    The five fields are workflow.md §14's, plus the membership a reader needs to go look. `effect` is what makes the list usable rather than merely accurate — a reader needs the denominator, and *2 samples excluded* is the sentence that supplies it.
    """

    subject: str
    """The class key, or the acknowledged item's subject."""

    what: str
    """What happened, in a sentence."""

    scope: str
    """How many, and where."""

    why: str
    """The decision's reasoning, **verbatim** — never reflowed, because it is the only account of the decision that survives."""

    who: str
    when: str

    effect: str
    """The report-facing sentence: `n` excluded, samples truncated at a limit, an arm dropped."""

    decision: str
    """How it was settled — a disposition, or `acknowledged`."""

    members: tuple[str, ...] = ()
    """`id:epoch` per member sample, or the attempts for a window with no sample population."""

    unnamed: int = 0
    """Members the entry did not name — past `SAMPLES_NAMED`, or gone from the census."""

    kind: str = ""


def caveats(
    anomalies: Anomalies,
    acks: Mapping[str, Ack],
    batches: Sequence[InstanceBatch] = (),
    keys: Mapping[str, str] = {},
    current: Mapping[str, str] = {},
    cleared: Collection[str] = (),
    reused: Mapping[str, frozenset[str]] = {},
) -> list[Caveat]:
    """Every caveat the record carries, from both ways in.

    Accepted windows grouped by `(class, ruling instant)` — **the same key the executor applies on**: a class-scoped ruling closing two generations is one decision with one reason, so their refs, tasks and members are **merged**; two generations under two different rulings are two decisions, and merging *those* would file one operator's reasoning under another's.

    **The counts are what reached the data, not what the window absorbed**, and the difference is the whole point of the document. Three samples that failed, were re-run, and failed again put six instances in one window — six failures over three samples, of which only the current attempt's three are in the results. Counting the window would print *6 samples excluded* three lines under a denominator line saying three, which is a footnote contradicting itself. So membership is narrowed to the current attempt exactly as `dispositions` narrows the errored cell, and the extra failures are reported as what they are.

    Args:
        anomalies: The fold — `accepted()` is the filter.
        acks: Acknowledgments, by item id. Those whose kind is in `MARKED` become caveats.
        batches: The turn's census, joined against each window's refs for membership. Absent for a caller that has none, where the window's capped evidence stands in unnarrowed.
        keys: Task identifier to the display key an operator reads. An identifier carries a content hash of the whole task, which is the right thing to key state on and the wrong thing to print in a sentence somebody quotes.
        current: Task identifier to its current attempt's log location — the narrowing. Empty for a caller that has none, which then reports the window's own population.
        cleared: The subjects whose condition demonstrably no longer holds — a log that now reads, a task that has since completed. An acknowledgment names a condition rather than an instant, so one whose condition has cleared is not a caveat: a replaced upload reads, and a stalled task that later finished is in the numbers. Stated as what *cleared* rather than what stands, because a subject nothing here recognises must keep its caveat — dropping a footnote nobody asked to drop is the worse of the two mistakes.
        reused: Per resumed task, the sample uuids its current log holds — what keeps a scan finding on a reused sample in the entry that covers it.

    Returns:
        The caveats, oldest decision first.
    """
    grouped: dict[tuple[str, str], _Group] = {}
    for anomaly in anomalies.accepted():
        ruling = anomaly.ruling
        if ruling is None:
            continue
        key = (anomaly.class_key, ruling.ts)
        if (group := grouped.get(key)) is None:
            grouped[key] = _Group(anomaly=anomaly, ruling=ruling)
        else:
            group.absorb(anomaly)
    listed = [
        caveat
        for group in grouped.values()
        if (caveat := _caveat(group, batches, keys, current, reused)) is not None
    ]
    listed.sort(key=lambda caveat: (caveat.when, caveat.subject))
    listed.extend(
        _from_ack(ack)
        for ack in sorted(acks.values(), key=lambda one: one.ts)
        if ack.kind in MARKED and ack.subject not in cleared
    )
    return listed


@dataclass
class _Group:
    """Every window standing under one decision, accumulated.

    Mutable and private, because it exists only between the grouping pass and the entry it becomes. `_grouped` in the executor does the same accumulation for the same reason and against the same key — the two must agree about what one decision covers, or the record would say a ruling was applied to more than the caveat admits it touched.
    """

    anomaly: Anomaly
    """The first window, for the class key and kind every member of the group shares."""

    ruling: Ruling

    refs: set[str] = field(default_factory=set[str])
    absorbed: int = 0
    """Instances across every window here — how many times the failure *happened*, before the narrowing to what is in the data."""

    tasks: list[str] = field(default_factory=list[str])
    logs: list[str] = field(default_factory=list[str])
    samples: list[str] = field(default_factory=list[str])
    """The capped display list, for a caller whose census has gone quiet."""

    def __post_init__(self) -> None:
        self.absorb(self.anomaly)

    def absorb(self, anomaly: Anomaly) -> None:
        """Fold a window of this decision in — the first one included, so there is one accumulation rather than two that can diverge."""
        self.refs |= set(anomaly.refs)
        self.absorbed += anomaly.evidence.count
        self.tasks = list(dict.fromkeys([*self.tasks, *anomaly.evidence.tasks]))
        self.logs = list(dict.fromkeys([*self.logs, *anomaly.evidence.logs]))
        self.samples = list(dict.fromkeys([*self.samples, *anomaly.evidence.samples]))


def _caveat(
    group: _Group,
    batches: Sequence[InstanceBatch],
    keys: Mapping[str, str],
    current: Mapping[str, str],
    reused: Mapping[str, frozenset[str]] = {},
) -> Caveat | None:
    """One decision, as the entry an operator quotes — or nothing, where it left no mark after all.

    **A decision that no longer touches the data is not a caveat**, which is the document's own filter applied to a case the state machine cannot see. A class accepted on one attempt, relaunched, and come home clean has an `accepted` window forever — that is what the fold records — but nothing of it is in the results, and a footnote saying *3 samples excluded* over samples that are not excluded is worse than no footnote. The decision itself is not lost: it is in the journal, and in *what happened*, which is where an acknowledgment with no mark ends up for the same reason.
    """
    members, unnamed, affected = _members(group, batches, current, keys, reused)
    if affected == 0:
        return None
    return Caveat(
        subject=group.anomaly.class_key,
        what=_what(group, affected),
        scope=_scope(group, affected, keys),
        why=group.ruling.reason,
        who=group.ruling.by,
        when=group.ruling.ts,
        effect=group.anomaly.effect or f"accepted — {group.ruling.reason}",
        decision=group.ruling.disposition.value,
        members=members,
        unnamed=unnamed,
        kind=group.anomaly.kind,
    )


def _what(group: _Group, affected: int) -> str:
    """The *what happened* line, without the class key the entry's own heading already carries.

    **Its own sentence rather than `anomaly_summary`'s**, which the entry used to borrow. That function is the *item's* wording and counts what the window absorbed, which is the right answer to the question an item asks — *how much of this has been seen* — and the wrong one here, where the sentence sits directly above a scope line counting what is in the data. Two numbers in one entry that disagree is worse than either being imprecise.

    The failures the re-runs replaced are named rather than dropped, because *three samples failed twice* and *three samples failed once* are different findings and only the first explains a re-run ruling that came before this one.
    """
    anomaly = group.anomaly
    plural = "s" if affected != 1 else ""
    if anomaly.kind == "limit":
        line = (
            f"{affected} sample{plural} "
            f"{'were' if affected != 1 else 'was'} terminated by an operator"
        )
    elif anomaly.kind == "task":
        line = f"{affected} task attempt{plural} failed"
    elif anomaly.kind == "score":
        line = "every score converts to zero"
    elif anomaly.kind == "scan":
        line = f"{affected} sample{plural} flagged for scoring integrity"
    elif anomaly.kind == "scanerror":
        # **the sample did not error; its scan did**, and the fallback below says
        # the opposite. This entry is the report-facing account of what reached
        # the signed data -- describing an absent verdict as a failed sample
        # sends a reader to the eval looking for a problem that is in the scan
        carry = "carries" if affected == 1 else "carry"
        line = (
            f"the scanner threw on {affected} transcript{plural}, which {carry} "
            f"no verdict either way — the sample{plural} ran normally"
        )
    else:
        line = f"{affected} sample{plural} errored the same way"
    if group.absorbed > affected and anomaly.kind in SAMPLE_SHAPED:
        line += f" ({group.absorbed} failures in all, counting re-runs)"
    if anomaly.substrate:
        line += "; this looks like the machinery under the run"
    return line


def _scope(group: _Group, affected: int, keys: Mapping[str, str] = {}) -> str:
    """How many, and where — in the names an operator reads rather than the ones state is keyed on.

    A task identifier carries a content hash of the whole task, which is exactly right for keying state and unreadable in a sentence somebody quotes into a write-up.
    """
    tasks = [keys.get(task, task) for task in group.tasks]
    unit = "sample" if group.anomaly.kind in SAMPLE_SHAPED else "attempt"
    if not tasks:
        where = ""
    elif len(tasks) <= 3:
        where = f" in {', '.join(f'`{task}`' for task in tasks)}"
    else:
        where = f" across {len(tasks)} tasks"
    return f"{affected} {unit}{'' if affected == 1 else 's'}{where}"


def _members(
    group: _Group,
    batches: Sequence[InstanceBatch],
    current: Mapping[str, str],
    keys: Mapping[str, str],
    reused: Mapping[str, frozenset[str]] = {},
) -> tuple[tuple[str, ...], int, int]:
    """The samples this decision left in the data, named — and how many there are.

    **From the census joined against the decision's refs**, never by taking a ref apart. A ref is composed in one place (`_evalset.instances`) and parsing it back here would make this a second reader of a format one writer owns — and a sample id is free dataset text, so the parse is guessable rather than certain.

    **Narrowed to each task's current attempt**, which is what makes the count a statement about the results rather than about the window. A sample that failed, was re-run, and failed again contributes two refs and one row to the data; counting both would name three samples and claim six. Same predicate as `dispositions`, so the entry and the denominator line beneath the table cannot disagree.

    **Identity is the task and the sample, never the sample alone.** A class key is an exception type and a raising frame, which says nothing about which task raised it — the scope line has always been able to read *across 4 tasks* — so two tasks that each lost `s0:1` are two rows. Keying the set on the sample id collapsed them into one, undercounting every class that spans tasks; where the group does span tasks, each member is qualified with the task a reader can find it in.

    A window whose kind has no sample population names its attempts instead, **narrowed the same way**: an accepted task failure whose attempt a relaunch superseded is not in the results, and an entry going on reporting it would put a hole in the footnotes that the data does not have. The attempt rather than the task is what is asked about, because a `score:zero` task is `COMPLETE` — its caveat is about which attempt the numbers came from, not about whether the task finished.

    Where the census has nothing to say about the class — a curation moved the log, a departed worker's record aged out, or the caller has no census at all — the decision's own capped evidence stands in **unnarrowed**, and the entry is honest that the list is partial rather than pretending it is whole.

    Returns:
        The names, how many are not named, and how many samples the decision left in the data.
    """
    if group.anomaly.kind not in SAMPLE_SHAPED:
        if group.logs and current:
            live = tuple(log for log in group.logs if log in set(current.values()))
            return live, 0, len(live)
        return tuple(group.logs or group.tasks), 0, group.absorbed
    census = [
        instance
        for batch in batches
        if batch.class_key == group.anomaly.class_key
        for instance in batch.instances
    ]
    if census:
        members = {
            (instance.task, f"{instance.sample_id}:{instance.epoch}")
            for instance in census
            if instance.ref in group.refs
            and instance.sample_id
            # a caller with no map narrows nothing, which is the honest answer
            # for one that cannot tell a superseded attempt from a current one
            and (not current or in_results(instance, current, reused))
        }
        # **an empty answer here is an answer**, and treating it as *the census
        # is unavailable* resurrected the dead: a class accepted on one attempt,
        # relaunched, and come home clean would go on reporting the failures of
        # a superseded log as samples excluded from the data -- and the
        # signature would name it as an exception it no longer has
        spans = len({task for task, _ in members}) > 1
        listed = sorted(
            f"{keys.get(task, task)}/{sample}" if spans else sample
            for task, sample in members
        )
        affected = len(listed)
    else:
        # nothing was scanned for this class at all; the journal's capped list
        # is what is left, and its count is the window's rather than the data's
        listed = sorted(dict.fromkeys(group.samples))
        affected = group.absorbed
    # **counted off what is affected rather than off the list**, which is the
    # only formula that stays true on the fallback: there the list is the
    # journal's capped evidence and the count is the window's, so the remainder
    # is what the census can no longer name rather than what the cap dropped
    names = tuple(listed[:SAMPLES_NAMED])
    return names, max(0, affected - len(names)), affected


def _from_ack(ack: Ack) -> Caveat:
    """An acknowledgment that touched the data, as a caveat.

    The same five fields as far as they go, and one it cannot carry: nobody composed an `effect`, because an ack has no disposition to compose one from. So the effect is a sentence per kind, which is the difference between the two paths and the only one worth printing.
    """
    if ack.kind == STALLED:
        effect = "the task's results stand as they are, without it"
    else:
        effect = "the numbers are over what could be read"
    return Caveat(
        subject=ack.subject or ack.id,
        what=ack.summary or ack.id,
        scope=_ACK_SCOPE.get(ack.kind, "1 log"),
        why=ack.reason,
        who=ack.by,
        when=ack.ts,
        effect=effect,
        decision="acknowledged",
        kind=ack.kind,
    )


_ACK_SCOPE = {STALLED: "1 task"}
"""What an acknowledged item is one *of*, per kind. A log is the default because it is what the vocabulary's other entry is."""


def caveat_line(caveat: Caveat) -> str:
    """One caveat as a single line — what `status.md` carries under its anomalies heading.

    The short rendering of the same facts the document below spells out, so a reader glancing at a run and a reader quoting it are told the same thing at two lengths.
    """
    return (
        f"`{caveat.subject}` — {caveat.effect} "
        f"({caveat.decision} by {caveat.who or 'somebody'})"
    )


OUTCOMES_HEADER = ("task", *(label for _, label in OUTCOME_COLUMNS))
"""The by-task table's header row."""


def outcomes_cells(
    outcomes: Mapping[str, Mapping[str, int]], progress: Progress, *, width: int = 0
) -> list[tuple[str, ...]]:
    """The by-task rows: each task's samples that did not take the normal course, in the task table's order.

    Keys are shortened the way the task table shortens them, so a model appears only where it separates two rows. Tasks with nothing to show have no row.

    Args:
        outcomes: Per task identifier, the counts per outcome cell — `Dispositions.outcomes`.
        progress: The turn's rows, for each task's display key and the render order.
        width: Cut display keys to this many characters, or 0 for whole.

    Returns:
        One row per task with a count in some cell; nothing where every sample took the normal course.
    """
    short = short_keys(progress.rows)
    named = {
        row.identifier: key for row, key in zip(progress.rows, short.keys, strict=True)
    }
    listed = [
        row.identifier
        for row in progress.rows
        if any(outcomes.get(row.identifier, {}).values())
    ]
    # a task the census counts and the progress table does not name is not
    # expected, and a table that silently dropped it would be worse than one
    # that prints an identifier
    listed += sorted(
        identifier
        for identifier, cells in outcomes.items()
        if identifier not in named and any(cells.values())
    )
    return [
        (
            clip(named.get(identifier, identifier), width),
            *(
                str(outcomes[identifier][cell])
                if outcomes[identifier].get(cell)
                else EMPTY
                for cell, _ in OUTCOME_COLUMNS
            ),
        )
        for identifier in listed
    ]


def outcomes_table(
    outcomes: Mapping[str, Mapping[str, int]], progress: Progress, *, width: int = 0
) -> list[str]:
    """`outcomes_cells` as a padded markdown table, then the model every row shares named once beneath — or nothing at all where every sample took the normal course."""
    cells = outcomes_cells(outcomes, progress, width=width)
    if not cells:
        return []
    lines = markdown_table(OUTCOMES_HEADER, cells)
    if (model := short_keys(progress.rows).model) is not None:
        lines += ["", f"Every task runs `{model}`."]
    return lines


def anomalies_markdown(
    listed: Sequence[Caveat],
    *,
    scored: str = "",
    header: bool = True,
    table: Sequence[str] = (),
) -> str:
    """The whole document.

    **Rendering only, over caveats somebody else decided on.** The turn computes them once, with the census in hand, and both this document and `status.md`'s one-line marks render what it computed — so the two cannot disagree about which decisions left a mark, which is a claim the module's own docstring makes and which two independent calls could not keep. It matters most where a decision drops out: a caveat this document no longer carries must not survive as a line under the status heading.

    Args:
        listed: The caveats, from `caveats()`.
        scored: The denominator sentence (`998 of 1000 samples scored; 2 excluded`), composed by the caller from the same `Dispositions` the status table's marks note uses — one computation, so the two documents cannot report different denominators.
        header: Whether to carry the regenerated-file banner.
        table: The by-task table from `outcomes_table`, which opens the document ahead of the caveats — the glance before the footnotes. Empty where every sample took the normal course.

    Returns:
        The document. A run with nothing accepted still gets one, saying so — an absent file is indistinguishable from a tend that never ran, and *no caveats* is a finding worth stating to somebody about to quote the numbers.
    """
    lines = ["# anomalies", ""]
    if header:
        lines += [HEADER, ""]
    lines += [
        "Every caveat that reached the final data, and per task the samples that "
        "did not take the normal course. Derived from `journal.jsonl` and the "
        "logs; nothing here is a second record, so it cannot disagree with them.",
        "",
    ]
    if scored:
        lines += [scored, ""]
    if table:
        lines += ["## By task", "", *table, ""]

    if not listed:
        lines += ["No caveats: nothing was accepted into these results.", ""]
        return "\n".join(lines)

    for caveat in listed:
        lines += [f"## `{caveat.subject}`", ""]
        lines.append(f"- **What happened** — {caveat.what}")
        lines.append(f"- **Scope** — {caveat.scope}")
        if caveat.why:
            # verbatim and quoted: the ruling's own words, never reflowed
            lines.append(f'- **Why accepted** — "{caveat.why}"')
        lines.append(
            f"- **Accepted by** — {caveat.who or 'somebody'}, at {caveat.when}"
        )
        lines.append(f"- **Effect on the data** — {caveat.effect}")
        if caveat.members:
            named = ", ".join(f"`{member}`" for member in caveat.members)
            more = f", and {caveat.unnamed} more" if caveat.unnamed else ""
            # the same predicate `_members` picked the list with, so the label
            # cannot come to disagree with what is under it
            what = "Samples" if caveat.kind in SAMPLE_SHAPED else "Attempts"
            lines.append(f"- **{what}** — {named}{more}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "HEADER",
    "MARKED",
    "SAMPLES_NAMED",
    "Caveat",
    "anomalies_markdown",
    "caveat_line",
    "caveats",
    "outcomes_table",
]
