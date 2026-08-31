from collections import Counter, defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class KeyParts:
    """One row's display key, still in pieces.

    `full` is the manifest's own key — unique within the manifest by construction, and therefore the terminating fallback when nothing shorter distinguishes two rows.
    """

    name: str
    solver: str | None = None
    model: str | None = None
    full: str = ""

    def rendered(self, *, solver: bool, model: bool) -> str:
        text = self.name
        if solver and self.solver is not None:
            text += f"[{self.solver}]"
        if model and self.model is not None:
            text += f"@{self.model}"
        return text


@dataclass(frozen=True)
class ShortKeys:
    """Keys as short as they can be while still naming one row each."""

    keys: list[str]
    """Index-aligned with the parts they were computed from."""

    model: str | None
    """The model every row shares and none of them shows, when there is one.

    Worth stating once beside the table: a sweep that is entirely one model is a fact about the run. A universally shared *solver* is not — `[default]` on every row is the absence of information, so it is elided silently.
    """


def shorten_keys(parts: Sequence[KeyParts]) -> ShortKeys:
    """Shorten display keys against the rows actually being shown.

    `compute_display_keys` renders only the args that *differ* when two tasks collide; this is the same rule one level up, and for the same reason — a column repeating `[default]@openai/gpt-5` on every line spends thirty characters saying nothing, which is the difference between a table that fits in a 76-column Slack code block and one that does not.

    **Expanding rather than eliding**, so the result is the shortest thing that still works: every row starts at its task name and gains `@model`, then `[solver]`, then its full key, only where a name collides — and **per colliding group**, so one row can read `alpha` while two others read `beta@gpt-5` and `beta@claude`.

    Args:
        parts: One entry per row being rendered, in render order. An orphan has no manifest row, so it carries a bare name and sits out every expansion.

    Returns:
        The keys, and the model they all share if they do and none of them shows it.
    """
    shown = [(False, False)] * len(parts)

    # model first: a sweep varies by model far more often than by solver, so
    # trying it first is what keeps the common collision one segment wide
    for segment in ("model", "solver"):
        for group in _colliding(parts, shown):
            trial = [
                (
                    (True, shown[member][1])
                    if segment == "model"
                    else (shown[member][0], True)
                )
                for member in group
            ]
            rendered = {
                parts[member].rendered(model=model, solver=solver)
                for member, (model, solver) in zip(group, trial, strict=True)
            }
            # take the segment only if it tells at least two of them apart;
            # adding a model every row shares would cost width and say nothing
            if len(rendered) > 1:
                for member, flags in zip(group, trial, strict=True):
                    shown[member] = flags

    keys = [
        part.rendered(model=model, solver=solver)
        for part, (model, solver) in zip(parts, shown, strict=True)
    ]
    # whatever two rows still share cannot be shortened at all, and the manifest
    # already computed a key that is unique across every task in the run
    duplicated = {key for key, count in Counter(keys).items() if count > 1}
    keys = [
        (part.full or key) if key in duplicated else key
        for part, key in zip(parts, keys, strict=True)
    ]

    # every row, not every row that has one: an orphan's model is unknown
    # rather than shared, and *all tasks ran against X* has to be true of the
    # table it sits under
    models = {part.model for part in parts}
    universal = models.pop() if len(models) == 1 else None
    if universal is not None and any(model for model, _ in shown):
        # somebody is showing it, so it is not a shared fact to state once
        universal = None
    return ShortKeys(keys=keys, model=universal)


def _colliding(
    parts: Sequence[KeyParts], shown: list[tuple[bool, bool]]
) -> list[list[int]]:
    """Row indices grouped by what they currently render as, collisions only."""
    groups: dict[str, list[int]] = defaultdict(list)
    for index, (part, (model, solver)) in enumerate(zip(parts, shown, strict=True)):
        groups[part.rendered(model=model, solver=solver)].append(index)
    return [members for members in groups.values() if len(members) > 1]
