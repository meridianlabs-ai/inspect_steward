from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate
from inspect_scout import Result, Transcript, scanner


@scanner(messages="all")
def transcript_echo():
    """Deterministic scanner: one row per transcript, no model."""

    async def scan(transcript: Transcript) -> Result:
        return Result(value=f"scanned:{transcript.transcript_id}")

    return scan


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
    scanner=[transcript_echo()],
    log_dir="logs",
)
