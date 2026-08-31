"""A definition whose samples all park, which is what saturates the sample limiter.

The ramp's up-gate requires demand — `in_use == limit` — and demand needs samples that occupy their slots rather than finishing. `mockllm` finishes instantly, so the hold is borrowed from `approval_evalset.py`: every sample calls a tool under `approver: human` and waits, indefinitely, holding its slot. Forty-five of them against the default floor of forty is a saturated limiter with a queue, no pushback, no errors, and an idle CPU — the exact clean window a step is bought with.

The first forty-five outputs are tool calls because only first generations happen while everything parks; the completions behind them are unreached, and exist so a sample would finish rather than error if an approval were ever answered.
"""

from inspect_ai import Task, eval_set, task
from inspect_ai.approval import ApprovalPolicy, human_approver
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import Tool, tool

MODEL = "mockllm/model"

SAMPLES = 45


@tool
def echo() -> Tool:
    async def execute(text: str) -> str:
        """Repeat back what it is given.

        Args:
            text: What to repeat.
        """
        return text

    return execute


def pinned(output: ModelOutput, id: str) -> ModelOutput:
    """Give a canned output stable identity across processes.

    `custom_outputs` is a model argument and participates in the task identifier; left random, the capture and the worker enumerate different tasks (see `approval_evalset.py`).
    """
    output.choices[0].message.id = id
    return output


@task
def ramped() -> Task:
    return Task(
        dataset=[
            Sample(id=f"s{index}", input="use the tool", target="hello")
            for index in range(SAMPLES)
        ],
        solver=[use_tools(echo()), generate()],
        approval=[ApprovalPolicy(human_approver(), "*")],
    )


eval_set(
    tasks=[ramped()],
    model=get_model(
        MODEL,
        custom_outputs=[
            *(
                pinned(
                    ModelOutput.for_tool_call(
                        model=MODEL,
                        tool_name="echo",
                        tool_arguments={"text": "hello"},
                        tool_call_id=f"approve-{index}",
                    ),
                    f"call-{index}",
                )
                for index in range(SAMPLES)
            ),
            *(
                pinned(
                    ModelOutput.from_content(model=MODEL, content="hello"),
                    f"done-{index}",
                )
                for index in range(SAMPLES)
            ),
        ],
    ),
    log_dir="logs",
)
