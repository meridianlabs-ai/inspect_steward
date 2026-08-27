"""Reading a running fleet, fast enough to do it every time somebody looks.

`ctl.py` reaches the control channel by running `inspect ctl`, and its reasoning holds for everything it does: the CLI already draws *alive but busy* apart from *gone*, retries reads, never retries mutations, and reports failures in a closed vocabulary. Reimplementing that would mean copying four constants and then drifting from them.

**This module revisits that decision for reads only, and the reason is a number.** A `status` table wants three things per worker — sample counts, per-sample limit usage, and the model connection pool — and only the first two span the fleet in one CLI call. `inspect ctl config` resolves a *single* task, so the third is one invocation per worker, and an invocation costs ~1.6s almost entirely in `import inspect_ai`. A fleet of ten is about nineteen seconds. The same three reads over the worker's own socket are about six milliseconds each and run concurrently across the fleet.

Nineteen seconds is not a slow table, it is a table nobody runs, and `status` exists to be run often. So reads come here and **every mutation stays in `ctl.py`** — which is also where the risk is, since a retune that half-lands is a real problem and a status column that is missing for one turn is not.

**Concurrent across the fleet and within a worker, which stopped being the same statement once a run could be packed.** A worker holding one task is three requests; a worker holding five hundred is three plus five hundred, and awaiting those in turn rebuilds the serial read this module exists to replace — inside a single process, where it is not even bounded by `timeout`, since that bounds each request rather than the chain. So `_read` gathers its per-eval reads.

**The retry policy is deliberately the opposite of the CLI's, for the same reason.** The control server shares the eval's event loop, so a busy eval can stall a response for seconds. The CLI waits, because a retune has to land. A table must not: a worker that does not answer promptly is reported as busy and the row is rendered without its live columns. Waiting would make the common case — a fleet mid-generate — the slow one.

Nothing here raises. A worker that has gone, wedged, or never bound a socket costs its own row's live columns and nothing else.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
    """How far the furthest-along sample has got against each per-sample budget.

    The **maximum** across running samples rather than the mean, because the question a limit column answers is *how close is this to tripping*, and that is decided by the leader. A mean would hide one sample about to be cut off behind ninety that just started.
    """

    turns: int = 0
    messages: int = 0
    tokens: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class LiveConnections:
    """The model connection pool, which is the fleet's real throughput knob."""

    in_use: int = 0
    limit: int | None = None


@dataclass(frozen=True)
class LiveTask:
    """What one worker says about itself right now."""

    pid: int
    identifier: str
    task_id: str = ""
    samples: LiveSamples = field(default_factory=LiveSamples)
    usage: LiveUsage = field(default_factory=LiveUsage)
    connections: LiveConnections = field(default_factory=LiveConnections)
    total_tokens: int = 0

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
) -> LiveFleet:
    """Read every worker concurrently.

    Args:
        targets: Running workers with a discovered socket. An empty list is an empty fleet and costs nothing — which is the common shape late in a campaign, when everything has finished and there is nothing live to ask.
        locations: Log location to task identifier, for naming the rows a packed worker reports. Required rather than defaulted, and positional rather than keyword, because a caller that omits it is only ever wrong: at the default width it is unread, and at any other width its absence silently costs every row its identity and reports a running task as finished. An empty mapping says *nothing to correlate* out loud.
        timeout: Seconds to wait per worker.

    Returns:
        What each worker reported, including the ones that could not be reached.
    """
    if not targets:
        return LiveFleet()
    return asyncio.run(read_fleet_async(targets, locations, timeout=timeout))


async def read_fleet_async(
    targets: list[LiveTarget],
    locations: Mapping[str, str],
    *,
    timeout: float = TIMEOUT,
) -> LiveFleet:
    """Read every worker concurrently.

    Args:
        targets: Running workers with a discovered socket.
        locations: Log location to task identifier, for naming the rows a packed worker reports.
        timeout: Seconds to wait per worker.

    Returns:
        What each worker reported.
    """
    if not targets:
        return LiveFleet()
    read = await asyncio.gather(
        *(_read(target, timeout, locations) for target in targets),
    )
    return LiveFleet(tasks={task.identifier: task for tasks in read for task in tasks})


