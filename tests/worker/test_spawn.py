"""Spawning a worker, and what it leaves behind.

A process launch costs about three seconds, so the tests that spend one say
what it buys. The document, the stem, and the eval set id are settled without
launching anything; only the claims that require a real process — correlation,
detachment, a shared directory, resume, and a death before the boundary — pay.
"""

import os
import sys
from pathlib import Path

import pytest
from inspect_ai._eval.eval_set_manifest import INSPECT_EVAL_SET_CAPTURE
from inspect_ai._eval.eval_set_selection import (
    EVAL_SET_SELECTION_VERSION,
    read_eval_set_selection,
)
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward import read_eval_set
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
        assert read_eval_set_selection(str(worker.selection)).log_dir == str(logs)
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
