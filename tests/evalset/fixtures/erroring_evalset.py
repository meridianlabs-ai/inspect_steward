"""A definition whose marked samples error until the test heals them.

The invalidate-and-resume cycle needs a failure that is real on the first run and gone on the re-run — a provider outage, as a fixture. Samples whose input starts with `fail` raise until the test touches `<dir>/healed`; the healthy samples pass either way, which is what lets the re-run prove it reused them.

The sample ids carry a colon, as a corpus id can (`user:cybergym/arvo_1`): a `zero` ruling selects ids by name in its side run, and an id with a colon is the shape that selection once dropped.

The marker directory arrives in `ERRORING_EVALSET_DIR` — this fixture's own protocol with the test driving it, outside the `STEWARD_*` namespace Steward polices (the same argument `faulty_evalset.py` records). The capture executes this module too, but no sample runs there, so the fault costs it nothing.
"""

import os
from pathlib import Path

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import Generate, TaskState, generate, solver

HEALED_DIR = "ERRORING_EVALSET_DIR"


@solver
def outage():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        marked = str(state.input_text).startswith("fail")
        healed = (Path(os.environ[HEALED_DIR]) / "healed").exists()
        if marked and not healed:
            raise RuntimeError(f"injected outage for {state.input_text}")
        return state

    return solve


@task
def probe() -> Task:
    return Task(
        dataset=[
            Sample(id="user:cybergym/arvo_1", input="1+1", target="2"),
            Sample(id="user:cybergym/arvo_2", input="2+2", target="4"),
            Sample(id="user:cybergym/arvo_3", input="fail-1", target="fail-1"),
            Sample(id="user:cybergym/arvo_4", input="fail-2", target="fail-2"),
        ],
        solver=[outage(), generate()],
        scorer=exact(),
    )


eval_set(
    tasks=[probe()],
    model="mockllm/model",
    log_dir="logs",
)
