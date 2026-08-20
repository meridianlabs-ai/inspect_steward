from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate


@task
def addition(difficulty: str = "easy") -> Task:
    return Task(
        dataset=[
            Sample(input="1+1", target="2"),
            Sample(input="2+2", target="4"),
        ],
        solver=[generate()],
        scorer=exact(),
    )
