"""Fixture that hangs before reaching eval_set() (for timeout testing)."""

import time

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate

time.sleep(60)


@task
def slow() -> Task:
    return Task(
        dataset=[Sample(input="1+1", target="2")],
        solver=[generate()],
        scorer=exact(),
    )


eval_set(
    tasks=[slow()],
    model="mockllm/model",
    log_dir="logs",
)
