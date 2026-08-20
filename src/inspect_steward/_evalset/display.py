from collections import Counter, defaultdict
from typing import Any, Sequence

from inspect_ai._eval.eval_set_manifest import EvalSetCaptureTask


def compute_display_keys(tasks: Sequence[EvalSetCaptureTask]) -> list[str]:
    """Compute unique human-facing display keys for a set of resolved tasks.

    Keys have the form `task[solver]@model`. Tasks that collide on that form are disambiguated by the args that differ between them (e.g. `task[solver]@model (difficulty=hard)`), then by differing model args, and finally by ordinal position (`#n`) for collisions the capture fields cannot distinguish (e.g. config-only sweeps).

    Args:
        tasks: Resolved tasks from a capture manifest.

    Returns:
        Display keys, index-aligned with `tasks` and unique within them.
    """
    base_keys = [
        f"{task.display_name or task.name}[{task.solver or 'default'}]@{task.model}"
        for task in tasks
    ]

    # group colliding tasks by base key
    groups: dict[str, list[int]] = defaultdict(list)
    for index, base in enumerate(base_keys):
        groups[base].append(index)

    keys = list(base_keys)
    for base, indices in groups.items():
        if len(indices) == 1:
            continue
        group = [tasks[i] for i in indices]
        suffixes = _args_suffixes([t.args for t in group])
        if suffixes is None:
            suffixes = _args_suffixes([t.args_full or {} for t in group])
        if suffixes is None:
            suffixes = _args_suffixes([t.model_args for t in group])
        if suffixes is None:
            suffixes = [f"#{n}" for n in range(1, len(group) + 1)]
        for i, suffix in zip(indices, suffixes, strict=True):
            keys[i] = f"{base} {suffix}"

    # disambiguation suffixes could in principle collide with another task's
    # literal base key (e.g. a task actually named "t@m #1")
    duplicates = sorted(key for key, n in Counter(keys).items() if n > 1)
    if duplicates:
        raise ValueError(
            f"Unable to compute unique display keys; colliding keys: {duplicates}"
        )
    return keys


def _args_suffixes(args: list[dict[str, Any]]) -> list[str] | None:
    """Suffixes rendering the args that differ across a group (`None` if the differing args do not uniquely distinguish every member)."""
    differing = sorted(
        {
            key
            for group_args in args
            for key in group_args
            if any(other.get(key) != group_args.get(key) for other in args)
        }
    )
    if not differing:
        return None
    suffixes = [
        "(" + ", ".join(f"{key}={group_args.get(key)}" for key in differing) + ")"
        for group_args in args
    ]
    if len(set(suffixes)) != len(suffixes):
        return None
    return suffixes
