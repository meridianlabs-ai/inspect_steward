"""The journal: the one record in a workspace that nothing can rebuild.

A manifest re-captures from the definition, anomalies re-derive from the log directory, the in-flight record rebuilds from the process table — but a ruling and its reasoning exist nowhere else. So the append here is flushed to disk, and the read is written for the situation after a crash rather than the ordinary one.

The mechanics live in `_util.jsonl`, which the in-flight record shares: single-write appends, damage costing one line, and an unrecognised type reading as data rather than as an error. What is journal-specific is the durability (`sync=True`, the default) and the vocabulary below.

State is derived from this file rather than stored beside it (workflow.md, *State is a fold over the journal*), which is what makes crash recovery the normal code path instead of a rescue routine nobody tests.
"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .._util.jsonl import (
    DamagedLine,
    Event,
    EventRead,
    append_event,
    read_events,
    utc_now,
)

__all__ = [
    "ACKNOWLEDGED",
    "ACTION",
    "ARMED",
    "COLLECTED",
    "DISARMED",
    "INSTANCE",
    "INVESTIGATING",
    "LAUNCHED",
    "OBSERVATION",
    "OPENED",
    "PAUSED",
    "PROPOSAL",
    "RAISED",
    "RAMP_HELD",
    "RAMP_RESUMED",
    "RESOLUTION",
    "RESUMED",
    "RULING",
    "SIGNOFF",
    "Ack",
    "Armed",
    "Collected",
    "DamagedLine",
    "InitializedEvent",
    "JournalEvent",
    "JournalRead",
    "JournalSummary",
    "LaunchedEvent",
    "Paused",
    "Raised",
    "RampHold",
    "Signature",
    "append_event",
    "read_acks",
    "read_armed",
    "read_collected",
    "read_journal",
    "read_launched",
    "read_pause",
    "read_raised",
    "read_ramp_holds",
    "read_signoff",
    "summarize",
    "utc_now",
]

OBSERVATION = "observation"
"""Journal event: what one turn saw, and the settings it saw it under.

Written by every executed turn (`_tend.turn`), whether or not anything happened, because an agent reads the run as a **time series** and its own memory does not survive a session boundary — there are several of those in a night. If the series is not written down it does not exist, and the 6am agent inherits a list of open items with no idea which are getting worse (workflow.md, *The journal records observations, not only decisions*).

It is also what makes degrading `_steward.yaml` possible: the settings in force are recorded here, so a turn that cannot parse the file has somewhere to read the last good ones from.

Named here with the other event types rather than beside its writer because two folds read it: the next turn diffs its `items` to find what appeared and what resolved, and `read_raised` uses the same list to expire a hand-off whose item is gone. It is the only record of a condition having *stopped* being true.
"""

ACKNOWLEDGED = "acknowledged"
"""Journal event: somebody looked at an item and accepted it.

The one event kind an item list was not supposed to need. Items are a projection, so a condition that ends stops being reported and a decision keeps its subject open — but neither covers a real condition nothing will clear mechanically that somebody has already accepted. Without this, a definition edited on purpose reports drift every ten minutes for the rest of the run, which is how an attention list stops being read.

Written by `steward ack`, never by a tend. It carries `id`, `by` (`agent` or `human`), and a required `reason` — the discipline `inspect ctl` already imposes on every applied change, and what makes *who decided, and why* (workflow.md, *The audit trail*) true of this act too.
"""

RAISED = "raised"
"""Journal event: the agent put an item in front of the person who can decide it.

**It closes nothing, and that is the whole point.** An item owned by a human stays open until a human rules on it — but the *agent's* work on it ended when it was surfaced, and without a record of that the item returns to the agent's queue at every collection all night. Reading is the wrong verb for something the reader cannot dispose of (agent.md §2.2), so raising is the verb.

The third of three item states, between *needs the agent* and *closed*. What it changes is one projection: `steward collect` sets a raised item aside and counts it; `status` still shows it, because a person still owes an answer.

Carries `id` and an optional `note` — optional where `acknowledged`'s reason is required, because disposing of a decision owes an account and handing one off does not.
"""

COLLECTED = "collected"
"""Journal event: how far an agent has read.

