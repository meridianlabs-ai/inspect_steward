from inspect_flow import FlowSpec, FlowTask


def spec(difficulty: str = "easy") -> FlowSpec:
    return FlowSpec(
        log_dir="logs",
        tasks=[
            FlowTask(
                name="tasks.py@sweep",
                args={"difficulty": difficulty},
                model="mockllm/model",
            ),
        ],
    )
