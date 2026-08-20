from inspect_flow import FlowSpec, FlowTask

FlowSpec(
    log_dir="logs",
    tasks=[
        FlowTask(name="tasks.py@addition", model="mockllm/model"),
    ],
)
