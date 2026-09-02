"""The rehearsal's slice is taken inside the run's own, never in place of it.

**The three selectors move as one.** `eval()` refuses `sample_id` beside `limit` or `sample_shuffle`, so every layer that combines two sources of them takes all three from whichever source spoke — which makes the obvious truncation destructive rather than additive. A bare `limit=2` on top of a run selecting `(100, 200)` clears the window and runs samples 0 and 1: a rehearsal of samples the run will never touch, reported against the intended manifest's digest as successfully rehearsed.

Read off what is *in force* rather than off the override alone, because a definition calling `eval_set(limit=(100, 200))` records that in `options` with no override at all.
"""

from typing import Any

import pytest
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_steward._evalset.manifest import Manifest
from inspect_steward._smoke.run import Plan, overrides, selection

from .._logs import SynthTask, synth_manifest

ADDITION = SynthTask("addition", samples=500)


def manifest(**options: Any) -> Manifest:
    return synth_manifest([ADDITION], **options)


def overridden(**named: Any) -> Manifest:
    return manifest().model_copy(update={"overrides": EvalSetOverrides(**named)})


SLICES = [
    ("nothing selected", manifest(), {"limit": 2}),
    ("a count", manifest(limit=10), {"limit": 2}),
    ("a count under the slice", manifest(limit=1), {"limit": 1}),
    ("a window", manifest(limit=[100, 200]), {"limit": (100, 102)}),
    (
        "a window narrower than the slice",
        manifest(limit=[100, 101]),
        {"limit": (100, 101)},
    ),
    ("named ids", manifest(sample_id=["a", "b", "c"]), {"sample_id": ["a", "b"]}),
    ("one named id", manifest(sample_id="a"), {"sample_id": "a"}),
    ("a shuffle", manifest(sample_shuffle=42), {"limit": 2, "sample_shuffle": 42}),
    ("an override window", overridden(limit=(300, 400)), {"limit": (300, 302)}),
]
"""What the run selects, and the slice a two-sample rehearsal takes inside it."""


@pytest.mark.parametrize(
    ("what", "run", "expected"),
    SLICES,
    ids=[row[0] for row in SLICES],
)
def test_the_slice_is_taken_inside_the_runs_own(
    what: str, run: Manifest, expected: dict[str, Any]
) -> None:
    taken = selection(run, 2)

    assert {name: value for name, value in taken.items() if value is not None} == (
        expected
    )


def test_naming_ids_never_names_a_limit_beside_them() -> None:
    # `eval()` refuses the combination outright, so producing one would be a
    # document the run rejects rather than a slice it narrows
    taken = selection(manifest(sample_id=["a", "b", "c"]), 2)

    assert taken["limit"] is None
    assert taken["sample_shuffle"] is None


def test_a_shuffle_survives_the_truncation() -> None:
    """Which samples the front of a shuffled dataset holds is part of what the run is.

    Dropped, the rehearsal runs the unshuffled front — different samples, under
    the same digest.
    """
    taken = selection(manifest(sample_shuffle=7), 2)

    assert taken["sample_shuffle"] == 7
    assert taken["limit"] == 2


def test_what_the_workers_are_finally_told() -> None:
    # end to end through the merge, which is where the loss happened: the
    # worker's container names a selector, so it takes all three -- and now all
    # three are ones this rehearsal computed rather than two it wiped
    run = manifest(limit=[100, 200], sample_shuffle=5)
    plan = Plan(
        manifest=run,
        log_dir="/tmp/smoke",
        scan_id="rehearsal",
        scan_dir=None,
        scanners=(),
        samples=2,
        cap=0,
    )

    told = overrides(plan, run.overrides)

    assert told.limit == (100, 102)
    assert told.sample_shuffle == 5
    assert told.sample_id is None
    assert told.log_dir == "/tmp/smoke"
