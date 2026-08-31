from typing import Any

import pytest
from inspect_ai._eval.eval_set_manifest import EvalSetCaptureTask
from inspect_steward._evalset.display import compute_display_keys


def make_task(
    name: str = "my_task",
    model: str = "mockllm/model",
    solver: str | None = "generate",
    args: dict[str, Any] | None = None,
    args_full: dict[str, Any] | None = None,
    model_args: dict[str, Any] | None = None,
    display_name: str | None = None,
    sequence: int = 0,
) -> EvalSetCaptureTask:
    return EvalSetCaptureTask(
        name=name,
        display_name=display_name,
        file="evalset.py",
        args=args or {},
        args_full=args_full,
        args_hash="hash",
        solver=solver,
        model=model,
        model_args=model_args or {},
        sequence=sequence,
        identifier=f"id-{name}-{model}-{sequence}",
        samples=1,
        epochs=1,
    )


def test_display_keys_no_collision() -> None:
    tasks = [
        make_task(name="alpha"),
        make_task(name="beta"),
        make_task(name="alpha", model="mockllm/model2"),
    ]
    assert compute_display_keys(tasks) == [
        "alpha[generate]@mockllm/model",
        "beta[generate]@mockllm/model",
        "alpha[generate]@mockllm/model2",
    ]


def test_display_keys_solver_and_display_name() -> None:
    tasks = [make_task(solver=None, display_name="My Task")]
    assert compute_display_keys(tasks) == ["My Task[default]@mockllm/model"]


def test_display_keys_args_disambiguation() -> None:
    tasks = [
        make_task(args={"difficulty": "easy", "shared": 1}),
        make_task(args={"difficulty": "hard", "shared": 1}, sequence=1),
    ]
    assert compute_display_keys(tasks) == [
        "my_task[generate]@mockllm/model (difficulty=easy)",
        "my_task[generate]@mockllm/model (difficulty=hard)",
    ]


def test_display_keys_args_full_disambiguation() -> None:
    # passed args identical; defaults (args_full) differ
    tasks = [
        make_task(args={}, args_full={"level": 1}),
        make_task(args={}, args_full={"level": 2}, sequence=1),
    ]
    assert compute_display_keys(tasks) == [
        "my_task[generate]@mockllm/model (level=1)",
        "my_task[generate]@mockllm/model (level=2)",
    ]


def test_display_keys_model_args_disambiguation() -> None:
    tasks = [
        make_task(model_args={"port": 8000}),
        make_task(model_args={"port": 8001}, sequence=1),
    ]
    assert compute_display_keys(tasks) == [
        "my_task[generate]@mockllm/model (port=8000)",
        "my_task[generate]@mockllm/model (port=8001)",
    ]


def test_display_keys_ordinal_fallback() -> None:
    # nothing in the capture fields distinguishes these (e.g. config sweep)
    tasks = [make_task(), make_task(sequence=1), make_task(sequence=2)]
    assert compute_display_keys(tasks) == [
        "my_task[generate]@mockllm/model #1",
        "my_task[generate]@mockllm/model #2",
        "my_task[generate]@mockllm/model #3",
    ]


def test_display_keys_cross_group_collision_error() -> None:
    # an ordinal-suffixed key collides with another task's literal base key
    tasks = [
        make_task(name="t", model="m"),
        make_task(name="t", model="m", sequence=1),
        make_task(name="t", model="m #1", sequence=2),
    ]
    with pytest.raises(ValueError, match="colliding keys"):
        compute_display_keys(tasks)


def test_display_keys_unique_for_mixed_set() -> None:
    tasks = [
        make_task(name="alpha"),
        make_task(name="alpha", sequence=1),
        make_task(name="alpha", args={"x": 1}, sequence=2),
        make_task(name="beta", model="mockllm/model2"),
    ]
    keys = compute_display_keys(tasks)
    assert len(set(keys)) == len(tasks)
