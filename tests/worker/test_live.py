"""Reading a worker over its own socket.

The server here is real — an AF_UNIX listener speaking HTTP/1.1 in a background
thread — so the transport, the timeout, and the concurrency are the genuine
article and only the payloads are canned. That matters more than usual for this
module: the reason it exists rather than deferring to `inspect ctl` is a timing
claim, and a stubbed client would test the parsing while quietly discarding the
part under test.

No launches. An eval that is reliably mid-generate cannot be manufactured on
demand, and a socket that answers slowly can.
"""

import asyncio
import json
import shutil
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from inspect_steward._worker import (
    LiveConnections,
    LiveFleet,
    LiveSamples,
    LiveTarget,
    LiveUsage,
    read_fleet,
)


@pytest.fixture
def sockets() -> Generator[Path]:
    """A directory short enough to bind a socket in.

    Not `tmp_path`, for the reason `fake_home` gives: `sun_path` holds 104 bytes and pytest's temporaries are longer than that on macOS.
    """
    path = Path(tempfile.mkdtemp(prefix="stw-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --- a worker, for the purposes of being read ---------------------------

Routes = dict[str, object]

TASK: dict[str, object] = {
    "eval_id": "E1",
    "task_id": "T1",
    "total_tokens": 4096,
    "samples": {
        "total": 123,
        "completed": 5,
        "errored": 2,
        "cancelled": 0,
        "in_flight": 57,
        "queued": 61,
    },
}

TASKS: list[object] = [TASK]

SAMPLES: dict[str, object] = {
    "samples": [
        {"status": "running", "turn_count": 8, "message_count": 20, "total_tokens": 90},
        {"status": "running", "turn_count": 3, "message_count": 40, "total_tokens": 10},
        # a finished sample ran to the limit; it is not what a limit column is
        # asking about, and letting it in would report a task as nearly stopped
        # forever after its first long sample
        {
            "status": "completed",
            "turn_count": 300,
            "message_count": 900,
            "total_tokens": 99,
        },
    ]
}

CONFIG: dict[str, object] = {"adaptive": [{"in_use": 52, "limit": 80}]}

WORKER: Routes = {"/tasks": TASKS, "/config": CONFIG, "/evals/E1/samples": SAMPLES}

FINISHED: Routes = {**WORKER, "/tasks": []}
"""A worker whose eval ended between the scan that found its socket and the read."""

NO_PACKING: dict[str, str] = {}
"""No log locations to correlate, which is every worker holding one task.

Spelled out at each call rather than defaulted, because it is exactly what a packed worker must not be given by accident: a correlation map that is missing rather than empty costs every row its identity and reports a running task as finished.
"""


@contextmanager
def worker(
    socket: Path, routes: Routes | None = None, *, stall: bool = False
) -> Generator[LiveTarget]:
    """Serve `routes` on `socket` until the block exits.

    Args:
        socket: Where to bind.
        routes: Path to JSON body; anything unrouted is a 404.
        stall: Accept the connection and never answer — a worker whose event loop is busy running the eval, which is the case this module is shaped around.
    """
    loop = asyncio.new_event_loop()
    listening = threading.Event()
    stopping = asyncio.Event()
    table = WORKER if routes is None else routes

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        if stall:
            await stopping.wait()
            writer.close()
            return
        path = request.split(b" ")[1].decode()
        found = path in table
        body = json.dumps(table.get(path)).encode()
        writer.write(
            f"HTTP/1.1 {200 if found else 404} .\r\n"
            f"Content-Type: application/json\r\n"
            # this server answers one request per connection, and HTTP/1.1
            # means keep-alive unless a response says otherwise. Without this
            # header the client pools a socket the next line closes, and the
            # second of `_read`'s three requests dies on EOF -- a
            # `RemoteProtocolError`, which reports a healthy worker as `gone`.
            # Rare when idle and reliable under load, so it read as a flake
            f"Connection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    async def serve() -> None:
        async with await asyncio.start_unix_server(handle, path=str(socket)):
            listening.set()
            await stopping.wait()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert listening.wait(timeout=5), "server never bound"
    try:
        yield LiveTarget(identifiers=("task-1",), pid=1, socket=socket)
    finally:
        loop.call_soon_threadsafe(stopping.set)
        thread.join(timeout=5)
        loop.close()


# --- what a worker says about itself ------------------------------------


def test_a_worker_reports_the_counts_its_log_has_not_caught_up_to(
    sockets: Path,
) -> None:
    with worker(sockets / "w.sock") as target:
        fleet = read_fleet([target], NO_PACKING)

    (task,) = fleet.tasks.values()
    assert task.unavailable is None
    assert (task.samples.completed, task.samples.total) == (5, 123)
    assert (task.samples.in_flight, task.samples.queued) == (57, 61)
    assert task.connections.in_use == 52
    assert task.connections.limit == 80
    assert task.total_tokens == 4096


def test_usage_is_the_leading_sample_rather_than_the_mean(sockets: Path) -> None:
    # a limit column answers *how close is this to tripping*, which the leader
    # decides -- a mean hides one sample about to be cut off behind ninety that
    # have just started
    with worker(sockets / "w.sock") as target:
        (task,) = read_fleet([target], NO_PACKING).tasks.values()

    assert task.usage.turns == 8
    assert task.usage.messages == 40
    assert task.usage.tokens == 90


def test_connection_pools_are_summed_across_a_task_s_controllers(
    sockets: Path,
) -> None:
    # one controller per model, so a task with model roles has several and the
    # pool is their total
    roles = dict(WORKER, **{"/config": {"adaptive": [{"in_use": 4, "limit": 10}] * 3}})
    with worker(sockets / "w.sock", roles) as target:
        (task,) = read_fleet([target], NO_PACKING).tasks.values()

    assert (task.connections.in_use, task.connections.limit) == (12, 30)


# --- and when it does not -----------------------------------------------


def test_a_worker_that_does_not_answer_promptly_is_busy_rather_than_gone(
    sockets: Path,
) -> None:
    # the control server shares the eval's event loop, so no answer within the
    # budget is the fleet doing its job -- the row renders from its log
    with worker(sockets / "w.sock", stall=True) as target:
        (task,) = read_fleet([target], NO_PACKING, timeout=0.25).tasks.values()

    assert task.unavailable == "busy"


def test_one_busy_worker_does_not_cost_the_others_their_columns(
    sockets: Path,
) -> None:
    # the whole fleet is read concurrently, so the slowest worker sets the wall
    # clock and nothing else
    with (
        worker(sockets / "slow.sock", stall=True) as slow,
        worker(sockets / "fast.sock") as fast,
    ):
        fleet = read_fleet(
            [slow, LiveTarget(identifiers=("task-2",), pid=2, socket=fast.socket)],
            NO_PACKING,
            timeout=0.25,
        )

    assert fleet.tasks["task-1"].unavailable == "busy"
    assert fleet.tasks["task-2"].unavailable is None
    assert fleet.tasks["task-2"].samples.completed == 5
    assert [task.identifier for task in fleet.unavailable] == ["task-1"]


def test_a_socket_nothing_is_listening_on_is_gone(sockets: Path) -> None:
    target = LiveTarget(identifiers=("task-1",), pid=1, socket=sockets / "nobody.sock")

    (task,) = read_fleet([target], NO_PACKING).tasks.values()

    assert task.unavailable == "gone"


def test_a_worker_whose_eval_finished_mid_read_says_so(sockets: Path) -> None:
    # the process is still up but has nothing to report: it finished between
    # the scan that found its socket and the read -- reaping is the next turn's
    with worker(sockets / "w.sock", FINISHED) as target:
        (task,) = read_fleet([target], NO_PACKING).tasks.values()

    assert task.unavailable == "finished"


# --- a process holding several tasks ------------------------------------

TWO_MODELS: dict[str, object] = {
    "adaptive": [
        {"name": "openai/gpt-4", "in_use": 3, "limit": 10},
        {"name": "anthropic/claude", "in_use": 7, "limit": 20},
    ]
}
"""Controllers are process-global and one per model, so a packed batch spanning two models has both."""

PACKED_ROWS: list[object] = [
    {**TASK, "log_location": "logs/one.eval", "model": "openai/gpt-4"},
    {
        **TASK,
        "eval_id": "E2",
        "task_id": "T2",
        "log_location": "logs/two.eval",
        "total_tokens": 8192,
        "model": "anthropic/claude",
    },
]

PACKED: Routes = {
    "/tasks": PACKED_ROWS,
    "/config": TWO_MODELS,
    "/evals/E1/samples": SAMPLES,
    "/evals/E2/samples": SAMPLES,
}
"""Two tasks in one process, each writing its own log — the whole run under `max_workers: 1`."""

CORRELATION = {"logs/one.eval": "task-1", "logs/two.eval": "task-2"}


def read_packed(socket: Path, locations: dict[str, str]) -> LiveFleet:
    """Read a worker holding both tasks."""
    target = LiveTarget(identifiers=("task-1", "task-2"), pid=1, socket=socket)
    return read_fleet([target], locations)


def test_each_task_of_a_packed_worker_is_named_by_the_log_it_is_writing(
    sockets: Path,
) -> None:
    """The join is the log location, which is the only field the control channel and Steward's observation both hold."""
    with worker(sockets / "w.sock", PACKED) as target:
        fleet = read_packed(target.socket, CORRELATION)

    assert sorted(fleet.tasks) == ["task-1", "task-2"]
    assert fleet.tasks["task-1"].total_tokens == 4096
    assert fleet.tasks["task-2"].total_tokens == 8192
    assert fleet.unavailable == []


def test_a_packed_worker_read_without_the_correlation_reports_nothing(
    sockets: Path,
) -> None:
    """The failure the required argument exists to make impossible.

    Asserted rather than left implicit because the wrong answer here is a quiet one: every row is dropped, both tasks read `finished`, and a run busy generating looks converged. If `locations` is ever made optional again, this is the test that says what that costs.
    """
    with worker(sockets / "w.sock", PACKED) as target:
        fleet = read_packed(target.socket, {})

    assert [task.unavailable for task in fleet.unavailable] == ["finished"] * 2


def test_one_uncorrelated_row_costs_its_own_task_and_no_other(
    sockets: Path,
) -> None:
    """A task whose log has not appeared yet — the pre-boundary window, where there is nothing to report about it anyway."""
    with worker(sockets / "w.sock", PACKED) as target:
        fleet = read_packed(target.socket, {"logs/one.eval": "task-1"})

    assert fleet.tasks["task-1"].unavailable is None
    assert fleet.tasks["task-2"].unavailable == "finished"


def test_a_packed_row_reports_its_own_model_s_pool_and_not_the_process_s(
    sockets: Path,
) -> None:
    """Controllers are process-global, so the unfiltered sum is not a fact about any one task.

    Both tasks would otherwise read 10/30 — every model in the process, added together, in every row. Each row's own controller is the number that means something about it.
    """
    with worker(sockets / "w.sock", PACKED) as target:
        fleet = read_packed(target.socket, CORRELATION)

    assert (
        fleet.tasks["task-1"].connections.in_use,
        fleet.tasks["task-1"].connections.limit,
    ) == (3, 10)
    assert (
        fleet.tasks["task-2"].connections.in_use,
        fleet.tasks["task-2"].connections.limit,
    ) == (7, 20)


def test_a_worker_holding_one_task_still_sums_every_controller(
    sockets: Path,
) -> None:
    """The default width needs no narrowing and must not get one.

    One task *is* the whole process, extra controllers included: a task with model roles has one per role, and their total is what its pool actually is. Narrowing by the row's own model would silently drop the roles.
    """
    single: Routes = {**PACKED, "/tasks": [PACKED_ROWS[0]], "/config": TWO_MODELS}
    with worker(sockets / "w.sock", single) as target:
        (task,) = read_fleet([target], NO_PACKING).tasks.values()

    assert (task.connections.in_use, task.connections.limit) == (10, 30)


# --- payloads this version has not seen ---------------------------------

BLANK: dict[str, object] = {
    "samples": LiveSamples(),
    "connections": LiveConnections(),
    "usage": LiveUsage(),
}
"""What a group of columns reads as when its payload could not be understood."""

MALFORMED: list[tuple[str, Routes, str]] = [
    (
        "a task listing that is not a list",
        {**WORKER, "/tasks": {"tasks": []}},
        "samples",
    ),
    ("a task row that is not an object", {**WORKER, "/tasks": ["T1"]}, "samples"),
    (
        "sample counts that are not an object",
        {**WORKER, "/tasks": [{"eval_id": "E1", "samples": 5}]},
        "samples",
    ),
    (
        "counts that are not numbers",
        {**WORKER, "/tasks": [{"eval_id": "E1", "samples": {"total": "5"}}]},
        "samples",
    ),
    (
        "a config with no controllers",
        {**WORKER, "/config": {"knobs": {}}},
        "connections",
    ),
    (
        "controllers that are not a list",
        {**WORKER, "/config": {"adaptive": 4}},
        "connections",
    ),
    (
        "a sample listing that is not a list",
        {**WORKER, "/evals/E1/samples": {}},
        "usage",
    ),
]


@pytest.mark.parametrize(
    ("routes", "blank"),
    [(routes, blank) for _, routes, blank in MALFORMED],
    ids=[case for case, _, _ in MALFORMED],
)
def test_a_shape_this_version_has_not_seen_costs_columns_not_the_row(
    routes: Routes, blank: str, sockets: Path
) -> None:
    # nothing here raises: a table is worth drawing without a column, and a
    # status command that dies on an upstream field rename is worth less than
    # one that reports what it could read
    with worker(sockets / "w.sock", routes) as target:
        fleet = read_fleet([target], NO_PACKING)

    (task,) = fleet.tasks.values()
    assert task.identifier == "task-1"
    assert getattr(task, blank) == BLANK[blank]
