from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate


@task
def addition() -> Task:
    return Task(
        dataset=[
            Sample(input="1+1", target="2"),
            Sample(input="2+2", target="4"),
        ],
        solver=[generate()],
        scorer=exact(),
    )


@task
def echo() -> Task:
    return Task(
        dataset=[Sample(input="hello", target="hello")],
        solver=[generate()],
        scorer=exact(),
        epochs=2,
    )


eval_set(
    tasks=[addition(), echo()],
    model="mockllm/model",
    log_dir="logs",
)
