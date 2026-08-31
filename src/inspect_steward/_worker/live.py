"""Reading a running fleet, fast enough to do it every time somebody looks.

`ctl.py` reaches the control channel by running `inspect ctl`, and its reasoning holds for everything it does: the CLI already draws *alive but busy* apart from *gone*, retries reads, never retries mutations, and reports failures in a closed vocabulary. Reimplementing that would mean copying four constants and then drifting from them.

**This module revisits that decision for reads only, and the reason is a number.** A `status` table wants three things per worker — sample counts, per-sample limit usage, and the model connection pool — and only the first two span the fleet in one CLI call. `inspect ctl config` resolves a *single* task, so the third is one invocation per worker, and an invocation costs ~1.6s almost entirely in `import inspect_ai`. A fleet of ten is about nineteen seconds. The same three reads over the worker's own socket are about six milliseconds each and run concurrently across the fleet.

Nineteen seconds is not a slow table, it is a table nobody runs, and `status` exists to be run often. So reads come here and **every mutation stays in `ctl.py`** — which is also where the risk is, since a retune that half-lands is a real problem and a status column that is missing for one turn is not.

**Concurrent across the fleet and within a worker, which stopped being the same statement once a run could be packed.** A worker holding one task is three requests; a worker holding five hundred is three plus five hundred, and awaiting those in turn rebuilds the serial read this module exists to replace — inside a single process, where it is not even bounded by `timeout`, since that bounds each request rather than the chain. So `_read` gathers its per-eval reads.

**The retry policy is deliberately the opposite of the CLI's, for the same reason.** The control server shares the eval's event loop, so a busy eval can stall a response for seconds. The CLI waits, because a retune has to land. A table must not: a worker that does not answer promptly is reported as busy and the row is rendered without its live columns. Waiting would make the common case — a fleet mid-generate — the slow one.

Nothing here raises. A worker that has gone, wedged, or never bound a socket costs its own row's live columns and nothing else.
"""

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import cast

import httpx

TIMEOUT = 2.0
"""Seconds to wait for one worker's reads.

Short on purpose, and the one number that encodes this module's whole difference from `ctl.py`. A control server sharing a busy event loop can take seconds to answer; the CLI waits it out because a mutation must land. Here the caller is drawing a table, where a row missing its live columns for one turn costs nothing and a ten-second pause costs the command's reason for existing.
"""


@dataclass(frozen=True)
class LiveSamples:
    """A running task's sample counts, as its own process reports them."""

    total: int = 0
    completed: int = 0
    errored: int = 0
    cancelled: int = 0
    in_flight: int = 0
    queued: int = 0


@dataclass(frozen=True)
class LiveUsage:
    """How far a typical running sample has got against each per-sample budget.

    The **median** across running samples, which answers *where is this run in its budget* — the question a reader deciding whether a limit is set right is actually asking. The maximum answers *is anything about to be cut off*, and one sample near its ceiling is the normal shape of a healthy run: some samples are hard. A column that reads `199/200` whenever a single outlier is near the top says nothing about the other ninety-nine.

    A sample that started a minute ago pulls the median down, and should: it is a running sample and this is what the running samples look like.
    """

    turns: int = 0
    messages: int = 0
    tokens: int = 0
    """Metered against the *token limit* rather than summed, because the two differ whenever a limit meters something narrower than the total — `output`, or a formula over input and output. The worker computes it (`token_limit_usage`), which is the only place the metering rule is known; total tokens stand in where no limit is configured and there is therefore nothing to meter against."""

    seconds: float = 0.0

    token_limit: int | None = None
    """The token ceiling the running samples are actually under, or `None` where they report none.

    A limit rather than a usage, here because it comes off the same rows and is only meaningful beside `tokens`. **The log header's `token_limit` is what the task was launched with**; this is what its samples hold now, which differs after `inspect ctl config` retunes one mid-run. Reading it from the samples is also what makes a formula limit legible, since the ceiling and the metering rule have to agree and only the worker knows both.
    """