A **position and nothing else** — no note, no claim about having acted. An event that asserts two things at once is eventually read as the wrong one, and what was *done* is recorded by `acknowledged`, `raised`, and `action` at the moment it was done.

The position is a journal line number (`Event.line`), which is assigned when the file is read rather than written into it — see that property for why a stored sequence number would race.

Two things read this fold: the delta an arriving agent is shown, and the collection age, which sits beside the tend age so that *the timer stopped* and *nobody is looking* are distinguishable (agent.md §2.2).

Carries `position`.
"""

PAUSED = "paused"
"""Journal event: stop scheduling new work.

**Here rather than in `.steward/`, and the difference is a safety property.** `.steward/` is disposable by construction — deleting it is documented as costing nothing — so a pause flag living there means clearing a cache silently *resumes* an expensive run overnight. Between the two directions this can fail in, a pause that outlives a wiped cache is recoverable and a resume nobody asked for is not.

Carries `by` (`agent` or `human`) and a required `reason`. A tend never writes it.
"""

RESUMED = "resumed"
"""Journal event: schedule again.

No reason, unlike its opposite: pausing asserts something about the run that a later reader will want explained, and resuming only restores the default.
"""

ACTION = "action"
"""Journal event: something Steward did, and how it turned out.

Written by a tend for each act worth a reader's attention — an archive, a departure with work stranded, a retune. Named here beside the other types because more than one module reads it: the history section admits it, and the tuning loop folds its `ramp` entries back into the levels a respawned worker should start at.
"""

RAMP_HELD = "ramp_held"
"""Journal event: stop climbing sample concurrency, leaving the levels where they are.

The agent's brake on the tuning loop, and deliberately **not** a brake on its safety half: a tend under a hold takes no up-steps, and still cuts on sustained pushback, because the cut is the move that exists precisely for when nobody is watching.

Carries `by`, a required `reason`, and an optional `identifier` — one task's ramp, or the fleet's when absent. In the journal rather than `.steward/` for the same reason the pause is: a hold that a cleared cache silently released would resume climbing into whatever the holder saw coming.
"""

RAMP_RESUMED = "ramp_resumed"
"""Journal event: let the tuning loop climb again.

Carries an optional `identifier`, matching the hold it releases; absent, it releases the fleet-wide hold and every per-task one — the bare verb means *ramp freely again*, not *ramp except where I have forgotten I said otherwise*.
"""

ARMED = "armed"
"""Journal event: a timer was installed, and by which scheduler.

What makes *the timer is not running* detectable at all. A scheduler cannot report its own absence, and probing one costs a subprocess on every turn — so the fact that a timer was installed is recorded once, here, and every later turn compares that record against how long it has actually been since a tend (`_tend.items`, `unsupervised`).

Carries `scheduler`, `interval` in seconds, and `label`.
"""

DISARMED = "disarmed"
"""Journal event: the timer was removed. Carries `scheduler`."""

LAUNCHED = "launched"
"""Journal event: somebody started this run, and what they started it with.

**Not `_worker.inflight`'s `launched`**, which is the same word about a different subject — that one says a worker's spawn returned, and lives in a rebuildable record. This one says a *run* was launched, and is here because losing it would change what Steward reports.

What it buys is one item's correctness. `unsupervised` is gated on a timer having been armed, deliberately, so a workspace nobody armed stays quiet rather than nagging somebody sitting at the terminal typing `steward tend` (`_tend.items`). `launch --no-timer` falls in the gap that leaves: the operator asked to run unsupervised, execution.md §8.3 requires that to *look* unsupervised, and with nothing recorded the run looks exactly like a hand-driven experiment nobody promised to schedule. So a launch writes itself down, and the item asks *did anyone launch this* as well as *did anyone arm it*.

Carries `tasks`, `definition`, and `timer` — the scheduler armed, or `None` where the launch was told not to arm one.
"""

NOTIFIED = "notified"
"""Journal event: a post went out about something no diff would latch.

**Not written for every post**, which would be a second answer to a question the observation already answers. Steward's ordinary triggers are edges — an item is in this turn's list and not the previous one's, a task is complete now and was not before — so the diff is what stops them repeating, and recording each one would only give the two records a chance to disagree.

