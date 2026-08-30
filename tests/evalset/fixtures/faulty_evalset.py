"""A definition that fails wherever a test asks it to.

Three points are worth failing at, and together they are the whole taxonomy of what can go wrong in a worker's lifetime (testing.md, *the faults, and how to inject them deterministically*):

- **`pre`** — the module body, before `eval_set()`. The invisible-worker window: a process exists, no log has been written, and no control socket is bound, so both ground-truth sources say nothing is there.
- **`run`** — inside a sample. The log has been started and the socket is bound; nothing has landed.
- **`post`** — after `eval_set()` returns. The log has landed and the process has not exited yet, which is the only way to observe that state at all.

**The fault is a state to wait for, not a delay to outlast.** On arrival the definition writes `<dir>/<point>.reached`, and `hang` then blocks until the test writes `<dir>/<point>.go`. A test that wants a worker held in one of these states waits for `reached` and never releases it; one that wants the worker to carry on releases it. Neither has a number in it. That is also why there is no `slow` behaviour despite testing.md naming slow-hang-crash as the taxonomy: a delay is a race that passes locally and fails in CI, and a gate is a state.

The marker directory is passed in rather than derived, because one of the faults this exists for is deleting `.steward/` — a marker underneath it would go with the fault it is meant to be observing.

Armed only in worker mode. Capture executes the definition too, and reading a manifest has to stay free; `slow_evalset.py` and `raises_early.py` are the capture-side equivalents and stay separate for that reason.
"""

import asyncio
import os
import time
from pathlib import Path

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import Generate, TaskState, generate, solver

FAULT = "FAULTY_EVALSET_FAULT"
"""`<point>:<behaviour>` — `pre`/`run`/`post` against `hang`/`crash`/`exit:<n>`.

Named for this fixture rather than for Steward, and that is not cosmetic: `STEWARD_*` is a namespace Steward polices, refusing anything under it that is not one of its settings. These variables are this file's own protocol with the tests that drive it, so they belong outside it — the rename was what the refusal caught.
"""

FAULT_DIR = "FAULTY_EVALSET_DIR"
"""Where the `reached` and `go` markers live. Outside the workspace, so a fault can delete `.steward/` without taking the markers with it."""

ORPHANED = 97
"""Exit status for a held worker whose test run has gone. Distinctive, so it reads as this and not as something the eval did."""

_POLL = 0.02

_armed = bool(
    os.environ.get("INSPECT_EVAL_SET_SELECTION") and os.environ.get(FAULT_DIR)
)
_point, _, _behaviour = (os.environ.get(FAULT, "") if _armed else "").partition(":")
_dir = Path(os.environ.get(FAULT_DIR, ""))
_spawner = os.getppid()


def _waiting(point: str) -> bool:
    """Whether to keep holding here.

    A worker is detached, so nothing kills it when the test run that spawned it ends — and a hold waits on a marker that a finished run will never write, which is a process that spins forever rather than one that eventually gives up. So the hold also watches for its spawner disappearing, which is a state rather than a timeout: against the pid captured at startup rather than against 1, since a subreaper can adopt an orphan without init ever seeing it.
    """
    if os.getppid() != _spawner:
        os._exit(ORPHANED)
    return not (_dir / f"{point}.go").exists()


def _reached(point: str) -> tuple[str, str] | None:
    """Announce arriving at a point, and say what to do here. `None` if nothing."""
    if point != _point:
        return None
    (_dir / f"{point}.reached").touch()
    kind, _, argument = _behaviour.partition(":")
    return kind, argument


def _abort(kind: str, argument: str, point: str) -> None:
    if kind == "crash":
        raise RuntimeError(f"fault injected at {point}")
    if kind == "exit":
        # `os._exit`, not `sys.exit`: inside a solver a `SystemExit` is caught
        # and becomes a sample error, and the whole point of `exit` is a
        # process that is simply gone
        os._exit(int(argument or 1))


def arrive(point: str) -> None:
    """Apply the fault at a point outside the eval (`pre`, `post`)."""
    if (fault := _reached(point)) is None:
        return
    kind, argument = fault
    if kind == "hang":
        while _waiting(point):
            time.sleep(_POLL)
    else:
        _abort(kind, argument, point)


async def arrive_in_eval(point: str) -> None:
    """Apply the fault at a point inside the running eval.

    Waits on the loop rather than blocking it. The control server shares the eval's event loop, so a synchronous hang here would make a held worker unreachable — the opposite of what holding one is for.
    """
    if (fault := _reached(point)) is None:
        return
    kind, argument = fault
    if kind == "hang":
        while _waiting(point):
            await asyncio.sleep(_POLL)
    else:
        _abort(kind, argument, point)


@solver
def faulty():
    """A sample that can be held open, which is the only way to catch an eval running."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await arrive_in_eval("run")
        return await generate(state)

    return solve


@task
def faulted() -> Task:
    return Task(
        dataset=[Sample(input="1+1", target="2")],
        solver=[faulty(), generate()],
        scorer=exact(),
    )


arrive("pre")

eval_set(
    tasks=[faulted()],
    model="mockllm/model",
    log_dir="logs",
)

arrive("post")