@dataclass(frozen=True)
class LiveConnections:
    """The model connection pool, which is the fleet's real throughput knob."""

    in_use: int = 0
    limit: int | None = None


@dataclass(frozen=True)
class LiveParked:
    """Samples in this task waiting on a person, and what they are waiting for.

    A detached worker has no terminal, so an approval or an `ask_user` question routes to the ACP server the worker binds and waits there — indefinitely, holding its slot, its sandbox and its model connections. Nothing else Steward reads says this: the sample is `running`, its transcript shows a pending tool call, and it will still show one tomorrow morning.
    """

    approvals: int = 0
    questions: int = 0

    functions: tuple[str, ...] = ()
    """The tool functions awaiting approval, sorted and deduplicated.

    The **name only**. A request's arguments and an `ask_user` prompt are model-generated text, and Steward's summary is relayed verbatim by an agent that then acts on it; a function name is structural, so it is the part that can be repeated without carrying a model's words into an instruction.
    """

    @property
    def total(self) -> int:
        return self.approvals + self.questions


DEFAULT_STUCK_AFTER = 5 * 60 * 60
"""Seconds a running sample may go without activity before it reads as stuck.

Five hours, because the threshold is a *reporting* one and the cost of tripping early is a person paged about a slow sandbox: a long tool call is the ordinary shape of agentic work, and `last_activity_at` already advances per streamed model chunk, so what crosses this is genuinely silent. `stuck_after` in `_steward.yaml` overrides it.
"""


@dataclass(frozen=True)
class StuckSample:
    """One running sample that has stopped moving, and what it is stopped inside."""

    sample_id: str
    epoch: int

    idle: float
    """Seconds since the sample's last recorded activity."""

    function: str = ""
    """The pending tool function, or empty where the wait is not a tool call — a silent non-streaming generate, or no activity at all."""

    call_id: str = ""
    """The pending call `sample cancel-tool-call` targets, or empty."""

    cancel_requested: bool = False
    """Whether a cancel has already been asked of this call and not been heeded — rung 1 already spent, which is what flips the ladder to rung 2."""


@dataclass(frozen=True)
class LiveStuck:
    """Samples in this task that have stopped moving past the threshold.

    Not parked, not failed — a `bash` that never returns, a connection held open silently. A parked sample is excluded (the human branch of the activity classification leads upstream, precisely so this reading can tell them apart), and so is a `retry_wait` whose deadline is still ahead: waiting out a backoff is progress of a kind.
    """

    count: int = 0
    """Stuck samples, not pending calls — a sample wedged on two calls is one stuck sample."""

    oldest_idle: float = 0.0
    """Seconds since the longest-stuck sample last moved."""

    samples: tuple[StuckSample, ...] = ()
    """One entry per pending tool call, plus one call-less entry per non-tool stuck sample."""

    asked: bool = False
    """Whether every stuck sample is a pending tool call that has already been asked to cancel — the delivered-but-unheeded state, which escalates the ladder. `False` while anything stuck was never askable."""


