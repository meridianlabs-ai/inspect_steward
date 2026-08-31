"""What happened to this run, as a person reads it.

The summary's third section (agent.md §4.3). Where the items above it say what is *open* and the table says where the run *stands*, this says what has been *done* — by Steward, by an agent, by a person — and it is the only place a reader arriving at six can learn that somebody dealt with something at two.

**Complete rather than a delta, deliberately.** It reports everything material since the workspace began, not everything since some mark, which is what keeps the summary stateless: `status` needs no cursor, and the collection cursor is left with one job instead of two.

**Completeness is affordable because admission is narrow.** The test:

> It happened *to* the run rather than *as* the run.

A task completing is the run happening; a task retried because its worker died is something that happened to it. And the corollary that bounds the rest: anything that can occur hundreds of times a night is a count, never a list of instances.

**The source is the journal and never `steward.log`.** That boundary is already drawn — a failed tend, a spawn error, a sync timeout are records of the runner working or not, and they belong to the operational log (workflow.md, *The journal records observations, not only decisions*). Here, one filter over the events a turn already read.

**Each entry carries the journal position it came from**, which is what lets `steward collect` mark what is new without this module knowing anything about cursors.
"""

from dataclasses import dataclass, field
from typing import cast

from .._workspace import (
    ACKNOWLEDGED,
    ACTION,
    ARMED,
    DISARMED,
    LAUNCHED,
    PAUSED,
    RAISED,
    RAMP_HELD,
    RAMP_RESUMED,
    RESUMED,
    RULING,
    JournalEvent,
)

_ADMITTED = frozenset(
    {
        ACKNOWLEDGED,
        RAISED,
        ACTION,
        PAUSED,
        RESUMED,
        RAMP_HELD,
        RAMP_RESUMED,
        ARMED,
        DISARMED,
        LAUNCHED,
        RULING,
    }
)
"""Event types this section reports.

An allow-list rather than a deny-list, because the journal's vocabulary grows and the default for something this file has never heard of has to be *not shown* — a section that admits by default would start rendering raw payloads the first time a later step adds a type. `observation` is the notable exclusion: sixty a night, and it is the run happening rather than something happening to it.
"""


@dataclass(frozen=True)
class Entry:
    """One thing that happened."""

    ts: str
    """When, UTC ISO-8601."""

    position: int
    """The journal line it came from. What `collect` marks *new* against, so this module needs to know nothing about cursors."""

    kind: str
    """The journal event type, for a caller that wants to filter without re-reading."""

    text: str
    """One line, in the past tense, already worded for a reader."""


@dataclass(frozen=True)
class Happened:
    """Everything material that has been done to this run."""

    entries: list[Entry] = field(default_factory=list[Entry])

    def since(self, position: int) -> list[Entry]:
        """Entries after a journal position."""
        return [entry for entry in self.entries if entry.position > position]

    def before(self, position: int) -> int:
        """How many entries a `since` would leave out. What keeps an omission counted rather than silent."""
        return sum(1 for entry in self.entries if entry.position <= position)


def happened(events: list[JournalEvent]) -> Happened:
    """Fold the journal into what a reader should be told was done.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        One entry per admitted event, oldest first — the order things happened in, which is the order somebody reconstructing a night reads them.
    """
    entries = [
        Entry(ts=event.ts, position=event.line, kind=event.type, text=text)
        for event in events
        if event.type in _ADMITTED
        for text in [_describe(event)]
        if text
    ]
    return Happened(entries=entries)


def _describe(event: JournalEvent) -> str:
    """One line for an event, or empty where its payload does not support one.

    A payload this version does not understand yields nothing rather than a partial line: the journal is history and an unrecognised shape is still history, but rendering half of it to a person is worse than rendering none.
    """
    payload = event.payload
    if event.type == ACKNOWLEDGED:
        by = _text(payload, "by") or "somebody"
        reason = _text(payload, "reason")
        summary = _text(payload, "summary") or _text(payload, "id")
        return f"accepted by {by}: {summary}" + (f" — {reason}" if reason else "")
    if event.type == RAISED:
        summary = _text(payload, "summary") or _text(payload, "id")
        note = _text(payload, "note")
        return f"raised with a person: {summary}" + (f" — {note}" if note else "")
    if event.type == ACTION:
        return _action(payload)
    if event.type == PAUSED:
        by = _text(payload, "by") or "somebody"
        reason = _text(payload, "reason")
        return f"paused by {by}" + (f" — {reason}" if reason else "")
    if event.type == RESUMED:
        return "resumed"
    if event.type == RAMP_HELD:
        by = _text(payload, "by") or "somebody"
        task = _text(payload, "task") or _text(payload, "identifier")
        what = f"the ramp on {task}" if task else "the ramp"
        reason = _text(payload, "reason")
        return f"{what} held by {by}" + (f" — {reason}" if reason else "")
    if event.type == RAMP_RESUMED:
        task = _text(payload, "task") or _text(payload, "identifier")
        return f"the ramp on {task} resumed" if task else "the ramp resumed"
    if event.type == ARMED:
        scheduler = _text(payload, "scheduler") or "a scheduler"
        interval = payload.get("interval")
        every = f" every {interval}s" if isinstance(interval, int) else ""
        return f"armed {scheduler}{every}"
    if event.type == DISARMED:
        scheduler = _text(payload, "scheduler") or "the timer"
        return f"disarmed {scheduler} — nothing tends this run automatically"
    if event.type == LAUNCHED:
        tasks = payload.get("tasks")
        count = f"{tasks} tasks" if isinstance(tasks, int) else "the eval set"
        return f"launched {count} from {_text(payload, 'definition') or 'a definition'}"
    if event.type == RULING:
        # the one decision the whole anomaly machinery exists to reach, and
        # exactly what a reader arriving at six needs from two: what was
        # decided, by whom, and the mark the data carries for it
        class_key = _text(payload, "class")
        disposition = _text(payload, "disposition")
        if not class_key or not disposition:
            return ""
        by = _text(payload, "by") or "somebody"
        reason = _text(payload, "reason")
        effect = _text(payload, "effect")
        line = f"ruled {class_key}: {disposition} by {by}"
        if reason:
            line += f" — {reason}"
        if effect:
            line += f" ({effect})"
        return line
    return ""


