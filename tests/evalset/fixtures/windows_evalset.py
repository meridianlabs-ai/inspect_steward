"""A definition that records its resolved context windows, the way veevals does.

Steward sets `STEWARD_CAPTURE_WINDOWS` to a sidecar path when it captures; a definition that
understands the channel writes each model's resolved window there before `eval_set()`. This
fixture stands in for veevals so the read path can be exercised without it — the real emitter is
`veevals.campaign.execute`.
"""

import json
import os
from pathlib import Path

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import exact
from inspect_ai.solver import generate

if path := os.environ.get("STEWARD_CAPTURE_WINDOWS"):
    Path(path).write_text(json.dumps({"mockllm/model": 1_048_576}) + "\n")


@task
def addition() -> Task:
    return Task(
        dataset=[Sample(input="1+1", target="2")],
        solver=[generate()],
        scorer=exact(),
    )


eval_set(
    tasks=[addition()],
    model="mockllm/model",
    log_dir="logs",
)