@dataclass(frozen=True)
class LiveTask:
    """What one worker says about itself right now."""

    pid: int
    identifier: str
    task_id: str = ""
    samples: LiveSamples = field(default_factory=LiveSamples)
    usage: LiveUsage = field(default_factory=LiveUsage)
    connections: LiveConnections = field(default_factory=LiveConnections)
    parked: LiveParked = field(default_factory=LiveParked)
    stuck: LiveStuck = field(default_factory=LiveStuck)
    total_tokens: int = 0

    max_samples: tuple[int, int] | None = None
    """The sample-concurrency knob as (in use, limit), or `None` where it is not retunable.

    `in_use == limit` is the saturation the tuning loop's up-gate requires: a limiter with headroom says demand does not exist, and raising it would misreport as discovered capacity what was never asked for.
    """

    sandboxes: tuple[int, int] | None = None
    """The process's sandbox limiters as (in use, limit), summed across providers, or `None` where no sandbox limit is in effect.

    The *effective* limit — the definition's `max_sandboxes` where it set one, the provider's own default where it did not — which is what makes the fleet-wide budget readable without manifest archaeology. An elastic provider registers no limiter and reads `None` here, which caps nothing.
    """

    scale_downs: tuple[float, ...] = ()
    """When the adaptive connection controllers last cut, as unix timestamps, newest last.

    The pushback signal: a rate-limit episode leaves a multiplicative cut in each controller's recent history, so "any scale-downs since my last turn" is a comparison rather than an inference. Bounded upstream to the last few changes per controller — enough to detect presence in a window, not to count a storm.
    """

    connections_ceiling: int | None = None
    """The adaptive controllers' scaling ceiling (their `max`), or `None` where none is adaptive.

    Distinct from `connections.limit`, which is where the controllers currently *are*: the ceiling is how far they may climb, and it is the knob the tuning loop moves — down at once to exit a retry storm, back up stepwise as the pushback clears. The maximum across this row's controllers, since a retune sets them all.
    """

    connections_limit: int | None = None
    """The highest limit any of this row's controllers currently holds, or `None` where none is adaptive.

    Where a storm cut clamps the ceiling to, which is why it is a maximum and **not** `connections.limit`'s sum. The two answer different questions: the sum is how many connections the row may have open across its models, which is what a person reading the live block wants; the ceiling is a single number applied to every controller alike, so the sum would set each one's bound to what all of them together were using — a cut that is really a raise.
    """

    refusals: int = 0
    """Model refusals this eval's samples have hit."""

    http_retries: int = 0
    """HTTP retries this eval's samples have made.

    With `refusals`, the pair that says whether a run is *slow* or *in trouble*, and both are **live-only**: inspect tallies them per task on the control channel and records neither in an eval log, so nothing can report them for a task that has finished. Any total built from these describes what is running at this instant and falls as tasks complete — which has to be said wherever it is rendered, because a falling number otherwise reads as a problem fixing itself (agent.md §4.2).
    """

    unavailable: str | None = None
    """Why this worker could not be read — `busy`, `gone`, or a reason. `None` when the reading worked."""


@dataclass(frozen=True)
class LiveFleet:
    """Every worker that answered, keyed by the identifier it is running."""

    tasks: dict[str, LiveTask] = field(default_factory=dict[str, LiveTask])

    @property
    def unavailable(self) -> list[LiveTask]:
        return [task for task in self.tasks.values() if task.unavailable is not None]


@dataclass(frozen=True)
class LiveTarget:
    """One worker to read: what it is running, its pid, and the socket it bound."""

    identifiers: tuple[str, ...]
    pid: int
    socket: Path

    @property
    def only(self) -> str | None:
        """The one task this worker is running, or `None` where it is running several.

        What lets a worker at the default width skip correlation entirely: with one task there is nothing to tell apart, so a row is that task whatever it says its log is — which matters in the window where a task has begun but its log has not appeared in anyone's observation yet.
        """
        return self.identifiers[0] if len(self.identifiers) == 1 else None


def read_fleet(
    targets: list[LiveTarget],
    locations: Mapping[str, str],
    *,
    timeout: float = TIMEOUT,
    stuck_after: float = DEFAULT_STUCK_AFTER,
) -> LiveFleet:
    """Read every worker concurrently.

    Args:
        targets: Running workers with a discovered socket. An empty list is an empty fleet and costs nothing — which is the common shape late in a campaign, when everything has finished and there is nothing live to ask.
        locations: Log location to task identifier, for naming the rows a packed worker reports. Required rather than defaulted, and positional rather than keyword, because a caller that omits it is only ever wrong: at the default width it is unread, and at any other width its absence silently costs every row its identity and reports a running task as finished. An empty mapping says *nothing to correlate* out loud.
        timeout: Seconds to wait per worker.
        stuck_after: Seconds a running sample may go without activity before it reads as stuck.

    Returns:
        What each worker reported, including the ones that could not be reached.
    """
    if not targets:
        return LiveFleet()
    return asyncio.run(
        read_fleet_async(targets, locations, timeout=timeout, stuck_after=stuck_after)
    )