async def _read(
    target: LiveTarget, timeout: float, locations: Mapping[str, str]
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
            # one read per distinct eval, all at once. Awaiting them in turn
            # would make a packed worker's latency its batch size -- a
            # five-hundred-task process is five hundred round trips, which is
            # the serial fleet read this module exists to avoid, rebuilt
            # inside one worker. `timeout` bounds each request, so a serial
            # chain is not bounded by it either
            evals = sorted({_text(row.get("eval_id")) for row in rows})
            read = await asyncio.gather(
                *(_get(client, f"/evals/{eval_id}/samples") for eval_id in evals)
            )
            config = await _get(client, "/config")
            samples = dict(zip(evals, read, strict=True))
    except httpx.TimeoutException:
        # alive, and its event loop is busy running the eval -- which is the
        # thing it is supposed to be doing, so this is not a fault
        return unavailable("busy")
    except (httpx.HTTPError, OSError):
        return unavailable("gone")
    except Exception as ex:  # a shape this version has not seen
        return unavailable(f"{type(ex).__name__}: {ex}")

    live: list[LiveTask] = []
    for row in rows:
        identifier = _identify(row, target, locations)
        if identifier is None:
            # a row this worker is running that nothing can name. Dropped
            # rather than guessed at: the cost is one row's live columns for
            # one turn, and the alternative is attributing a task's numbers to
            # a sibling that is not the one doing the work
            continue
        live.append(
            LiveTask(
                pid=target.pid,
                identifier=identifier,
                task_id=_text(row.get("task_id")),
                samples=_samples(row.get("samples")),
                usage=_usage(samples.get(_text(row.get("eval_id")))),
                connections=_connections(config, _model(row, target)),
                total_tokens=_number(row.get("total_tokens")),
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


def _usage(payload: object) -> LiveUsage:
    """The leading running sample's counts against each per-sample budget."""
    if not isinstance(payload, dict):
        return LiveUsage()
    rows = cast(dict[str, object], payload).get("samples")
    if not isinstance(rows, list):
        return LiveUsage()

    turns = messages = tokens = 0
    seconds = 0.0
    for entry in cast(list[object], rows):
        if not isinstance(entry, dict):
            continue
        sample = cast(dict[str, object], entry)
        if sample.get("status") != "running":
            continue
        turns = max(turns, _number(sample.get("turn_count")))
        messages = max(messages, _number(sample.get("message_count")))
        tokens = max(tokens, _number(sample.get("total_tokens")))
        elapsed = sample.get("total_time")
        if isinstance(elapsed, float | int):
            seconds = max(seconds, float(elapsed))
    return LiveUsage(turns=turns, messages=messages, tokens=tokens, seconds=seconds)


def _model(row: dict[str, object], target: LiveTarget) -> str | None:
    """Which controllers this row's connections should be read from, if it must be narrowed.

    `None` for a worker holding one task, which is the whole process and therefore every controller in it — including the extra ones a task with model roles brings, whose total is what its pool actually is.
    """
    return None if target.only is not None else _text(row.get("model")) or None


def _connections(payload: object, model: str | None) -> LiveConnections:
    """The model connection pool, summed across the process's controllers.

    Summed rather than picked, because a worker running one task against one model has exactly one controller and the sum is that controller — while a task with model roles has several, and their total is what the pool actually is.

    **Narrowed to the row's own model once a process holds several tasks**, because past that point the process total is not a fact about any one of them: a batch spanning four models would give every row all four pools added together, four times over. Two caveats the narrowing does not remove and cannot, since both are true of the process rather than of the reading. A controller is **shared** — two packed tasks on one model are drawing on the same pool, and it appears in both their rows because that is where it is being spent. And a packed task's **role** models are dropped, since nothing in the row names them; the alternative is attributing a sibling's models to it, which is the error this is fixing.
    """
    if not isinstance(payload, dict):
        return LiveConnections()
    # `adaptive` sits at the top of the server's own payload; the `knobs`
    # wrapper with `max_connections` inside it is `inspect ctl`'s presentation
    # rather than the endpoint's, and this module talks to the endpoint
    controllers = cast(dict[str, object], payload).get("adaptive")
    if not isinstance(controllers, list):
        return LiveConnections()

    in_use = 0
    limit: int | None = None
    for entry in cast(list[object], controllers):
        if not isinstance(entry, dict):
            continue
        controller = cast(dict[str, object], entry)
        if model is not None and _text(controller.get("name")) != model:
            continue
        in_use += _number(controller.get("in_use"))
        if isinstance(controller.get("limit"), int):
            limit = (limit or 0) + cast(int, controller["limit"])
    return LiveConnections(in_use=in_use, limit=limit)


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
