"""The tuning loop: what to retune this turn, decided from signals alone.

Two control loops share the throughput problem and must not fight, and the discipline that keeps them apart is one sentence: **inspect's adaptive controllers move within bounds; tend moves the bounds.** The controllers discover a provider's level minute by minute — additive increase, multiplicative decrease — and nothing here second-guesses that. What they cannot do is see the fleet, raise a task's sample setpoint, or escape a retry storm that independent controllers keep probing back into. Those three are this module's, at tend cadence.

**Up is gated on everything; down is gated on nothing.** A step up happens only inside a *clean window* — the sample limiter saturated (headroom nobody is using is not capacity), zero scale-downs, no new sample errors, HTTP retries below a surge, CPU under the gate, spacing since the last step, the sandbox budget holding, no hold in force. A cut happens the moment pushback is *sustained* — two consecutive windows, because a single episode is the controllers' own job — and ignores holds entirely, since the cut exists precisely for when nobody is watching. That asymmetry is the ratchet the design promises (workflow.md §10.5), expressed as code rather than a comment.

**Pure, for the same reason `reconcile` is.** Signals in, moves out; no clock beyond the `now` it is handed, no filesystem, no sockets. Every gate is a table a test can drive without a live worker — which matters doubly here, because the one signal a test cannot manufacture is a real provider's 429 (`mockllm` never sends one), so the ramp-down path lives or dies by these tables.

**Every move carries its sentence.** The reason lands in the eval log through the control channel's provenance, in the journal as a ramp `action`, and in the history section an attending agent's next `collect` marks as new — which is the whole notification story: tend narrates, the agent reads the narration and holds the ramp (`steward ramp hold`) if it dislikes what it sees.

See scheduling.md §3.4–3.5 and workflow.md §10 (the tuning loop, as rewritten by step 21).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from .._worker import LiveTask
from .._workspace import ACTION, OBSERVATION, JournalEvent, RampHold

RAMP_STEP = 20
"""How much one clean window buys.

Fixed rather than proportional: the default range climbs 40→200 in eight steps, each one small enough that the drain back out of it is minutes rather than hours (the ratchet: lowering a limit never preempts, it waits for holders to finish).
"""

STEP_SPACING = 20 * 60.0
"""Seconds between one task's steps, whatever the tend cadence.

A floor rather than a multiple of the interval, because the measurement needs the time more than the loop does: a +20 batch has sandboxes to start — a helm install is not instant — and a window read before the new level is exercised would measure startup, call it clean, and step again on top of it.
"""

CPU_GATE = 0.75
"""Fraction of one core above which a worker's window blocks its step.

Per worker process, measured over the inter-tend window (the delta of cumulative CPU seconds two observations apart). A python event loop saturates around one core, so a worker regularly near this is throttled by the host rather than the provider — and more samples would queue on a CPU, which no amount of provider headroom pays for.
"""

RETRY_GATE = 0.25
"""HTTP retries per sample slot over a window, above which a step is blocked.

A rate rather than a count, because what counts as a surge scales with how many samples are making calls. Not *zero*, deliberately: HTTP retries include transient 5xxs and connection resets that any long-running endpoint produces, so a zero-tolerance gate would park the ramp permanently against a provider that is merely imperfect. A quarter of a retry per slot is well clear of that noise and well under the one-per-slot that would let a materially unhealthy window read as clean — rate-limit pushback proper is the controllers' scale-downs, which this sits beside rather than duplicates.
"""

CONNECTIONS_FLOOR = 20
"""The lowest a storm cut may set the connection ceiling.

The adaptive controllers' own starting level: a ceiling below where a fresh controller begins is not throttling, it is strangling, and the point of the cut is to stop the climb-back rather than to stop the run.
"""

WINDOW_FALLBACK = 600.0
"""Seconds of history to treat as the window when no previous observation exists.

