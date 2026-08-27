"""Spawning workers in a test, and reading back what they landed.

Shared by the spawn tests and the frontend ones, which differ only in the
definition they fan out over.

Not named `test_*`, so pytest does not collect it.
"""

from pathlib import Path

from inspect_ai._eval.evalset import task_identifier
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward import Manifest, read_eval_set
from inspect_steward._evalset.detect import DefinitionType
from inspect_steward._evalset.observe import observe_logs, observe_tasks
from inspect_steward._schedule import (
    DEFAULT_MAX_SAMPLES,
    InFlight,
    Pool,
    SpawnTask,
    SpawnWorker,
    reconcile,
)
from inspect_steward._worker import Fleet, SpawnedWorker, resolve_eval_set_id

# definition fixtures live beside the tests that read them; spawning runs the
# same ones rather than growing a second set
FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"

EVAL_SET_ID = "steward-test-eval-set"


def fleet(
    definition: Path,
    workspace: Path,
    *,
    type: DefinitionType = "evalset",
    cwd: Path | None = None,
) -> Fleet:
    """A fleet for one definition, with the eval set id resolved as a run would."""
    log_dir = workspace / "logs"
    return Fleet(
        definition=definition,
        type=type,
        log_dir=str(log_dir),
        eval_set_id=resolve_eval_set_id(str(log_dir), EVAL_SET_ID),
        workers_dir=workspace / ".steward" / "workers",
        inflight=workspace / ".steward" / "inflight.jsonl",
        cwd=cwd or workspace,
    )


def action(
    *identifiers: str,
    key: str = "task@model",
    resume: str | None = None,
    attempt: int = 1,
) -> SpawnWorker:
    """A spawn decision, for the cases where reconcile would not produce one.

    Variadic so that a packed worker is written the same way as a plain one, with the extra identifiers simply appended.
    """
    return SpawnWorker(
        tasks=tuple(
            SpawnTask(
                identifier=identifier,
                key=key if index == 0 else f"{key}-{index}",
                resume=resume if index == 0 else None,
                attempt=attempt,
                reason=None,
            )
            for index, identifier in enumerate(identifiers)
        ),
        max_samples=DEFAULT_MAX_SAMPLES,
    )


def spawn_all(manifest: Manifest, workers: Fleet) -> list[SpawnedWorker]:
    """Reconcile an untouched log directory and spawn everything it asks for."""
    observed = observe_tasks(manifest, observe_logs(workers.log_dir))
    plan = reconcile(manifest, InFlight(), observed, pool=Pool())
    spawns = [item for item in plan.actions if isinstance(item, SpawnWorker)]
    assert len(spawns) == len(manifest.tasks), "expected every task pending"
    return [workers.spawn(spawn) for spawn in spawns]


def fan_out(
    fixture: str, workspace: Path, *, type: DefinitionType = "evalset"
) -> tuple[Manifest, Fleet, list[SpawnedWorker]]:
    """Capture a definition, spawn one worker per task, and wait for all of them."""
    definition = FIXTURES / fixture
    manifest = read_eval_set(definition, cwd=workspace)
    workers = fleet(definition, workspace, type=type)
    spawned = spawn_all(manifest, workers)
    wait(spawned)
    return manifest, workers, spawned


def wait(workers: list[SpawnedWorker], timeout: float = 300) -> None:
    """Wait for every worker, reporting what it printed if it failed."""
    for worker in workers:
        code = worker.process.wait(timeout=timeout)
        assert code == 0, f"{worker.key} exited {code}:\n{output(worker)}"


def output(worker: SpawnedWorker) -> str:
    """What a worker printed, decoded as its display writes it."""
    return worker.output.read_text(encoding="utf-8", errors="replace")


def landed(log_dir: Path | str) -> list[str]:
    """Every landed log's identifier, recomputed from the log itself."""
    return [
        task_identifier(read_eval_log(info, header_only=True), None)
        for info in list_eval_logs(str(log_dir))
    ]