async def read_fleet_async(
    targets: list[LiveTarget],
    locations: Mapping[str, str],
    *,
    timeout: float = TIMEOUT,
    stuck_after: float = DEFAULT_STUCK_AFTER,
) -> LiveFleet:
    """Read every worker concurrently.

    Args:
        targets: Running workers with a discovered socket.
        locations: Log location to task identifier, for naming the rows a packed worker reports.
        timeout: Seconds to wait per worker.
        stuck_after: Seconds a running sample may go without activity before it reads as stuck.

    Returns:
        What each worker reported.
    """
    if not targets:
        return LiveFleet()
    # one clock reading for the whole fleet, so two workers' idle times are
    # measured against the same instant rather than drifting with read order
    now = time.time()
    read = await asyncio.gather(
        *(_read(target, timeout, locations, stuck_after, now) for target in targets),
    )
    return LiveFleet(tasks={task.identifier: task for tasks in read for task in tasks})


async def _read(
    target: LiveTarget,
    timeout: float,
    locations: Mapping[str, str],
    stuck_after: float = DEFAULT_STUCK_AFTER,
    now: float = 0.0,
) -> list[LiveTask]:
    """One worker's reads, or why they did not happen.

    One entry per task the worker is running, because a packed process reports a row for each. A task the worker has already finished while its siblings run has no row and reads `finished` — which is true of it, and is exactly what the same word meant when a worker only ever held one.
    """

    def unavailable(reason: str) -> list[LiveTask]:
        return [
            LiveTask(pid=target.pid, identifier=identifier, unavailable=reason)
            for identifier in target.identifiers
        ]

    try:
        transport = httpx.AsyncHTTPTransport(uds=str(target.socket))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=timeout
        ) as client:
            rows = _task_rows(await _get(client, "/tasks"))
            if not rows:
                # the socket answered but claims no task: the eval finished
                # between the scan and now, which the next turn will reap
                return unavailable("finished")
            # one read per distinct eval and one per task, all at once. Awaiting
            # them in turn would make a packed worker's latency its batch size --
            # a five-hundred-task process is five hundred round trips, which is
            # the serial fleet read this module exists to avoid, rebuilt
            # inside one worker. `timeout` bounds each request, so a serial
            # chain is not bounded by it either.
            #
            # the config read is task-scoped rather than the process's `/config`,
            # because the task envelope is a superset: the same adaptive
            # controllers, plus the `max_samples` knob and the sandbox limiters
            # the tuning loop reads. At the default width it is the same number
            # of requests
            evals = sorted({_text(row.get("eval_id")) for row in rows})
            # only rows that name a task: a row without one is a shape this
            # version has not seen, and it costs its own columns rather than
            # a request to a path that cannot exist
            tasks = sorted(
                {task_id for row in rows if (task_id := _text(row.get("task_id")))}
            )
            read = await asyncio.gather(
                # `all=true`, because the endpoint's default caps the listing
                # at 100 rows -- and at the 200-300 sample concurrency a tuned
                # task runs, the rows past the cap are exactly where a stuck
                # or parked sample would silently hide. The rows are small and
                # the read is per-tend, so the full dump costs nothing worth
                # a truncation blind spot
                *(
                    _get(client, f"/evals/{eval_id}/samples?all=true")
                    for eval_id in evals
                ),
                *(_get(client, f"/tasks/{task_id}/config") for task_id in tasks),
            )
            samples = dict(zip(evals, read[: len(evals)], strict=True))
            configs = dict(zip(tasks, read[len(evals) :], strict=True))
    except httpx.TimeoutException:
        # alive, and its event loop is busy running the eval -- which is the
        # thing it is supposed to be doing, so this is not a fault
        return unavailable("busy")
    except (httpx.HTTPError, OSError):
        return unavailable("gone")
    except Exception as ex:  # a shape this version has not seen
        return unavailable(f"{type(ex).__name__}: {ex}")

    live: list[LiveTask] = []
    claimed = frozenset(model for row in rows if (model := _text(row.get("model"))))
    for row in rows:
        identifier = _identify(row, target, locations)
        if identifier is None:
            # a row this worker is running that nothing can name. Dropped
            # rather than guessed at: the cost is one row's live columns for
            # one turn, and the alternative is attributing a task's numbers to
            # a sibling that is not the one doing the work
            continue
        config = configs.get(_text(row.get("task_id")))
        model = _model(row, target)
        live.append(
            LiveTask(
                pid=target.pid,
                identifier=identifier,
                task_id=_text(row.get("task_id")),
                samples=_samples(row.get("samples")),
                usage=_usage(samples.get(_text(row.get("eval_id")))),
                parked=_parked(samples.get(_text(row.get("eval_id")))),
                stuck=_stuck(samples.get(_text(row.get("eval_id"))), stuck_after, now),
                connections=_connections(config, model),
                max_samples=_sample_limit(config),
                sandboxes=_sandboxes(config),
                scale_downs=_scale_downs(config, model, claimed),
                connections_ceiling=_connections_ceiling(config, model),
                connections_limit=_connections_limit(config, model),
                total_tokens=_number(row.get("total_tokens")),
                refusals=_number(row.get("refusals")),
                http_retries=_number(row.get("http_retries")),
            )
        )

    named = {task.identifier for task in live}
    live.extend(
        LiveTask(pid=target.pid, identifier=identifier, unavailable="finished")
        for identifier in target.identifiers
        if identifier not in named
    )
    return live


