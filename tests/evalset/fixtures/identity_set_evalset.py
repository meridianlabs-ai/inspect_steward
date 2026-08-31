"""Identity fields supplied at the `eval_set()` level rather than per task.

These cannot be varied per task within one call, so this fixture exercises the
round trip rather than participation: `task_identifier` merges eval-set args
into a `ResolvedTask`'s hash but reads them back off `eval.config` and
`log.plan` when given an `EvalLog`, and nothing else checks that merge survives
the crossing.

The second task deliberately sets limits of its own that the eval-set values
shadow — the case where capture and the log could most plausibly disagree
about which value won. Its `version` is what keeps the two distinguishable
once the shadowing has flattened everything else.
"""

from inspect_ai import Task, eval_set
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelCost, ModelInfo, set_model_info
from inspect_ai.scorer import exact
from inspect_ai.solver import generate, system_message
from inspect_ai.util import TokenLimit

# see identity_evalset.py — a cost_limit needs pricing, and registering it in
# the definition is what puts it in front of every worker
set_model_info(
    "mockllm/model",
    ModelInfo(
        cost=ModelCost(
            input=1.0, output=1.0, input_cache_write=1.0, input_cache_read=1.0
        )
    ),
)


def probe(**kwargs: object) -> Task:
    defaults: dict[str, object] = dict(
        name="probe",
        dataset=[Sample(input="1+1", target="2")],
        solver=[generate()],
        scorer=exact(),
    )
    return Task(**(defaults | kwargs))  # type: ignore[arg-type]


eval_set(
    tasks=[
        probe(version=1),
        # every limit below is shadowed by the eval-set value
        probe(version=2, message_limit=10, token_limit=1000, time_limit=60),
    ],
    model="mockllm/model",
    # replaces each task's own plan
    solver=[system_message("eval-set override"), generate()],
    # generate config, which reaches the identifier through the plan's config
    temperature=0.3,
    message_limit=20,
    token_limit=TokenLimit(tokens=2000, type="output"),
    turn_limit=7,
    time_limit=90,
    working_limit=45,
    cost_limit=2.0,
    log_dir="logs",
)