def _action(payload: dict[str, object]) -> str:
    """One line for something Steward did.

    Archiving is the one that matters most to state plainly: *Steward never deletes an eval log* is a headline guarantee, and this is where it shows its work. A worker that went away with work still to do is the one a reader of an overnight history most needs: a task that died at 1am and was picked up again is invisible in any snapshot, because by morning the snapshot shows it running normally.
    """
    action = _text(payload, "action")
    if action == "reap":
        return _reap(payload)
    if action == "archive":
        reason = _text(payload, "reason") or "superseded"
        location = _text(payload, "location")
        # the reason is a bare adjective from a growing vocabulary, so it goes
        # after the noun rather than in front of it -- *a orphaned log* is what
        # putting it in front costs, and picking an article per word is a rule
        # the next reason has to remember
        return f"archived a log — {reason}" + (f": {location}" if location else "")
    if action == "ramp":
        return _ramp(payload)
    if action == "claim_broke":
        pid = payload.get("pid")
        where = f" (pid {pid})" if isinstance(pid, int) else ""
        return f"killed a wedged claim holder{where} to take the claim"
    if action == "status_unwritable":
        return "status.md stopped being writable — the snapshot is frozen"
    if action == "status_unwritable_restored":
        return "status.md is being written again"
    if action == "sync_failed":
        target = _text(payload, "target")
        return f"the workspace stopped propagating{f' to {target}' if target else ''}"
    if action == "sync_restored":
        target = _text(payload, "target")
        return f"propagation{f' to {target}' if target else ''} recovered"
    return action or ""


def _ramp(payload: dict[str, object]) -> str:
    """One line for a retune the tuning loop made.

    This is the notification your plan's agent reads: an attending agent's next collection shows these lines as new, which is what tells it a level moved and that the tuning block is worth a look. The knob is named in the reader's terms — sample concurrency against the connection ceiling — because `max_samples` in a history line is jargon where *ramped* is a verb.
    """
    task = _text(payload, "task") or _text(payload, "identifier") or "a task"
    at, to = payload.get("at"), payload.get("to")
    move = f" {at}→{to}" if isinstance(at, int) and isinstance(to, int) else ""
    reason = _text(payload, "reason")
    tail = f" — {reason}" if reason else ""
    if _text(payload, "knob") == "max_connections":
        return f"retuned the connection ceiling on {task}{move}{tail}"
    return f"ramped {task}{move}{tail}"


def _reap(payload: dict[str, object]) -> str:
    """One line for a worker that exited with its task unfinished.

    Only departures that left wanted work undone are journalled. *Never started* is kept apart from *died*, because the two point at different things — a spawn that did not take, against a process that went away mid-task — and a reader chasing either would waste the night on the other.

    **The promise of a retry is made only where the turn actually made one.** A departure on the turn the stall guard trips is reaped and not respawned, and so is one on a run already at its width; saying *it will be tried again* there sends a reader looking for a worker that was never going to start.
    """
    # the display keys, never the identifiers beside them: an entry a person
    # cannot read is an entry that costs the section its readers
    tasks = ", ".join(_strings(payload.get("tasks")))
    where = f" (pid {pid})" if isinstance(pid := payload.get("pid"), int) else ""
    what = tasks or "its task"
    again = "; it is being tried again" if payload.get("retrying") is True else ""
    if _text(payload, "reason") == "never_started":
        return f"a worker never started{where}; {what} did not run{again}"
    return f"a worker exited{where} with {what} unfinished{again}"


def _strings(value: object) -> list[str]:
    """A list of strings from a payload, which may hold anything."""
    if not isinstance(value, list):
        return []
    return [entry for entry in cast(list[object], value) if isinstance(entry, str)]


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    return value if isinstance(value, str) else ""
