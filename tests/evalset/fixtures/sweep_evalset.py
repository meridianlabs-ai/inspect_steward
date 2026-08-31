from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate


@task
def sweep(difficulty: str = "easy") -> Task:
    return Task(
        dataset=[Sample(input=f"question ({difficulty})", target="answer")],
        solver=[generate()],
        scorer=exact(),
    )


eval_set(
    tasks=[sweep(difficulty="easy"), sweep(difficulty="hard")],
    model=["mockllm/model", "mockllm/model2"],
    log_dir="logs",
)
