"""Committing desired state, and noticing the definition moved underneath it.

No processes: a manifest is a pydantic model and a hash is a hash. That capture
and drift compute the *same* hash is guaranteed by construction rather than by a
test here — `read_eval_set` calls `definition_hash`, so there is no second
literal to disagree with. What that cannot show is that a tend looks at the same
file capture did, which is an integration property and belongs where a real
capture happens (`tests/schedule/test_tend_live.py`).
"""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_steward._evalset.manifest import (
    DEFAULT_RETRY_ON_ERROR,
    ManifestError,
    definition_hash,
    manifest_digest,
    read_manifest,
    worker_overrides,
    write_manifest,
)

from .._logs import SynthTask, synth_manifest


def test_a_manifest_survives_the_round_trip(tmp_path: Path) -> None:
    manifest = synth_manifest(
        [SynthTask("probe", samples=10), SynthTask("other", args={"n": 1})],
        log_dir="s3://bucket/run/logs",
        max_samples=60,
    )
    path = tmp_path / "state" / "manifest.json"

    write_manifest(manifest, path)

    # the directory is created on the way, because a workspace's .steward/ is
    # disposable and may not be there
    assert read_manifest(path) == manifest


def test_committing_twice_replaces_rather_than_appends(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(synth_manifest([SynthTask("first")]), path)
    write_manifest(synth_manifest([SynthTask("second")]), path)

    assert [task.name for task in read_manifest(path).tasks] == ["second"]
    # the rename leaves nothing behind for a reader to trip over
    assert [child.name for child in tmp_path.iterdir()] == ["manifest.json"]


def test_no_manifest_is_an_answer_rather_than_damage(tmp_path: Path) -> None:
    # what a workspace that has never launched looks like, which the caller
    # turns into "run `steward launch`" rather than into an error
    with pytest.raises(FileNotFoundError):
        read_manifest(tmp_path / "manifest.json")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty"),
        pytest.param("{", id="torn"),
        pytest.param('{"version": 1}', id="missing_everything_else"),
        pytest.param("[]", id="not_an_object"),
    ],
)
def test_something_that_is_not_a_manifest_says_so(content: str, tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ManifestError, match="steward launch"):
        read_manifest(path)


def test_a_manifest_from_a_later_steward_is_refused(tmp_path: Path) -> None:
    # desired state is *input*, and a reader guessing at a schema it does not
    # know drives spawning and archiving: a field it has never heard of is
    # dropped in silence, one it expects is defaulted, and either way a decision
    # gets made about a manifest nobody wrote. The journal reads unknown types
    # because it is history; this is not
    path = tmp_path / "manifest.json"
    manifest = synth_manifest([SynthTask("probe")])
    write_manifest(manifest.model_copy(update={"version": 999}), path)

    with pytest.raises(ManifestError, match="version 999"):
        read_manifest(path)


def test_the_hash_is_of_the_file_and_nothing_else(tmp_path: Path) -> None:
    definition = tmp_path / "evalset.py"
    definition.write_bytes(b"eval_set(tasks=[])\n")

    assert (
        definition_hash(definition)
        == f"sha256:{hashlib.sha256(b'eval_set(tasks=[])\n').hexdigest()}"
    )


def test_any_edit_at_all_changes_the_hash(tmp_path: Path) -> None:
    definition = tmp_path / "evalset.py"
    definition.write_text("eval_set(tasks=[])\n", encoding="utf-8")
    before = definition_hash(definition)

    definition.write_text("eval_set(tasks=[])\n# a comment\n", encoding="utf-8")

    # deliberately blunt: the hash is a nudge to re-capture, not a judgement
    # about whether the edit was semantically interesting
    assert definition_hash(definition) != before


SHAPES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("a moved range of the same size", {"limit": (0, 5)}, {"limit": (5, 10)}),
    ("a reshuffle", {"sample_shuffle": 7}, {"sample_shuffle": 42}),
    ("different ids", {"sample_id": ["a", "b"]}, {"sample_id": ["a", "c"]}),
]


@pytest.mark.parametrize(
    ("one", "other"),
    [(one, other) for _, one, other in SHAPES],
    ids=[case for case, _, _ in SHAPES],
)
def test_two_different_result_sets_do_not_share_a_digest(
    one: dict[str, Any], other: dict[str, Any]
) -> None:
    """The digest is what an acknowledgment is keyed on, so a collision is silent.

    `limit`, `sample_id` and `sample_shuffle` are identity-neutral *and*
    count-neutral — `(0, 5)` and `(5, 10)` are five samples each — so two
    genuinely different result sets hashed identically, and acknowledging the
    first `signoff_ready` quietly covered the second.
    """
    task = SynthTask("probe", samples=5)

    assert manifest_digest(synth_manifest([task], **one)) != manifest_digest(
        synth_manifest([task], **other)
    )


def test_a_redirect_is_a_different_result_set_too() -> None:
    # same samples, every answer different
    task = SynthTask("probe", samples=5)
    plain = synth_manifest([task])
    elsewhere = plain.model_copy(
        update={"overrides": EvalSetOverrides(model_base_url="https://other.example")}
    )

    assert manifest_digest(plain) != manifest_digest(elsewhere)


def test_the_same_run_said_twice_is_one_digest() -> None:
    # the property the whole thing rests on: re-capturing an unedited definition
    # must not invalidate an acknowledgment somebody already gave
    task = SynthTask("probe", samples=5)

    assert manifest_digest(synth_manifest([task], limit=(0, 5))) == manifest_digest(
        synth_manifest([task], limit=(0, 5))
    )


def test_an_override_and_a_definition_saying_the_same_thing_agree() -> None:
    """The digest is over *effective* values, so where a run ends up is what counts."""
    task = SynthTask("probe", samples=5)
    from_definition = synth_manifest([task], limit=3)
    from_override = synth_manifest([task]).model_copy(
        update={"overrides": EvalSetOverrides(limit=3)}
    )

    assert manifest_digest(from_definition) == manifest_digest(from_override)


def test_a_worker_retries_samples_where_nobody_asked() -> None:
    """Steward's sample-retry default.

    Inspect leaves `retry_on_error` unset, which under an unattended fleet turns
    a provider blip into a decision somebody answers in the morning.
    """
    manifest = synth_manifest([SynthTask("probe", samples=5)])

    resolved = worker_overrides(manifest)

    assert resolved is not None
    assert resolved.retry_on_error == DEFAULT_RETRY_ON_ERROR


@pytest.mark.parametrize("asked", [0, 7], ids=["none", "seven"])
def test_a_definition_that_named_its_own_retries_is_left_alone(asked: int) -> None:
    """The default never displaces the definition, `0` included.

    How many attempts a sample deserves is a property of the eval, so a worker
    is sent nothing and honours what the definition passed. A definition asking
    for none is asking to see the failure.
    """
    manifest = synth_manifest([SynthTask("probe", samples=5)], retry_on_error=asked)

    resolved = worker_overrides(manifest)

    assert resolved is None or resolved.retry_on_error is None


def test_a_run_that_named_its_own_retries_keeps_them() -> None:
    """An override is somebody typing a number for this run, and outranks the default."""
    manifest = synth_manifest([SynthTask("probe", samples=5)]).model_copy(
        update={"overrides": EvalSetOverrides(retry_on_error=1, epochs=2)}
    )

    resolved = worker_overrides(manifest)

    assert resolved is not None
    assert resolved.retry_on_error == 1
    assert resolved.epochs == 2