A turn that *raises* has no edge to stand on. It never reaches its observation, so the next failure is indistinguishable from this one, and a `_steward.yaml` that has been malformed since Tuesday would post every ten minutes all night — the noise fatigue notification exists to avoid, produced by the feature meant to prevent silence. This is what makes that one post per distinct failure instead, re-armed by the next turn that actually runs.

Carries `kind` and `subject`, the latter a fingerprint of what was said so that a *different* failure is still heard.
"""

UNDELIVERED = "undelivered"
"""Journal event: a post about this turn's edge did not reach anybody.

**The counterweight to every trigger being a diff.** An edge is consumed by the observation that records it — the item is in this turn's list, so it is not new next turn — and that is exactly what stops a condition being reported every ten minutes. It also means a post that *failed* has spent the only chance its news had: a notifier unreachable for one minute at 2am loses the gate, or the park, permanently, and the next turn sees a run in which nothing changed.

So a turn that could not deliver writes down what it was carrying, and the next turn subtracts that from the baseline it diffs against (`read_undelivered`). The edge is therefore retained rather than retried — the following turn recomputes a post from the current state and includes what was owed, which is right for a message that describes a moment rather than a queue of moments.

Carries `items` (the ids that appeared) and `complete` (the display keys that finished). Read only back to the most recent `OBSERVATION`, since a turn that delivered records a baseline that already accounts for everything before it.
"""

OPENED = "opened"
"""Journal event: a class of failures has a window absorbing instances.

Written by a tend, mechanically, when detection finds instances of a class with no absorbing window — the first ever, or the first after a ruling closed the previous one. Carries `class`, `kind` (`error` | `limit` | `task` | `score`), and `substrate` (whether the failure is the machinery under the run, which forbids a re-run proposal until a person has looked — execution.md §9.1).
"""

INSTANCE = "instance"
"""Journal event: new instances of a class, batched per turn.