def _identify(
    row: dict[str, object], target: LiveTarget, locations: Mapping[str, str]
) -> str | None:
    """Which of a worker's tasks this row is.

    A worker holding one task needs no correlation and gets none, which keeps the default width free of a lookup that can fail. Beyond that the log a task is writing is what names it, since that is the only field the control channel and Steward's own observation both hold.
    """
    if (only := target.only) is not None:
        return only
    location = _text(row.get("log_location"))
    identifier = locations.get(location) if location else None
    return identifier if identifier in target.identifiers else None


async def _get(client: httpx.AsyncClient, path: str) -> object:
    response = await client.get(path)
    response.raise_for_status()
    return cast(object, response.json())


def _task_rows(payload: object) -> list[dict[str, object]]:
    """The task rows for this worker.

    One per task it is running, which is one at the default width and the whole batch where a run has been packed. Read as a list of dicts rather than assumed to be either, since a process Steward did not spawn could be reached through the same discovery directory.
    """
    if not isinstance(payload, list):
        return []
    return [
        cast(dict[str, object], row)
        for row in cast(list[object], payload)
        if isinstance(row, dict)
    ]


def _samples(payload: object) -> LiveSamples:
    if not isinstance(payload, dict):
        return LiveSamples()
    counts = cast(dict[str, object], payload)
    return LiveSamples(
        total=_number(counts.get("total")),
        completed=_number(counts.get("completed")),
        errored=_number(counts.get("errored")),
        cancelled=_number(counts.get("cancelled")),
        in_flight=_number(counts.get("in_flight")),
        queued=_number(counts.get("queued")),
    )