Only the connections ceiling consults it — a fresh worker's first tend raises the ceiling to the ramp target without waiting a window, because until then the default bound (100) silently caps a climb the range authorized. Every sample-level gate simply waits for a real baseline instead.
"""


@dataclass(frozen=True)
class TaskSignals:
    """One running task's window, as the policy consumes it."""

    identifier: str

    key: str
    """Display key, because every sentence this module writes is read by a person."""

    task_id: str
    """The control channel's selector, carried so a move needs no second lookup."""

    pid: int

    level: int | None
    """The sample-concurrency setpoint as read live, or `None` where it is not retunable."""

    in_use: int
    """How much of the setpoint is held right now. `in_use == level` is saturation — the demand half of the up-gate."""

    errored: int
    """Samples errored so far this attempt, cumulative."""

    http_retries: int
    """HTTP retries so far this attempt, cumulative and live-only."""

    scale_downs: tuple[float, ...] = ()
    """When this task's controllers cut, as unix timestamps."""

    sandboxes: tuple[int, int] | None = None
    """The process's sandbox limiter as (in use, limit), or `None` where none is in effect."""

    connections_ceiling: int | None = None
    """The adaptive controllers' scaling ceiling, or `None` where none is adaptive."""

    connections_limit: int | None = None
    """The highest limit any of this row's controllers currently holds. What a storm cut clamps the ceiling to."""


def signals(key: str, live: LiveTask) -> TaskSignals:
    """One live row as the policy's input.

    Args:
        key: The task's display key.
        live: What the worker reported.

    Returns:
        The window's signals.
    """
    in_use, level = live.max_samples if live.max_samples is not None else (0, None)
    return TaskSignals(
        identifier=live.identifier,
        key=key,
        task_id=live.task_id,
        pid=live.pid,
        level=level,
        in_use=in_use,
        errored=live.samples.errored,
        http_retries=live.http_retries,
        scale_downs=live.scale_downs,
        sandboxes=live.sandboxes,
        connections_ceiling=live.connections_ceiling,
        connections_limit=live.connections_limit,
    )


@dataclass(frozen=True)
class Baseline:
    """What the previous turn recorded, which is what makes this turn a window.

    All of it optional, because the first turn of a fleet has none — and *no baseline* reads as *not known to be clean*, never as clean. The one consumer that proceeds without one is the connection ceiling's first raise, argued at `WINDOW_FALLBACK`.
    """

    ts: float | None = None
    """When the previous observation was written, unix. The window's left edge."""

    levels: Mapping[str, int] = field(default_factory=dict[str, int])
    """Each task's setpoint as the previous turn left it — the stability half of the gate."""

    cpu: Mapping[int, float] = field(default_factory=dict[int, float])
    """Cumulative CPU seconds per pid at the window's left edge."""

    retries: Mapping[str, int] = field(default_factory=dict[str, int])
    errors: Mapping[str, int] = field(default_factory=dict[str, int])

    pushback: frozenset[str] = frozenset()
    """Tasks whose previous window already had scale-downs. Pushback in two consecutive windows is a storm; in one, it is the controllers doing their job."""

    capacity: frozenset[str] = frozenset()
    """Tasks whose previous window was already clean at a bound. Capacity in two consecutive windows is a proposal; in one, it is a good ten minutes."""


@dataclass(frozen=True)
class Move:
    """One retune to make, and the sentence that justifies it everywhere it is recorded."""

    identifier: str
    key: str
    task_id: str

    knob: str
    """`max_samples` or `max_connections`."""

    to: int

    at: int | None
    """Where the knob was when the decision was made, for the record's `from`."""

    reason: str


@dataclass(frozen=True)
class Proposal:
    """Capacity somebody chose not to authorize, surfaced to the one who could.

    Two shapes with one meaning — *the binding constraint is yours*. A pinned setpoint showing a clean, saturated window for two turns running; a ramp at its ceiling with pushback still absent. In both, tend has no authority left to spend, so the item's owner is the human whose number binds (scheduling.md §3.3).
    """

    identifier: str
    key: str

    level: int
    """Where the task is held, which is what the proposal proposes leaving."""

    ceiling: int | None
    """The ramp ceiling that binds, or `None` where the bound is a pinned setpoint."""

    @property
    def pinned(self) -> bool:
        return self.ceiling is None


