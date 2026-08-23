"""Tasks that vary across every field participating in `task_identifier`.

Every task here shares one name and one (empty) args set, differing from the
baseline in exactly one identity-relevant field. That is deliberate: it makes
the identifiers differ only in the part of the hash the dimension feeds, so a
field that silently dropped out of the computation shows up as a *collision*
rather than passing a round trip vacuously.

Tasks are constructed directly rather than through `@task`, since a registry
function's identity travels with its name and args and would mask the very
thing being varied.
"""

from inspect_ai import Task, eval_set
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    GenerateConfig,
    ModelCost,
    ModelInfo,
    get_model,
    set_model_info,
)
from inspect_ai.scorer import exact
from inspect_ai.solver import generate, system_message
from inspect_ai.util import TokenLimit

NAME = "probe"

# a `cost_limit` refuses to run without pricing for every model, and mockllm
# has none. Registering it here rather than in the test is the point: this is a
# definition side effect, and worker mode preserves it precisely because every
# worker re-executes the definition.
for model in ("mockllm/model", "mockllm/model2"):
    set_model_info(
        model,
        ModelInfo(
            cost=ModelCost(
                input=1.0, output=1.0, input_cache_write=1.0, input_cache_read=1.0
            )
        ),
    )


def probe(**kwargs: object) -> Task:
    """A one-sample task, varied by whatever identity field is passed."""
    defaults: dict[str, object] = dict(
        name=NAME,
        dataset=[Sample(input="1+1", target="2")],
        solver=[generate()],
        scorer=exact(),
        model="mockllm/model",
    )
    return Task(**(defaults | kwargs))  # type: ignore[arg-type]


eval_set(
    tasks=[
        probe(),
        # model segment of the identifier
        probe(model="mockllm/model2"),
        # model_args_for_log(task.model.model_args) vs eval.model_args
        probe(model=get_model("mockllm/model", probe_arg="varied")),
        # model_roles_to_model_roles_config(...) vs eval.model_roles
        probe(model_roles={"grader": "mockllm/model2"}),
        # task.task.version vs eval.task_version
        probe(version=2),
        # resolve_plan(...) vs log.plan — steps and their params
        probe(solver=[system_message("varied"), generate()]),
        # task.config.merge(eval_set_args.config) vs eval.model_generate_config
        probe(config=GenerateConfig(temperature=0.5)),
        # the execution limits, each read from task.task.* vs eval.config.*
        probe(message_limit=10),
        probe(token_limit=1000),
        # "output:<n>" rather than a bare int — a distinct encoding
        probe(token_limit=TokenLimit(tokens=1000, type="output")),
        probe(token_limit=TokenLimit(tokens=1000, type="(input * 0.1) + output")),
        probe(turn_limit=5),
        probe(time_limit=60),
        probe(working_limit=30),
        probe(cost_limit=1.0),
    ],
    log_dir="logs",
)
