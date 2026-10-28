"""One manifest task that creates a second task while it runs.

`creator`'s solver hands `child` to `enqueue_task()`, so a worker selected to run `creator` alone also writes a log for a task no manifest names — whose `eval.origin` names `creator`, which is what lets Steward file it under its creator.

`child` is built by calling its `@task` function inside the running eval, the case eval-set selection pruning must leave alone, and answers without a model so it does not depend on which model an enqueued task resolves to.
"""

from inspect_ai import Task, enqueue_task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import Generate, Solver, TaskState, solver


@solver
def echo() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.output.completion = state.input_text
        return state

    return solve


@task
def child() -> Task:
    return Task(
        dataset=[Sample(input="hello", target="hello")],
        solver=[echo()],
        scorer=exact(),
    )


@solver
def create_child() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        enqueue_task(child())
        return await generate(state)

    return solve


@task
def creator() -> Task:
    return Task(
        dataset=[Sample(input="1+1", target="2")],
        solver=[create_child()],
        scorer=exact(),
    )


eval_set(
    tasks=[creator()],
    model="mockllm/model",
    log_dir="logs",
)