@dataclass(frozen=True)
class TuningPlan:
    """What this turn's window supports: the moves, the proposals, and the account."""

    active: bool = False
    """Whether a ramp is configured at all. `False` is pinned mode: no moves, proposals only."""

    range: tuple[int, int] | None = None

    moves: list[Move] = field(default_factory=list[Move])
    """To execute in order. A `status` computes these and throws them away, exactly as it does `reconcile`'s actions."""

    proposals: list[Proposal] = field(default_factory=list[Proposal])

    record: dict[str, Any] = field(default_factory=dict[str, Any])
    """The observation's `tuning` payload — next turn's `Baseline`, before this turn's applied moves are overlaid (see `observation_payload`)."""

    lines: list[str] = field(default_factory=list[str])
    """The tuning block, one source for both renderers."""


def plan_tuning(
    tasks: Sequence[TaskSignals],
    *,
    ramp: tuple[int, int] | None,
    budget: int | None,
    baseline: Baseline,
    holds: Mapping[str, RampHold],
    last_step: Mapping[str, float],
    cpu: Mapping[int, float],
    now: float,
    absent: Collection[str] = (),
) -> TuningPlan:
    """Decide this turn's retunes.

    Args:
        tasks: The running tasks whose workers answered, in table order.
        ramp: The authorized range, or `None` where an explicit `max_samples` pinned the setpoint (`resolve_samples_ramp`).
        budget: The definition's `max_sandboxes`, or `None` to fall back to the limit the workers themselves report — the provider default, which is a statement about the host.
        baseline: What the previous turn recorded.
        holds: The holds in force, keyed by identifier with `""` for the fleet's.
        last_step: When each task's setpoint last moved, by identifier, unix.
        cpu: Cumulative CPU seconds per pid, read this turn.
        now: The turn's clock, unix — passed in so the policy stays pure.
        absent: Running tasks whose worker did not answer, which have no window this turn but are still holding whatever the last turn left them at. Charged against the sandbox budget at that level, because a worker too busy to serve its socket is running samples exactly as hard as one that answered — and *busy* is the ordinary state of a fleet mid-generate, not a failure. Charged whether or not it was sandboxed, since the reading that would say is the one that did not arrive: the error is then a step declined, which costs a tend, rather than a host over-committed, which costs the run.

    Returns:
        The plan. Empty moves under a pinned setpoint, always.
    """
    edge = baseline.ts if baseline.ts is not None else now - WINDOW_FALLBACK
    pushback = {
        task.identifier for task in tasks if any(at >= edge for at in task.scale_downs)
    }
    storms = pushback & baseline.pushback

    moves: list[Move] = []
    lines: list[str] = []
    capacity: set[str] = set()
    proposals: list[Proposal] = []

    if ramp is not None:
        held = holds.get("")
        if held is not None:
            who = held.by or "somebody"
            lines.append(
                f"held by {who}"
                + (f" — {held.reason}" if held.reason else "")
                + " · `steward ramp resume` re-arms it"
            )
        moves.extend(_ceilings(tasks, ramp, pushback, storms, holds))

    sandboxed = _budget(tasks, budget)
    unmeasured = sum(
        baseline.levels.get(identifier, ramp[0] if ramp is not None else 0)
        for identifier in absent
    )
    committed = unmeasured + sum(
        task.level
        for task in tasks
        if task.sandboxes is not None and task.level is not None
    )

    for task in tasks:
        blocked = _window(task, baseline, cpu, now)
        storm = task.identifier in storms
        held = "" in holds or task.identifier in holds

        if ramp is None:
            # pinned mode: the signal still runs, the authority does not
            if blocked is None and not held:
                capacity.add(task.identifier)
                if task.identifier in baseline.capacity and task.level is not None:
                    proposals.append(
                        Proposal(
                            identifier=task.identifier,
                            key=task.key,
                            level=task.level,
                            ceiling=None,
                        )
                    )
            continue

        floor, ceiling = ramp
        if storm and task.level is not None and task.level > floor:
            to = max(floor, task.level - RAMP_STEP)
            moves.append(
                Move(
                    identifier=task.identifier,
                    key=task.key,
                    task_id=task.task_id,
                    knob="max_samples",
                    at=task.level,
                    to=to,
                    reason=(
                        "sustained rate-limit pushback; stepping sample "
                        "concurrency down so new samples stop being admitted "
                        "against connection capacity that is not there"
                    ),
                )
            )
            lines.append(f"{task.key}: pushback — stepping {task.level}→{to}")
            continue
        if storm:
            lines.append(f"{task.key}: pushback — holding at the floor")
            continue

        if task.level is not None and task.level > ceiling:
            # the range was narrowed under a running worker. Not a step and so
            # not gated like one -- no window, no spacing, no hold -- because
            # the envelope is the authorization itself, and a level outside it
            # is not capacity being declined but authority already exceeded.
            # Reducing admits no new samples; the ones running drain as usual
            moves.append(
                Move(
                    identifier=task.identifier,
                    key=task.key,
                    task_id=task.task_id,
                    knob="max_samples",
                    at=task.level,
                    to=ceiling,
                    reason=(
                        f"the authorized range now ends at {ceiling}; bringing "
                        f"sample concurrency back inside it"
                    ),
                )
            )
            lines.append(
                f"{task.key}: above the range — stepping {task.level}→{ceiling}"
            )
            continue

        if task.level is not None and task.level >= ceiling:
            if blocked is None and not held:
                capacity.add(task.identifier)
                if task.identifier in baseline.capacity:
                    proposals.append(
                        Proposal(
                            identifier=task.identifier,
                            key=task.key,
                            level=task.level,
                            ceiling=ceiling,
                        )
                    )
            lines.append(f"{task.key}: {task.level} — at the ceiling")
            continue

        if held:
            if task.level is not None:
                lines.append(f"{task.key}: {task.level} — held")
            continue
        if blocked is not None:
            if task.level is not None:
                lines.append(f"{task.key}: {task.level} — {blocked}")
            continue
        assert task.level is not None, "a clean window requires a readable level"
        if now - last_step.get(task.identifier, 0.0) < STEP_SPACING:
            lines.append(
                f"{task.key}: {task.level} — stepped recently, letting it settle"
            )
            continue
        to = min(ceiling, task.level + RAMP_STEP)
        if task.sandboxes is not None and sandboxed is not None:
            # what this step actually costs, which is short of a whole one for
            # a task finishing its climb -- charging the full step there would
            # refuse a move that fits
            asked = to - task.level
            if sandboxed - committed < asked:
                # the unread share is named rather than folded in, because a
                # budget that binds on workers nobody could read this turn is
                # the one case where the number does not add up on the page
                unread = (
                    f", {unmeasured} of it on workers that did not answer"
                    if unmeasured
                    else ""
                )
                lines.append(
                    f"{task.key}: {task.level} — at the sandbox budget "
                    f"({committed}/{sandboxed}{unread})"
                )
                continue
            committed += asked

        moves.append(
            Move(
                identifier=task.identifier,
                key=task.key,
                task_id=task.task_id,
                knob="max_samples",
                at=task.level,
                to=to,
                reason=(
                    f"no rate-limit pushback with the limiter saturated; ramping "
                    f"within the authorized range {floor}–{ceiling}"
                ),
            )
        )
        lines.append(f"{task.key}: {task.level}→{to} — clean window, saturated")

    if ramp is not None and not lines:
        lines.append("nothing running to tune")
    if ramp is not None:
        floor, ceiling = ramp
        lines.insert(0, f"samples ramp {floor}–{ceiling}")
        lines.append("`steward ramp hold` pauses climbing; safety cuts stay active")

    return TuningPlan(
        active=ramp is not None,
        range=ramp,
        moves=moves,
        proposals=proposals,
        record={
            "levels": {
                task.identifier: task.level for task in tasks if task.level is not None
            },
            "cpu": {str(pid): seconds for pid, seconds in cpu.items()},
            "retries": {task.identifier: task.http_retries for task in tasks},
            "errors": {task.identifier: task.errored for task in tasks},
            "pushback": sorted(pushback),
            "capacity": sorted(capacity),
        },
        lines=lines,
    )


