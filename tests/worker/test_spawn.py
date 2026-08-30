"""Spawning a worker, and what it leaves behind.

A process launch costs about three seconds, so the tests that spend one say
what it buys. The document, the stem, and the eval set id are settled without
launching anything; only the claims that require a real process — correlation,
detachment, a shared directory, resume, and a death before the boundary — pay.
"""

import os
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from inspect_ai._eval.eval_set_manifest import INSPECT_EVAL_SET_CAPTURE
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_ai._eval.eval_set_selection import (
    EVAL_SET_SELECTION_VERSION,
    read_eval_set_selection,
)
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward import read_eval_set
from inspect_steward._util.jsonl import read_events
from inspect_steward._worker import resolve_eval_set_id, worker_selection, worker_stem

from ._fleet import (
    EVAL_SET_ID,
    FIXTURES,
    action,
    fleet,
    landed,
    output,
    spawn_all,
    wait,
)


def test_the_selection_document_is_one_inspect_accepts(tmp_path: Path) -> None:
    # Steward writes this document by hand -- there is no upstream writer -- so
    # the guard is upstream's own reader, which enforces the declared version,
    # the field-minimum-version rule, and both overrides' sanity
    built = worker_selection(
        action("file.py@task#hash/mockllm/model/x", resume="logs/prior.eval"),
        eval_set_id=EVAL_SET_ID,
        log_dir="s3://bucket/logs",
    )
    assert built.version == EVAL_SET_SELECTION_VERSION
    path = tmp_path / "selection.json"
    path.write_text(built.model_dump_json(exclude_none=True))
    assert read_eval_set_selection(str(path)) == built


def test_a_worker_inherits_the_runs_overrides_and_keeps_its_own(
    tmp_path: Path,
) -> None:
    """One container per worker, merged from the run's and this worker's.

    Inspect would take a run-wide document by environment variable as readily,
    and Steward does not use it: a worker's overrides would then live in two
    places, one of them a file under `.steward/` this design tells people they
    may delete. The three values that differ between workers are applied over
    the run's, because they are the ones a run cannot know.
    """
    built = worker_selection(
        action("id-a", "id-b"),
        eval_set_id=EVAL_SET_ID,
        log_dir="s3://bucket/logs",
        overrides=EvalSetOverrides(epochs=3, max_tasks=99, log_dir="s3://elsewhere"),
    )

    assert built.overrides is not None
    # the run's, which no worker has a reason to disagree with
    assert built.overrides.epochs == 3
    # and this worker's, which are the whole point of a per-worker container
    assert built.overrides.max_tasks == 2
    assert built.overrides.log_dir == "s3://bucket/logs"


def test_a_packed_selection_names_every_task_and_its_own_concurrency(
    tmp_path: Path,
) -> None:
    """Several tasks in one process, and the one override that makes them run.

    `max_tasks` is written because leaving it out is not neutral: `eval_set()`
    fills its own default in below the selection branch, so an unset one falls
    through to `eval()`'s rule — one task at a time for a single model — and the
    batch would run sequentially with nobody having chosen that.
    """
    built = worker_selection(
        action("id-a", "id-b", "id-c"),
        eval_set_id=EVAL_SET_ID,
        log_dir="s3://bucket/logs",
    )

    assert [task.identifier for task in built.tasks] == ["id-a", "id-b", "id-c"]
    assert built.overrides is not None
    assert built.overrides.max_tasks == 3
    # and upstream's own reader accepts it, which is what the version and the
    # override sanity rules are enforced by
    path = tmp_path / "packed.json"
    path.write_text(built.model_dump_json(exclude_none=True))
    assert read_eval_set_selection(str(path)) == built


def test_a_selection_carries_the_facets_that_let_a_worker_skip_the_rest(
    tmp_path: Path,
) -> None:
    """Steward's whole half of early pruning: two fields per task, from the manifest.

    A worker resolves the entire eval set to find the tasks it was given, and
    constructing a task loads its dataset — so without these it pays for every
    dataset in the set. They are an optimization and nothing decides on them:
    `identifier` still says what runs.

    Written for **every** task, because inspect prunes only against a complete
    set. A selection describing three of its four tasks would prune the fourth.
    """
    built = worker_selection(
        action("id-a", "id-b"),
        eval_set_id=EVAL_SET_ID,
        log_dir="s3://bucket/logs",
    )

    assert [(t.registry_name, t.args_hash) for t in built.tasks] == [
        ("task0", "hash0"),
        ("task1", "hash1"),
    ]
    # and upstream reads it back, which is what enforces the version the facets
    # arrived in -- they cannot be sent at a version that does not know them
    path = tmp_path / "faceted.json"
    path.write_text(built.model_dump_json(exclude_none=True))
    assert read_eval_set_selection(str(path)) == built