**At most one per class per turn**: five hundred errored samples arriving in one interval are one `opened` and one `instance` with `count=500`, never five hundred lines. The journal carries the time series and the decision trail; the authoritative instance set re-derives from the log directory every turn (this file's own module docstring: anomalies re-derive from logs).

Carries `class`, `count`, and `refs` — one content-derived ref per instance, the dedupe ledger the fold maintains — plus capped evidence: `samples` (≤20 `id:epoch`), `tasks`, `logs`, `exemplar` (one verbatim message, display-only, never identity).
"""

INVESTIGATING = "investigating"
"""Journal event: the agent is working a class before proposing anything.

A sixth event rather than a flag on `proposal`, because investigation precedes proposing and must survive a session boundary: the next agent must not re-propose what the last one was mid-way through, and `status` must be able to say a class is being worked (workflow.md §12.5). Carries `class`, `by`, `note`.
"""

PROPOSAL = "proposal"
"""Journal event: the agent's grouping judgement — these classes are one decision.

Carries `id` (`prop-<digest8>`), one `action`, the covered `classes` with per-class evidence snapshotted from the fold (count, exemplar, window, precedent) — snapshotted by the verb so the record shows what the human was shown, and so a partial answer is possible — plus `reason` and `by`. A later proposal covering a class supersedes the earlier one for that class.
"""

RULING = "ruling"
"""Journal event: a human decided what a class of failures means.

**One event per class**: ruling a twelve-class proposal appends twelve lines sharing a `proposal` id, which is what lets a group decision be unpicked later (workflow.md §5.6). Carries `class`, `disposition` (`rerun` | `exclude` | `zero` | `score` | `accept` | `dismiss`), a required `reason`, `by` — free text naming who decided, never a role, with room for `policy` when a standing pre-authorization applies (step 25) — `effect` (the report-facing sentence for the dispositions that mark the data), and `proposal`.

A ruling closes the class's window: the next instance opens a new generation carrying the old rulings as precedent.
"""

SIGNOFF = "signoff"
"""Journal event: a person accepted these results. The terminal act.

**There is no un-sign event, and the absence is the design.** A signature is a thing that happened, on a date, by somebody — the journal records it and later facts qualify it rather than erasing it, exactly as a superseded ruling stays in the record. What a second signoff writes is a second signature, and the fold's last word wins.

Carries `by` (free text, a name on a document), an optional `note`, the `digest` of the manifest it covered, the `tasks` and `accepted` counts that were true at the time, the `exceptions` — the class keys whose caveats it signed over — and `curated`, the number of superseded attempts it moved.

**It pins to the manifest digest rather than to a run** (workflow.md §2.4), which is what gives invalidation a precise trigger: the definition changed, so what was signed is no longer what is current.
"""

RESOLUTION = "resolution"
"""Journal event: what happened after a `rerun` ruling, or a task class healing.

Written by a tend, mechanically, when it observes the outcome. Carries `class`, `outcome` (`reran_passed` | `reran_failed`), and `detail`; a `reran_failed` also carries the `refs` ledger entries for the instances it consumed, so the fold absorbs them rather than re-counting them as news. **Not** written for accepted dispositions — ACCEPTED derives from the ruling itself, and an echo event is two records that can disagree (the same argument as `NOTIFIED`).
"""

JournalEvent = Event
"""One event in the journal."""

JournalRead = EventRead
"""Everything a journal file yielded, including what it could not."""


class InitializedEvent(JournalEvent):
    """The workspace was created.

    One of two typed events. Every other type in workflow.md's table arrives with the step that writes it, rather than being transcribed ahead of the code that gives it meaning.
    """

    definition: str | None = None
    """Definition filename the workspace expects, if it had one at `init`."""


class LaunchedEvent(JournalEvent):
    """A run was launched, and under what.

    Typed because a person reads this line: it is the top of the story every later `observation` continues, and the one place the journal says what the run is *of*.
    """

    definition: str
    """The definition captured, as the manifest records it — so a journal read months later names the file even if it has since moved."""

    tasks: int
    """How many tasks the committed manifest holds."""

    timer: str | None = None
    """Scheduler armed, or `None` where the launch was told not to arm one. What `read_launched` is folded for."""


@dataclass(frozen=True)
class Ack:
    """One disposal, as the fold reports it."""

    id: str
    by: str
    """`agent` or `human`. An agent disposing of a transient it investigated is its own ack, not a human's relayed through it."""

    reason: str
    ts: str

    kind: str = ""
    """The item kind, as the acknowledging verb recorded it.

    Read because an acknowledgment whose subject left a mark on the results is a caveat and belongs in `anomalies.md` exactly as a ruling would (workflow.md §14), and which kinds those are is the routing question. Defaulted because an event written before this field was read is data rather than damage — it simply cannot be routed, which is the honest answer for a record that never said what it was about.
    """

    subject: str = ""
    summary: str = ""
    """What the item said when it was disposed of — the *what happened* line of its caveat entry."""


@dataclass(frozen=True)
class Collected:
    """How far an agent has read, and when it last did."""

    position: int
    """Journal line number read to."""

    ts: str
    """When, which is the other half of what this fold is for: the collection age sits beside the tend age so that *the timer stopped* and *nobody is looking* are distinguishable."""


@dataclass(frozen=True)
class Raised:
    """One hand-off, as the fold reports it."""

    id: str
    note: str
    """What the agent did to surface it, or empty. Optional by design — see `RAISED`."""

    ts: str


@dataclass(frozen=True)
class Paused:
    """The pause in force, as the fold reports it."""

    by: str
    """`agent` or `human`."""

    reason: str
    ts: str


@dataclass(frozen=True)
class RampHold:
    """One hold on the tuning loop, as the fold reports it."""

    by: str
    """`agent` or `human`."""

    reason: str
    ts: str

    identifier: str = ""
    """The task whose ramp is held, or empty for the fleet's."""


@dataclass(frozen=True)
class Armed:
    """The timer in force, as the fold reports it."""

    scheduler: str
    """Which backend installed it: `launchd`, `systemd`, or `cron`."""

    interval: int
    """Seconds between tends, as the arming asked for."""

    label: str
    """The scheduler's name for this workspace's entry."""

    ts: str


@dataclass(frozen=True)
class Signature:
    """The attestation in force, as the fold reports it."""

    by: str
    """Who accepted the results. Free text — a name on a document, never a role."""

    note: str
    """What they wanted said about it, or empty. Optional by design: the account of every decision is already in the journal, and a required field here would collect *results look good* at scale."""

    ts: str

    digest: str
    """The manifest digest this signature covered. What it is compared against to decide whether it still stands."""

    exceptions: tuple[str, ...] = ()
    """The class keys whose caveats were signed over — `anomalies.md`'s contents at the moment of signing, by name."""


@dataclass(frozen=True)
class JournalSummary:
    """What a journal says, at a glance."""

    count: int
    counts_by_type: dict[str, int]
    first_ts: str | None
    last_ts: str | None
    last: JournalEvent | None


def read_journal(journal: Path) -> JournalRead:
    """Read every event in a journal, reporting what could not be read.

    Args:
        journal: Path to `journal.jsonl`.

    Returns:
        The events, in file order, and one entry per line that was not one. A journal that does not exist is an empty history rather than damage.

    Raises:
        OSError: If the file exists but cannot be read. A missing journal is expected; an unreadable one is not.
    """
    return read_events(journal)


def read_acks(events: list[JournalEvent]) -> dict[str, Ack]:
    """Fold a journal down to what has been disposed of.

    An acknowledgment says a person or an agent looked at something nothing will clear mechanically and accepted it. The item then leaves every surface — `status.md`, the summary, the channel, the verdict — and this record is what it leaves behind (plan.md step 14).

    Keyed on the **item id**, which is chosen per kind so that a material change produces a different one. That is what makes a permanent-looking suppression safe: acknowledging a definition edit does not acknowledge the next edit, because the next edit hashes differently and is therefore a different item.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent acknowledgment per item id. Later wins, so a re-acknowledgment carries the newer reason.
    """
    acks: dict[str, Ack] = {}
    for event in events:
        if event.type != ACKNOWLEDGED:
            continue
        identifier = event.payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            # a payload this version does not understand is data, not damage
            continue
        by = event.payload.get("by")
        reason = event.payload.get("reason")
        kind = event.payload.get("kind")
        subject = event.payload.get("subject")
        summary = event.payload.get("summary")
        acks[identifier] = Ack(
            id=identifier,
            by=by if isinstance(by, str) else "",
            reason=reason if isinstance(reason, str) else "",
            ts=event.ts,
            kind=kind if isinstance(kind, str) else "",
            subject=subject if isinstance(subject, str) else "",
            summary=summary if isinstance(summary, str) else "",
        )
    return acks


def read_signoff(events: list[JournalEvent]) -> Signature | None:
    """Fold a journal down to the attestation in force.

    Last wins, and there is nothing that clears one: **a signature is a thing that happened**, so a second signoff amends rather than replaces, and both lines stay in the record as what was believed at the time. Whether the standing one still *stands* is a separate question — the digest it pinned and the windows opened since it answer that, and neither is a fold (`_signoff.gate`).

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent signature, or `None` where nobody has signed.
    """
    signature: Signature | None = None
    for event in events:
        if event.type != SIGNOFF:
            continue
        by = event.payload.get("by")
        if not isinstance(by, str) or not by:
            # a payload this version does not understand is data, not damage --
            # but an attestation with nobody behind it is not an attestation
            continue
        note = event.payload.get("note")
        digest = event.payload.get("digest")
        signature = Signature(
            by=by,
            note=note if isinstance(note, str) else "",
            ts=event.ts,
            digest=digest if isinstance(digest, str) else "",
            exceptions=tuple(_listed(event.payload.get("exceptions"))),
        )
    return signature


def read_raised(events: list[JournalEvent]) -> dict[str, Raised]:
    """Fold a journal down to what the agent has handed to its owner, and has not handed off twice.

    Keyed on the **item id** for the same reason `read_acks` is, and for most kinds that is the whole story: a stall raised at attempt 2 does not raise the one at attempt 3, because the attempt count is in the id.

    **A hand-off expires when a turn observes the item gone**, which the ids alone cannot express. Not every id encodes an instance — a park is keyed on its task, deliberately, so that one item stays stable while several samples in it wait rather than churning as each is answered. Without expiry that stability becomes silence: the first park is raised, somebody answers it, a second approval arrives in the same task hours later, and the id it re-uses is still marked handed-off, so `collect` sets aside a decision nobody has been told about. An observation records the ids that were open when it was written, so an id absent from one is a condition that ended — and a hand-off refers to *the episode it was made about*, which is over.

    An acknowledgment deliberately does **not** expire this way. The two acts differ in what they are about: raising says *I told somebody about this*, which stops being true of a condition that has been and gone, while acking says *this is accepted*, which stays true of the thing that was accepted however many times it recurs.

    The expiry is only as fine as the tend cadence, which is worth stating rather than hiding: a condition that appeared and cleared entirely between two turns was never observed to resolve, so a hand-off made inside that window survives it. What that costs is one un-repeated hand-off in the case where somebody is plainly present and answering.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent hand-off per item id still open since it was made.
    """
    raised: dict[str, Raised] = {}
    for event in events:
        if event.type == OBSERVATION:
            open_ids = event.payload.get("items")
            # a payload with no item list is a turn this version cannot read
            # the open set from, not a turn that saw nothing -- expiring on it
            # would clear every hand-off in the file
            if isinstance(open_ids, list):
                still_open = {
                    entry
                    for entry in cast(list[object], open_ids)
                    if isinstance(entry, str)
                }
                raised = {
                    identifier: hand_off
                    for identifier, hand_off in raised.items()
                    if identifier in still_open
                }
            continue
        if event.type != RAISED:
            continue
        identifier = event.payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        note = event.payload.get("note")
        raised[identifier] = Raised(
            id=identifier,
            note=note if isinstance(note, str) else "",
            ts=event.ts,
        )
    return raised


def read_collected(events: list[JournalEvent]) -> Collected | None:
    """Fold a journal down to how far an agent has read.

    A switch rather than an accumulation, like the pause: the last word wins, so a `--since` that deliberately reaches backwards and then advances again leaves the newer position behind.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent collection, or `None` where nobody has collected — which reads as *everything is new*, the right answer for a workspace no agent has attached to. Distinct from position `0`, which an agent could deliberately collect at.
    """
    collected: Collected | None = None
    for event in events:
        if event.type != COLLECTED:
            continue
        value = event.payload.get("position")
        if isinstance(value, int) and not isinstance(value, bool):
            collected = Collected(position=value, ts=event.ts)
    return collected


def read_pause(events: list[JournalEvent]) -> Paused | None:
    """Fold a journal down to whether the run is paused.

    A two-state fold rather than an accumulating one, so the last word wins and a double pause or a resume with nothing to resume is simply the state it leaves behind rather than an error somebody has to handle.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The pause in force, or `None` where the run is scheduling normally.
    """
    paused: Paused | None = None
    for event in events:
        if event.type == RESUMED:
            paused = None
        elif event.type == PAUSED:
            by = event.payload.get("by")
            reason = event.payload.get("reason")
            paused = Paused(
                by=by if isinstance(by, str) else "",
                reason=reason if isinstance(reason, str) else "",
                ts=event.ts,
            )
    return paused


def read_ramp_holds(events: list[JournalEvent]) -> dict[str, RampHold]:
    """Fold a journal down to the holds on the tuning loop.

    Keyed by task identifier, with the empty string for the fleet-wide hold, so *hold everything* and *hold this arm* compose rather than overwrite: a fleet hold does not erase a per-task one somebody placed for a different reason, and releasing one leaves the other standing. A bare resume clears the lot — see `RAMP_RESUMED`.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The holds in force, empty where the loop may climb freely.
    """
    holds: dict[str, RampHold] = {}
    for event in events:
        if event.type == RAMP_RESUMED:
            identifier = event.payload.get("identifier")
            if isinstance(identifier, str) and identifier:
                holds.pop(identifier, None)
            else:
                holds = {}
        elif event.type == RAMP_HELD:
            by = event.payload.get("by")
            reason = event.payload.get("reason")
            identifier = event.payload.get("identifier")
            key = identifier if isinstance(identifier, str) else ""
            holds[key] = RampHold(
                by=by if isinstance(by, str) else "",
                reason=reason if isinstance(reason, str) else "",
                ts=event.ts,
                identifier=key,
            )
    return holds


def read_armed(events: list[JournalEvent]) -> Armed | None:
    """Fold a journal down to what timer is installed.

    The same two-state shape as `read_pause`. What it reports is what the *arming* said, not what the scheduler currently holds — nothing here shells out, because a turn runs this every ten minutes and `steward timer status` is where paying for the truth belongs.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The timer in force, or `None` where none was ever armed or the last word was a disarm.
    """
    armed: Armed | None = None
    for event in events:
        if event.type == DISARMED:
            armed = None
        elif event.type == ARMED:
            scheduler = event.payload.get("scheduler")
            interval = event.payload.get("interval")
            label = event.payload.get("label")
            if not isinstance(scheduler, str) or not isinstance(interval, int):
                # a payload this version does not understand is data, not damage
                continue
            armed = Armed(
                scheduler=scheduler,
                interval=interval,
                label=label if isinstance(label, str) else "",
                ts=event.ts,
            )
    return armed


def read_launched(events: list[JournalEvent]) -> str | None:
    """Fold a journal down to when this run was last launched.

    **Accumulating rather than two-state, because there is no un-launch.** `read_armed` and `read_pause` both have an event that undoes them, so the last word wins; a launch is a thing that happened, and the second one amends the first rather than cancelling it (workflow.md, *A second `launch` is the amend path*). So this only ever moves forward, and its answer to *was this run launched* is monotonic — which is the property `ever_launched` needs to be worth gating on.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        When the most recent launch was, or `None` where nothing ever launched this workspace.
    """
    launched: str | None = None
    for event in events:
        if event.type == LAUNCHED:
            launched = event.ts
    return launched


def read_notified(events: list[JournalEvent]) -> set[str]:
    """Fold a journal down to what has been posted since the last turn that ran.

    **The window is the most recent `OBSERVATION`, not the whole file**, and that is the latch's release. A turn reaching its observation is the run working again; the next failure after that is news, and posting it is the whole point. Without the window a workspace that broke, was fixed, and broke again the same way would go quiet on the second break — a latch that silences the case it exists to report.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The `subject` of every `NOTIFIED` event after the most recent observation.
    """
    subjects: set[str] = set()
    for event in reversed(events):
        if event.type == OBSERVATION:
            break
        if event.type == NOTIFIED and isinstance(
            subject := event.payload.get("subject"), str
        ):
            subjects.add(subject)
    return subjects


def read_undelivered(
    events: list[JournalEvent],
) -> tuple[frozenset[str], frozenset[str]]:
    """Fold a journal down to the edge a failed post is still owed.

    The window is the most recent `OBSERVATION` for the reason `read_notified`'s is: a turn that reached its observation recorded a baseline covering everything before it, so an older `UNDELIVERED` has already been accounted for or superseded. In practice there is at most one, written by the same turn that recorded the observation just above it.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        Item ids and completed display keys that were not delivered, to be subtracted from the baseline the next diff runs against.
    """
    items: set[str] = set()
    complete: set[str] = set()
    for event in reversed(events):
        if event.type == OBSERVATION:
            break
        if event.type != UNDELIVERED:
            continue
        items |= set(_listed(event.payload.get("items")))
        complete |= set(_listed(event.payload.get("complete")))
    return frozenset(items), frozenset(complete)


def _listed(value: object) -> list[str]:
    """The strings in a payload list, over a payload that may hold anything."""
    if not isinstance(value, list):
        return []
    return [one for one in cast(list[Any], value) if isinstance(one, str)]


def summarize(events: list[JournalEvent]) -> JournalSummary:
    """Fold a journal down to what it says at a glance.

    The smallest useful fold, and the shape every later one takes: state is computed from the events on demand rather than maintained beside them.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        Counts, span, and the most recent event.
    """
    return JournalSummary(
        count=len(events),
        counts_by_type=dict(Counter(event.type for event in events)),
        first_ts=events[0].ts if events else None,
        last_ts=events[-1].ts if events else None,
        last=events[-1] if events else None,
    )