def _running_samples(payload: object) -> list[dict[str, object]]:
    """The running sample rows in one eval's `/samples` payload.

    Walked twice — once for limit usage, once for parked samples — off a payload `_read` already fetches, so the park costs no request of its own.
    """
    if not isinstance(payload, dict):
        return []
    rows = cast(dict[str, object], payload).get("samples")
    if not isinstance(rows, list):
        return []
    samples = [
        cast(dict[str, object], entry)
        for entry in cast(list[object], rows)
        if isinstance(entry, dict)
    ]
    return [sample for sample in samples if sample.get("status") == "running"]


def _parked(payload: object) -> LiveParked:
    """Samples parked on a human decision, from each running row's `activity`.

    A pending interaction leads inspect's activity classification precisely so this reading is possible: an approval is awaited *before* its tool call is recorded, so a parked sample has no pending event of any kind and would otherwise be reported as one that has simply gone quiet.
    """
    approvals = questions = 0
    functions: set[str] = set()
    for sample in _running_samples(payload):
        entry = sample.get("activity")
        if not isinstance(entry, dict):
            continue
        activity = cast(dict[str, object], entry)
        kind = _text(activity.get("type"))
        if kind == "approval":
            approvals += 1
            if function := _text(activity.get("detail")):
                functions.add(function)
        elif kind == "question":
            questions += 1
    return LiveParked(
        approvals=approvals, questions=questions, functions=tuple(sorted(functions))
    )


def _stuck(payload: object, stuck_after: float, now: float) -> LiveStuck:
    """Samples that have stopped moving, from each running row's `last_activity_at`.

    The third walk of the rows `_read` already fetched, beside `_usage` and `_parked`, so the reading costs no request. Per running row, stuck means: not waiting on a person (`approval`/`question` — that is a park, and the human branch leads upstream precisely so the two never conflate); not inside a `retry_wait` whose deadline is still ahead (waiting out a backoff is progress); and `last_activity_at` more than `stuck_after` seconds ago. `last_activity_at` advances per streamed model chunk, so a slow-but-streaming generate never reads as idle.

    A tool row contributes one `StuckSample` per pending call — `sample cancel-tool-call` targets a call, so the calls are what the item has to be able to name. Everything else stuck contributes one call-less entry.
    """
    stuck: list[StuckSample] = []
    sample_ids: set[tuple[str, int]] = set()
    oldest = 0.0
    for sample in _running_samples(payload):
        entry = sample.get("activity")
        activity = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        kind = _text(activity.get("type"))
        if kind in ("approval", "question"):
            continue
        last = sample.get("last_activity_at")
        if not isinstance(last, int | float) or isinstance(last, bool):
            continue
        idle = now - float(last)
        if idle <= stuck_after:
            continue
        if kind == "retry_wait":
            deadline = activity.get("deadline")
            if (
                isinstance(deadline, int | float)
                and not isinstance(deadline, bool)
                and float(deadline) > now
            ):
                continue
        sample_id = str(sample.get("sample_id", ""))
        epoch = _number(sample.get("epoch")) or 1
        calls = _pending_calls(activity)
        for call in calls:
            stuck.append(
                StuckSample(
                    sample_id=sample_id,
                    epoch=epoch,
                    idle=idle,
                    function=_text(call.get("function")),
                    call_id=_text(call.get("id")),
                    cancel_requested=call.get("cancel_requested") is True,
                )
            )
        if not calls:
            stuck.append(StuckSample(sample_id=sample_id, epoch=epoch, idle=idle))
        sample_ids.add((sample_id, epoch))
        oldest = max(oldest, idle)
    asked = bool(stuck) and all(
        sample.function and sample.cancel_requested for sample in stuck
    )
    return LiveStuck(
        count=len(sample_ids),
        oldest_idle=oldest,
        samples=tuple(stuck),
        asked=asked,
    )


def _pending_calls(activity: dict[str, object]) -> list[dict[str, object]]:
    """The pending tool calls an activity carries, if any."""
    calls = activity.get("calls")
    if not isinstance(calls, list):
        return []
    return [
        cast(dict[str, object], call)
        for call in cast(list[object], calls)
        if isinstance(call, dict)
    ]