def test_an_orphan_carries_no_facets_rather_than_invented_ones() -> None:
    """A task with no manifest row has no registry name to send, and must not guess.

    Sending a wrong facet is worse than sending none: none disables pruning for
    that worker, while a wrong one prunes the task it was meant to describe. The
    absence is what `_spawn` produces for an orphan, whose row left the manifest.
    """
    built = worker_selection(
        action("id-orphan", facets=False),
        eval_set_id=EVAL_SET_ID,
        log_dir="s3://bucket/logs",
    )

    assert [(t.registry_name, t.args_hash) for t in built.tasks] == [(None, None)]


def test_packing_leaves_a_single_task_worker_byte_for_byte_as_it_was() -> None:
    """The default width must not move when a wider one becomes possible.

    The stem names a live worker's selection document and the entry the record
    folds on, and `STEWARD_TASK` is what the process-table scan reads to name a
    worker whose `.steward/` has been deleted. Both now carry a list — so the
    guard worth having is that a list of one is spelled exactly as one was.
    """
    one = action("only-me", key="k")

    assert worker_stem(one) == f"k_{sha256(b'only-me').hexdigest()[:8]}_1"
    assert "\n".join(one.identifiers) == "only-me"


def test_a_packed_stem_says_how_many_and_distinguishes_the_batch() -> None:
    # named after its first task and countable at a glance, but keyed on all of
    # them: two batches sharing a first task are different workers
    packed = worker_stem(action("a", "b", key="k"))

    assert packed.startswith("k-plus1_")
    assert packed != worker_stem(action("a", "c", key="k"))
    assert packed != worker_stem(action("a", key="k"))
    # order is not identity: the same batch dealt differently is the same batch
    assert packed == worker_stem(action("b", "a", key="k"))


def test_a_worker_stem_separates_what_a_display_key_would_merge() -> None:
    # two keys that sanitize to the same string, which punctuation in an
    # argument sweep produces
    assert worker_stem(action("a", key="task@model (n=1)")) != worker_stem(
        action("b", key="task@model [n=1]")
    )
    # and a retry keeps its own evidence rather than overwriting the attempt it
    # replaced
    assert worker_stem(action("a", key="k")) != worker_stem(
        action("a", key="k", attempt=2)
    )


def test_a_second_worker_never_takes_a_stem_already_in_use(tmp_path: Path) -> None:
    """The attempt number is a decision-layer estimate; the stem cannot be.

    `SpawnWorker.attempt` counts the logs on disk and the in-flight record, and
    both are disposable — two landed logs plus a deleted `inflight.jsonl` would
    number the next attempt 3 when 3 has already been used. That is not a
    cosmetic clash: the stem names the selection document a live worker is
    reading and the entry the record folds on, so one of the two attempts would
    vanish from the record entirely, taking the stall guard's evidence with it.
    """
    definition = tmp_path / "evalset.py"
    definition.write_bytes(b"# resolves nothing, exits immediately\n")
    workers = fleet(definition, tmp_path)
    repeated = action("file.py@task#hash/mockllm/model/x", key="k", attempt=1)

    first = workers.spawn(repeated)
    second = workers.spawn(repeated)
    for worker in (first, second):
        worker.process.wait(timeout=60)

    assert first.worker != second.worker
    assert first.selection.exists() and second.selection.exists()
    # and the record can tell them apart, which is the point of the exercise
    recorded = {
        event.payload["worker"]
        for event in read_events(workers.inflight).events
        if event.type == "intent"
    }
    assert recorded == {first.worker, second.worker}


def test_windows_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows silently ignores `start_new_session`, so a worker there would die
    # with its console. The refusal comes first so a caller that catches it is
    # not left with an orphan selection document to reason about
    monkeypatch.setattr(sys, "platform", "win32")
    workers = fleet(FIXTURES / "simple_evalset.py", tmp_path)
    with pytest.raises(RuntimeError, match="macOS or Linux"):
        workers.spawn(action("never-spawned"))
    assert not workers.workers_dir.exists()


