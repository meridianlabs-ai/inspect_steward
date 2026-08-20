# The hawk package is an optional dependency (and requires Python >= 3.13),
# so its imports are function-level and unresolvable in a default dev install.
# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

"""Conforming program for Hawk eval set configs.

Parses a config with hawk's own `EvalSetConfig`, crosses the grid (tasks x solvers/agents x models), and makes the `eval_set()` call — the same lowering hawk's runner performs, minus its platform concerns. Run by Steward with the eval-set capture (and, in the future, selection) environment applied.

Platform-managed config fields are ignored when running directly (they belong to hawk deployments): `runner`, `isolation`, `checkpoint`, `monitor`, `acp_server`, `approval_timeout_minutes`, `human_eval`, `scan`, and `secrets`. Packages referenced by the config must already be installed — Steward does not install them.

The config carries no `log_dir` (hawk's infrastructure supplies one); this runner uses `logs/<eval_set_id or name>`, mirroring `hawk local`.
"""

import argparse
from typing import Any

import yaml

# hawk config keys that are platform concerns (not forwarded to eval_set)
_PLATFORM_FIELDS = {
    "runner",
    "isolation",
    "checkpoint",
    "monitor",
    "acp_server",
    "approval_timeout_minutes",
    "human_eval",
    "scan",
    "secrets",
    "packages",
}

# hawk reserves these (scan-shaped) keys and rejects them itself
_RESERVED_EXTRA_KEYS = {"scanner", "scans"}


def main() -> None:
    from inspect_ai import eval_set

    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Path to the hawk eval set config (.yaml).")
    parsed = parser.parse_args()

    with open(parsed.file) as f:
        config_data = yaml.safe_load(f)
    config = _validate_config(config_data)

    tasks = _cross_tasks(config)
    log_dir = f"logs/{config.eval_set_id or config.name or 'eval-set'}"
    eval_set(
        tasks=tasks,
        log_dir=log_dir,
        model_roles=_model_roles(config),
        eval_set_id=config.eval_set_id,
        **_eval_set_options(config),
    )


def _validate_config(config_data: Any) -> Any:
    from hawk.core.types.evals import EvalSetConfig

    return EvalSetConfig.model_validate(config_data)


def _qualified_name(package_config: Any, item: Any) -> str:
    # inspect-ai builtins use bare names; package items are namespaced
    if package_config.package == "inspect-ai":
        return str(item.name)
    return f"{package_config.name}/{item.name}"


def _create_model(package_config: Any, item: Any) -> Any:
    from inspect_ai.model import GenerateConfig, get_model

    name = _qualified_name(package_config, item)
    args = (
        item.args.model_dump(exclude_none=True, exclude_unset=True) if item.args else {}
    )
    config = GenerateConfig.model_validate(args.pop("config", {}))
    return get_model(name, config=config, **args)


def _create_solvers(config: Any) -> list[Any]:
    """Resolve the solver axis: solvers plus agents converted via `as_solver`."""
    from inspect_ai._eval.loader import solver_from_spec
    from inspect_ai.agent import as_solver
    from inspect_ai.solver import SolverSpec
    from inspect_ai.util import registry_create

    solvers: list[Any] = []
    for package_config in config.solvers or []:
        for item in package_config.items:
            solvers.append(
                solver_from_spec(
                    SolverSpec(
                        solver=_qualified_name(package_config, item),
                        args=item.args or {},
                    )
                )
            )
    for package_config in config.agents or []:
        for item in package_config.items:
            agent = registry_create(
                "agent", _qualified_name(package_config, item), **(item.args or {})
            )
            solvers.append(as_solver(agent))
    return solvers


def _cross_tasks(config: Any) -> list[Any]:
    """The task grid: tasks x (solvers + agents) x models, one Task per combination (mirrors hawk's runner)."""
    from inspect_ai import task_with
    from inspect_ai.util import registry_create

    solvers = _create_solvers(config)
    models: list[Any] = [
        _create_model(package_config, item)
        for package_config in config.models or []
        for item in package_config.items
    ]

    tasks: list[Any] = []
    for package_config in config.tasks:
        for item in package_config.items:
            for solver in solvers or [None]:
                for model in models or [None]:
                    task = registry_create(
                        "task",
                        _qualified_name(package_config, item),
                        **(item.args or {}),
                    )
                    overrides: dict[str, Any] = {}
                    if solver is not None:
                        overrides["solver"] = solver
                    if model is not None:
                        overrides["model"] = model
                    if item.sample_ids:
                        overrides["dataset"] = task.dataset.filter(
                            lambda sample: sample.id in item.sample_ids  # noqa: B023
                        )
                    tasks.append(task_with(task, **overrides) if overrides else task)
    return tasks


def _model_roles(config: Any) -> dict[str, Any] | None:
    if not config.model_roles:
        return None
    return {
        role: _create_model(role_config, role_config.items[0])
        for role, role_config in config.model_roles.items()
    }


def _eval_set_options(config: Any) -> dict[str, Any]:
    """Map hawk config fields (and `extra=allow` passthrough keys) to `eval_set()` kwargs."""
    from inspect_ai import Epochs

    options: dict[str, Any] = {}

    fields = config.model_dump(exclude_none=True, exclude_unset=True)
    for key in (
        "score",
        "sample_shuffle",
        "message_limit",
        "token_limit",
        "time_limit",
        "working_limit",
        "cost_limit",
        "retry_attempts",
        "log_realtime",
        "log_model_api",
        "log_images",
        "tags",
        "metadata",
    ):
        if key in fields:
            options[key] = fields[key]

    if "limit" in fields:
        limit = fields["limit"]
        options["limit"] = tuple(limit) if isinstance(limit, list) else limit

    if config.epochs is not None:
        if isinstance(config.epochs, int):
            options["epochs"] = config.epochs
        else:
            options["epochs"] = Epochs(config.epochs.epochs, config.epochs.reducer)

    if isinstance(config.approval, str):
        options["approval"] = config.approval

    if config.adaptive_connections:
        options["adaptive_connections"] = config.adaptive_connections

    # extra="allow" passthrough: unknown top-level keys forward to eval_set()
    for key, value in (config.model_extra or {}).items():
        if key not in _RESERVED_EXTRA_KEYS and key not in _PLATFORM_FIELDS:
            options[key] = value

    return options


if __name__ == "__main__":
    main()
