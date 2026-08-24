"""Fast to capture, held open to run: a worker parked before its `eval_set()`.

The wait happens only in worker mode and only until a test releases the gate, so reading a manifest from this definition costs nothing. What it holds open is the window between a worker's process existing and its eval starting — the one interval where the log directory and control discovery both say nothing — which can only be tested by making it last long enough to observe.
"""

import asyncio
import os
import time

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import Generate, TaskState, generate, solver

_gate = os.environ.get("STEWARD_TEST_GATE")
if _gate and os.environ.get("INSPECT_EVAL_SET_SELECTION"):
    while not os.path.exists(_gate):
        time.sleep(0.02)


@solver
def unhurried():
    """A sample slow enough to be caught running.

    One mockllm sample is over before a poll can see the eval at all, and a
    control socket that exists for less time than it takes to look for it is
    not a socket any test can assert on. `STEWARD_TEST_SLEEP` raises it for a
    test that has to ask the running eval several questions — each `inspect
    ctl` invocation costs about 1.3s, so a handful of them outlast the default.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await asyncio.sleep(float(os.environ.get("STEWARD_TEST_SLEEP", "5")))
        return await generate(state)

    return solve


@task
def gated() -> Task:
    return Task(
        dataset=[Sample(input="1+1", target="2")],
        solver=[unhurried(), generate()],
        scorer=exact(),
    )


eval_set(
    tasks=[gated()],
    model="mockllm/model",
    log_dir="logs",
)