def test_the_eval_set_id_is_minted_once(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    minted = resolve_eval_set_id(str(logs))
    assert (logs / ".eval-set-id").read_text() == minted
    # worker mode never writes this file, so a later run has to find the same
    # id rather than stamping a new one into half the directory's logs
    assert resolve_eval_set_id(str(logs)) == minted


def test_a_worker_lands_the_log_its_identifier_predicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # capture and selection are mutually exclusive, so a worker that inherited
    # an exported capture path would die at startup rather than run
    monkeypatch.setenv(INSPECT_EVAL_SET_CAPTURE, str(tmp_path / "stray.json"))

    definition = FIXTURES / "simple_evalset.py"
    manifest = read_eval_set(definition, cwd=tmp_path)
    workers = spawn_all(manifest, fleet(definition, tmp_path))
    logs = tmp_path / "logs"

    # detachment, asserted at its mechanism: a worker in its own session does
    # not receive the interrupt or hangup aimed at the tend that spawned it
    assert all(os.getsid(worker.pid) != os.getsid(0) for worker in workers)

    wait(workers)

    assert sorted(landed(logs)) == sorted(task.identifier for task in manifest.tasks)
    # the runner owns the eval set id; workers stamp what they are told
    assert {
        read_eval_log(info, header_only=True).eval.eval_set_id
        for info in list_eval_logs(str(logs))
    } == {EVAL_SET_ID}
    # worker mode writes no eval-set metadata: these two are what a second
    # orchestrator sharing the directory would fight over
    assert not (logs / "eval-set.json").exists()
    assert not (logs / "logs.json").exists()

    # one selection and one output per worker, at a path the process table can
    # be searched for
    for worker in workers:
        overrides = read_eval_set_selection(str(worker.selection)).overrides
        assert overrides is not None and overrides.log_dir == str(logs)
        assert worker.output.exists()


def test_a_fleet_shares_one_log_directory(tmp_path: Path) -> None:
    # the production shape: one task per worker, all writing into one flat
    # directory at the same time. Four workers cost the wall time of one, so
    # this also carries the working-directory case -- a task's source file is
    # part of its identity and inspect warns that a worker running from
    # elsewhere may not match, but Steward is immune by construction, because
    # `definition_command` resolves the definition absolutely. If that stops
    # being true, correlation breaks silently and this fails.
    definition = FIXTURES / "sweep_evalset.py"
    manifest = read_eval_set(definition, cwd=tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    workers = spawn_all(manifest, fleet(definition, tmp_path, cwd=elsewhere))
    wait(workers)

    identifiers = sorted(task.identifier for task in manifest.tasks)
    assert len(set(identifiers)) == len(manifest.tasks), "manifest identifiers collided"
    assert sorted(landed(tmp_path / "logs")) == identifiers


def test_a_resumed_worker_lands_a_second_log(tmp_path: Path) -> None:
    definition = FIXTURES / "simple_evalset.py"
    manifest = read_eval_set(definition, cwd=tmp_path)
    task = manifest.tasks[0]
    logs = tmp_path / "logs"
    workers = fleet(definition, tmp_path)

    first = workers.spawn(action(task.identifier, key=task.key))
    wait([first])
    prior = list_eval_logs(str(logs))[0].name

    second = workers.spawn(
        action(task.identifier, key=task.key, resume=prior, attempt=2)
    )
    wait([second])

    # both attempts correlate to the one task -- which is also the supersession
    # case: two logs for one identifier in a shared directory
    assert landed(logs) == [task.identifier, task.identifier]
    # and the second attempt did not write over the first's evidence
    assert first.selection != second.selection
    assert first.output != second.output


def test_a_death_before_the_boundary_leaves_evidence(tmp_path: Path) -> None:
    # the window execution.md names: until a worker reaches its eval it has no
    # log and no control discovery entry, so its output is the only witness.
    # This fixture raises on import, which is that window at its shortest
    definition = FIXTURES / "raises_early.py"
    worker = fleet(definition, tmp_path).spawn(action("never-resolved"))

    assert worker.process.wait(timeout=120) != 0
    assert not list_eval_logs(str(tmp_path / "logs"))
    assert "definition failed during setup" in output(worker)
