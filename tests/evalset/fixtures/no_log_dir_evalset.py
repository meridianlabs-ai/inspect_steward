"""A definition that names no log directory, which is the shape Steward recommends."""

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate


@task
def addition() -> Task:
    return Task(
        dataset=[Sample(input="1+1", target="2")],
        solver=[generate()],
        scorer=exact(),
    )


eval_set(
    tasks=[addition()],
    model="mockllm/model",
)