def _window(
    task: TaskSignals, baseline: Baseline, cpu: Mapping[int, float], now: float
) -> str | None:
    """Why this task's window is not clean, or `None` where it is.

    Every early return is a gate, every gate is a sentence a reader sees in the tuning block, and *unknown* always reads as *not clean* — a window with no baseline, a counter that went backwards (the worker respawned), a pid the CPU read missed, are all windows that measured nothing, and a step is only ever bought with a measurement.
    """
    if task.level is None:
        return "sample concurrency is not retunable here"
    if baseline.ts is None or baseline.ts >= now:
        return "waiting for a first full window"
    recorded = baseline.levels.get(task.identifier)
    if recorded is None:
        # this task's first appearance in a record: nothing to be a delta against
        return "waiting for a first full window"
    if recorded != task.level:
        return "the level moved this window"
    if task.in_use < task.level:
        return f"not saturated ({task.in_use}/{task.level} in use)"
    if any(at >= baseline.ts for at in task.scale_downs):
        return "rate-limit pushback this window"

    retries = baseline.retries.get(task.identifier)
    if retries is None or task.http_retries < retries:
        return "waiting for a first full window"
    if (surge := task.http_retries - retries) > task.level * RETRY_GATE:
        return f"HTTP retries surging ({surge} this window)"

    errors = baseline.errors.get(task.identifier)
    if errors is None or task.errored < errors:
        return "waiting for a first full window"
    if task.errored > errors:
        return "new sample errors this window"

    spent = cpu.get(task.pid)
    was = baseline.cpu.get(task.pid)
    if spent is None or was is None or spent < was:
        return "no CPU baseline for this worker yet"
    utilization = (spent - was) / (now - baseline.ts)
    if utilization >= CPU_GATE:
        return f"CPU at {utilization:.0%} of a core"
    return None