def _usage(payload: object) -> LiveUsage:
    """What the typical running sample has spent against each per-sample budget."""
    turns: list[int] = []
    messages: list[int] = []
    tokens: list[int] = []
    seconds: list[float] = []
    ceilings: list[int] = []
    for sample in _running_samples(payload):
        turns.append(_number(sample.get("turn_count")))
        messages.append(_number(sample.get("message_count")))
        # the metered figure where a limit is set, the plain total where none
        # is: `token_limit_usage` is `None` for an unlimited sample
        metered = sample.get("token_limit_usage")
        tokens.append(
            _number(metered if metered is not None else sample.get("total_tokens"))
        )
        elapsed = sample.get("total_time")
        seconds.append(float(elapsed) if isinstance(elapsed, float | int) else 0.0)
        if (ceiling := sample.get("token_limit_total")) is not None:
            ceilings.append(_number(ceiling))
    return LiveUsage(
        turns=_median(turns),
        messages=_median(messages),
        tokens=_median(tokens),
        seconds=median(seconds) if seconds else 0.0,
        # every sample of a task runs under the same ceiling, so the median is
        # picking a representative rather than reconciling a disagreement
        token_limit=_median(ceilings) if ceilings else None,
    )


def _median(values: list[int]) -> int:
    """The middle value, rounded, or zero for nothing to take a middle of."""
    return round(median(values)) if values else 0


def _model(row: dict[str, object], target: LiveTarget) -> str | None:
    """Which controllers this row's connections should be read from, if it must be narrowed.

    `None` for a worker holding one task, which is the whole process and therefore every controller in it — including the extra ones a task with model roles brings, whose total is what its pool actually is.
    """
    return None if target.only is not None else _text(row.get("model")) or None


def _controllers(payload: object, model: str | None) -> list[dict[str, object]]:
    """The adaptive connection controllers in a config payload, narrowed to a model.

    `adaptive` sits at the top of the server's own payload — in the task envelope exactly as in the process one; the `knobs` wrapper `inspect ctl` prints is the CLI's presentation rather than the endpoint's, and this module talks to the endpoint.
    """
    if not isinstance(payload, dict):
        return []
    controllers = cast(dict[str, object], payload).get("adaptive")
    if not isinstance(controllers, list):
        return []
    return [
        cast(dict[str, object], entry)
        for entry in cast(list[object], controllers)
        if isinstance(entry, dict)
        and (
            model is None or _text(cast(dict[str, object], entry).get("name")) == model
        )
    ]


def _connections(payload: object, model: str | None) -> LiveConnections:
    """The model connection pool, summed across the process's controllers.

    Summed rather than picked, because a worker running one task against one model has exactly one controller and the sum is that controller — while a task with model roles has several, and their total is what the pool actually is.

    **Narrowed to the row's own model once a process holds several tasks**, because past that point the process total is not a fact about any one of them: a batch spanning four models would give every row all four pools added together, four times over. Two caveats the narrowing does not remove and cannot, since both are true of the process rather than of the reading. A controller is **shared** — two packed tasks on one model are drawing on the same pool, and it appears in both their rows because that is where it is being spent. And a packed task's **role** models are dropped, since nothing in the row names them; the alternative is attributing a sibling's models to it, which is the error this is fixing.
    """
    in_use = 0
    limit: int | None = None
    for controller in _controllers(payload, model):
        in_use += _number(controller.get("in_use"))
        if isinstance(controller.get("limit"), int):
            limit = (limit or 0) + cast(int, controller["limit"])
    return LiveConnections(in_use=in_use, limit=limit)


