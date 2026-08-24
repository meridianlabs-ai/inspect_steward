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

import pytest
from inspect_steward._evalset.manifest import (
    ManifestError,
    definition_hash,
    read_manifest,
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