def _ceilings(
    tasks: Sequence[TaskSignals],
    ramp: tuple[int, int],
    pushback: Collection[str],
    storms: Collection[str],
    holds: Mapping[str, RampHold],
) -> list[Move]:
    """The connection-ceiling moves, one per process.

    The asymmetry lives here. A storm cuts the ceiling to where the controllers already fell — at once, holds notwithstanding, because backoffs stop within seconds of the ceiling landing and the climb-back that would restart them cannot happen. A clear window raises it by at most doubling toward the ramp ceiling, so the way back up is stepwise where the way down was one move. The first raise is also how a fresh worker's default bound (100) gets out of the way of a range that authorized more.

    Per process rather than per task, because the knob is process-scoped: one row is elected per pid, a packed process storms if any of its rows do, and its ceiling reads as the highest any row reports. **A hold on any of a process's rows holds the whole process's ceiling**, for the same reason — the knob cannot be raised for one task and not its sibling, so the only reading that keeps `ramp hold <identifier>` honest is the conservative one.
    """
    _, target = ramp
    moves: list[Move] = []
    seen: set[int] = set()
    for task in tasks:
        if task.pid in seen or task.connections_ceiling is None:
            continue
        seen.add(task.pid)
        siblings = [entry for entry in tasks if entry.pid == task.pid]
        ceiling = max(
            entry.connections_ceiling
            for entry in siblings
            if entry.connections_ceiling is not None
        )
        held = "" in holds or any(entry.identifier in holds for entry in siblings)
        if any(entry.identifier in storms for entry in siblings):
            # the highest any controller currently holds, never their sum: the
            # ceiling this writes is worn by each of them alike, so summing
            # would bound every controller at what all of them together were
            # using -- a cut that is really a raise
            level = max((entry.connections_limit or 0 for entry in siblings), default=0)
            to = max(level, CONNECTIONS_FLOOR)
            if to < ceiling:
                moves.append(
                    Move(
                        identifier=task.identifier,
                        key=task.key,
                        task_id=task.task_id,
                        knob="max_connections",
                        at=ceiling,
                        to=to,
                        reason=(
                            "sustained rate-limit pushback across consecutive "
                            "windows; clamping the connection ceiling to stop "
                            "the retry storm"
                        ),
                    )
                )
        elif (
            ceiling < target
            and not held
            and not any(entry.identifier in pushback for entry in siblings)
        ):
            moves.append(
                Move(
                    identifier=task.identifier,
                    key=task.key,
                    task_id=task.task_id,
                    knob="max_connections",
                    at=ceiling,
                    to=min(target, ceiling * 2),
                    reason=f"no pushback; raising the connection ceiling toward {target}",
                )
            )
    return moves


