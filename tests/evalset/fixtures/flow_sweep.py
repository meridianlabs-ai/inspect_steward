"""Two tasks, so that a flow definition can be fanned out over two workers."""

from inspect_flow import FlowSpec, FlowTask

FlowSpec(
    log_dir="logs",
    tasks=[
        FlowTask(
            name="tasks.py@sweep", args={"difficulty": "easy"}, model="mockllm/model"
        ),
        FlowTask(
            name="tasks.py@sweep", args={"difficulty": "hard"}, model="mockllm/model"
        ),
    ],
)