def _scale_downs(
    payload: object, model: str | None, claimed: frozenset[str] = frozenset()
) -> tuple[float, ...]:
    """When this row's controllers last cut, oldest first.

    A cut is a `recent_changes` entry whose `to` is below its `from` — the multiplicative decrease a rate-limit episode leaves behind. Narrowed by model as `_connections` is, since a sibling task's pushback is not this row's — but with one deliberate difference, because this reading gates a *decision* where that one feeds a column.

    **A controller no row claims is charged to every row.** A packed task's role models are invisible from its row, so narrowing on the primary model alone drops their pushback from the whole process: nobody claims the grader's controller, so nobody sees it cut, and every sibling reads clean while the process is being rate-limited. Attributing an unclaimed controller to all of them fails closed on exactly the cuts whose owner cannot be established, and leaves a named sibling's pushback where it belongs. `claimed` is the set of models the process's rows name; empty, or a `model` of `None`, means no narrowing is needed at all.
    """
    cuts: list[float] = []
    for controller in _controllers(payload, None):
        name = _text(controller.get("name"))
        if model is not None and name != model and name in claimed:
            continue
        changes = controller.get("recent_changes")
        if not isinstance(changes, list):
            continue
        for entry in cast(list[object], changes):
            if not isinstance(entry, dict):
                continue
            change = cast(dict[str, object], entry)
            at, was, to = change.get("at"), change.get("from"), change.get("to")
            if (
                isinstance(at, int | float)
                and not isinstance(at, bool)
                and isinstance(was, int)
                and isinstance(to, int)
                and to < was
            ):
                cuts.append(float(at))
    return tuple(sorted(cuts))


def _connections_ceiling(payload: object, model: str | None) -> int | None:
    """How far this row's adaptive controllers may climb, or `None` where none is adaptive.

    The maximum across controllers rather than a sum, because a retune through the control channel sets every controller's bound to the same number — the ceiling is one setting worn by several, not a pool they divide.
    """
    return _highest(payload, model, "max")


def _connections_limit(payload: object, model: str | None) -> int | None:
    """Where this row's adaptive controllers currently sit, or `None` where none is adaptive.

    A maximum for the same reason the ceiling is one: this is what a storm cut clamps the ceiling *to*, and the ceiling it writes is worn by every controller alike.
    """
    return _highest(payload, model, "limit")


def _highest(payload: object, model: str | None, key: str) -> int | None:
    values = [
        cast(int, controller[key])
        for controller in _controllers(payload, model)
        if isinstance(controller.get(key), int)
        and not isinstance(controller.get(key), bool)
    ]
    return max(values) if values else None


def _sample_limit(payload: object) -> tuple[int, int] | None:
    """The `max_samples` knob as (in use, limit), or `None` where it is not retunable.

    Only the task envelope carries it, and only as a setpoint where the run set one explicitly — the adaptive sample-concurrency path reports it unadjustable, and a knob the tuning loop cannot turn is one it must not report as a level.
    """
    if not isinstance(payload, dict):
        return None
    knob = cast(dict[str, object], payload).get("max_samples")
    if not isinstance(knob, dict):
        return None
    view = cast(dict[str, object], knob)
    if view.get("adjustable") is not True:
        return None
    limit, in_use = view.get("limit"), view.get("in_use")
    if not isinstance(limit, int) or isinstance(limit, bool):
        return None
    return (_number(in_use), limit)


def _sandboxes(payload: object) -> tuple[int, int] | None:
    """The process's sandbox limiters as (in use, limit), or `None` where none is in effect.

    Summed across providers, which is almost always a sum of one; an elastic provider registers no limiter at all and so caps nothing.
    """
    if not isinstance(payload, dict):
        return None
    limiters = cast(dict[str, object], payload).get("max_sandboxes")
    if not isinstance(limiters, list):
        return None
    in_use = limit = 0
    counted = False
    for entry in cast(list[object], limiters):
        if not isinstance(entry, dict):
            continue
        limiter = cast(dict[str, object], entry)
        ceiling = limiter.get("limit")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool):
            continue
        counted = True
        limit += ceiling
        in_use += _number(limiter.get("in_use"))
    return (in_use, limit) if counted else None


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