def _budget(tasks: Sequence[TaskSignals], declared: int | None) -> int | None:
    """The machine-wide sandbox budget, or `None` where nothing is host-bound.

    The definition's `max_sandboxes` where it set one; otherwise the limit the workers themselves report, which is the provider's default — and that default's shape (`2 × cores` under Docker) is a statement about what one *host* supports, applied per process. Reading it back as the machine's budget is exactly the correction scheduling.md §3.6 asks for, enforced here as a cap on the fleet-wide sum of setpoints rather than divided once at spawn: a task's containers never exceed its running samples, so bounding the sum bounds the containers.
    """
    if declared is not None:
        return declared
    limits = [task.sandboxes[1] for task in tasks if task.sandboxes is not None]
    return max(limits) if limits else None


def observation_payload(plan: TuningPlan, applied: Sequence[Move]) -> dict[str, Any]:
    """The `tuning` payload an observation records, after this turn's moves.

    The levels are overlaid with the setpoint moves that actually landed, so the record says what the turn left behind rather than what it read on the way in — the same rule `_record` follows for everything else. A move that failed is simply not in `applied`, and the stale level self-corrects on the next live read.

    Args:
        plan: This turn's plan.
        applied: The moves that were executed and accepted.

    Returns:
        The payload for the observation's `tuning` key.
    """
    payload = dict(plan.record)
    levels = dict(cast(dict[str, int], payload.get("levels", {})))
    for move in applied:
        if move.knob == "max_samples":
            levels[move.identifier] = move.to
    payload["levels"] = levels
    return payload


def read_baseline(events: list[JournalEvent]) -> Baseline:
    """Fold the journal down to the previous turn's window edge.

    The most recent observation that carried a `tuning` payload, because that is the most recent turn whose reading this one can be a delta against. An observation without one — an older version's, or a turn with nothing live to read — contributes nothing, and the gates treat the missing baseline as *not clean* rather than reaching further back for a window that spans hours.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The baseline, empty where no turn has recorded one.
    """
    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        tuning = event.payload.get("tuning")
        if not isinstance(tuning, dict):
            continue
        recorded = cast(dict[str, Any], tuning)
        return Baseline(
            ts=_unix(event.ts),
            levels=_ints(recorded.get("levels")),
            cpu=_cpu(recorded.get("cpu")),
            retries=_ints(recorded.get("retries")),
            errors=_ints(recorded.get("errors")),
            pushback=frozenset(_strings(recorded.get("pushback"))),
            capacity=frozenset(_strings(recorded.get("capacity"))),
        )
    return Baseline()


def read_ramp_record(
    events: list[JournalEvent],
) -> tuple[dict[str, int], dict[str, float]]:
    """Fold the journal down to where the ramp has been.

    Two answers from one walk of the same entries. The **levels** are each task's last recorded setpoint move — what a respawned worker should start at, so a crash does not send a climbed task back to the floor (`reconcile`'s `levels`). The **last steps** are when each moved, which is what `STEP_SPACING` measures from.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        Levels and last-step times, both by identifier.
    """
    levels: dict[str, int] = {}
    steps: dict[str, float] = {}
    for event in events:
        if event.type != ACTION or event.payload.get("action") != "ramp":
            continue
        if event.payload.get("knob") != "max_samples":
            continue
        identifier = event.payload.get("identifier")
        to = event.payload.get("to")
        if not isinstance(identifier, str) or not identifier:
            continue
        if isinstance(to, int) and not isinstance(to, bool) and to > 0:
            levels[identifier] = to
        if (when := _unix(event.ts)) is not None:
            steps[identifier] = when
    return levels, steps


def _unix(ts: str) -> float | None:
    """A journal timestamp as unix seconds, or `None` where it will not parse."""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _ints(value: object) -> dict[str, int]:
    """A string-to-int mapping from a journal payload, which may hold anything."""
    if not isinstance(value, dict):
        return {}
    return {
        key: entry
        for key, entry in cast(dict[str, object], value).items()
        if isinstance(entry, int) and not isinstance(entry, bool)
    }


def _cpu(value: object) -> dict[int, float]:
    """The per-pid CPU map, whose keys JSON stored as strings."""
    if not isinstance(value, dict):
        return {}
    seconds: dict[int, float] = {}
    for key, entry in cast(dict[str, object], value).items():
        if not key.isdigit():
            continue
        if isinstance(entry, int | float) and not isinstance(entry, bool):
            seconds[int(key)] = float(entry)
    return seconds


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in cast(list[object], value) if isinstance(entry, str)]
