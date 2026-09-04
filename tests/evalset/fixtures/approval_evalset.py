"""A definition whose one sample parks waiting for an operator to approve a tool call.

The only fixture here that holds a worker open without a fault marker: what
holds it is the eval doing exactly what it was asked to do. `approver: human`
plus a model that calls a tool is a sample that stops and waits, indefinitely,
holding its slot — which is the condition step 20 is about.

Deterministic without a sleep for the same reason `faulty_evalset.py` is: the
hold is a *state*. The model's output is canned, so the tool call happens on the
first generation, and the approval never resolves because nobody attaches.
"""

from inspect_ai import Task, eval_set, task
from inspect_ai.approval import ApprovalPolicy, human_approver
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import Tool, tool

MODEL = "mockllm/model"


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

    Both the assistant message and the tool call default to a fresh uuid, and
    `custom_outputs` is a *model argument* — which participates in the task
    identifier. Left random, the capture and the worker enumerate different
    tasks and the worker refuses the selection it was handed, which surfaces as
    a startup error rather than as anything to do with approvals.
    """
    output.choices[0].message.id = id
    return output


@task
def approved() -> Task:
    return Task(
        dataset=[Sample(id="one", input="use the tool", target="hello")],
        solver=[use_tools(echo()), generate()],
        approval=[ApprovalPolicy(human_approver(), "*")],
    )


eval_set(
    tasks=[approved()],
    model=get_model(
        MODEL,
        custom_outputs=[
            pinned(
                ModelOutput.for_tool_call(
                    model=MODEL,
                    tool_name="echo",
                    tool_arguments={"text": "hello"},
                    tool_call_id="approve-me",
                ),
                "call",
            ),
            # never reached while nobody answers; here so that the sample would
            # finish rather than error if one ever did, which is what makes the
            # park the only thing this fixture is holding on
            pinned(ModelOutput.from_content(model=MODEL, content="hello"), "done"),
        ],
    ),
    log_dir="logs",
)
