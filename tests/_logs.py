"""Synthesizing log directories without running evals.

`reconcile` takes a manifest and a log directory and returns decisions, so
testing it means producing log directories — and producing them by running
evals would cost a process launch per state (testing.md, *the fixture
generator is the highest-leverage thing to build*). Everything here writes
files directly: a whole synthetic log is under a kilobyte, and the eight states
worth testing are eight function calls.

One `_eval_spec()` builds the `EvalSpec` that both the manifest row and every
log for a task derive from, so **the manifest and the logs agree on the
identifier by construction** rather than by a literal repeated in two places.

The limitation that comes with that, stated rather than discovered: both sides
compute the identifier through `task_identifier`'s `EvalLog` branch, so these
fixtures cannot prove that a *captured* task and its log correlate. They are
not meant to — that claim belongs to `tests/evalset/test_selection.py`, which
runs real workers.
"""

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from inspect_ai._eval.eval_set_manifest import task_args_hash
from inspect_ai._eval.evalset import TASK_IDENTIFIER_VERSION, task_identifier
from inspect_ai._util.file import clean_filename_component
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalError,
    EvalLog,
    EvalMetric,
    EvalPlan,
    EvalResults,
    EvalScore,
    EvalSpec,
    EvalStats,
    HeadlineMetric,
    write_eval_log,
)
from inspect_ai.util._sandbox.environment import SandboxEnvironmentSpec
from inspect_steward._evalset.display import compute_display_keys
from inspect_steward._evalset.manifest import (
    MANIFEST_VERSION,
    Manifest,
    ManifestSource,
    ManifestTask,
)

DEFINITION = "evalset.py"
CREATED = "2026-08-23T19:00:00+00:00"

LogFormat = Literal["json", "eval"]


@dataclass(frozen=True)
class SynthTask:
    """A task that never runs.

    The fields are exactly the ones a test varies: identity (`name`, `args`,
    `model`, `file`) and the completeness inputs the manifest carries
    (`samples`, `epochs`).
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    model: str = "mockllm/model"
    file: str | None = DEFINITION
    samples: int = 10
    epochs: int = 1

    display_name: str | None = None
    """What the manifest calls this task, when that differs from `name`.

    Only ever on the manifest row: a log records the registered `name`, and the
    two are correlated by identifier rather than by either of them.
    """

    limits: dict[str, int] = field(default_factory=dict[str, int])
    """Per-sample budgets by `EvalConfig` field name, e.g. `{"turn_limit": 300}`.

    On the task rather than on `write_log`, because `task_identifier` hashes the
    eval config: a log carrying a limit its manifest row does not would compute
    a different identifier and read as an orphan.
    """

    @property
    def identifier(self) -> str:
        return task_identifier(EvalLog(eval=_eval_spec(self), plan=EvalPlan()), None)

    @property
    def required_samples(self) -> int:
        return self.samples * self.epochs

    @property
    def task_id(self) -> str:
        """A stable task id, shared by every attempt at this task.

        Which is what a real resume produces: `to_eval_set_task` reuses the
        prior log's `task_id` rather than minting a new one.

        Derived from the identity fields rather than from `identifier`, which
        would recur: the identifier is computed from a spec that carries this.
        """
        identity = f"{self.file}@{self.name}#{sorted(self.args.items())}/{self.model}"
        return hashlib.sha256(identity.encode()).hexdigest()[:22]


def _eval_spec(
    task: SynthTask,
    *,
    created: str = CREATED,
    epochs: int | None = None,
    selection: dict[str, Any] | None = None,
    sandbox: SandboxEnvironmentSpec | None = None,
    model_base_url: str | None = None,
) -> EvalSpec:
    """The one place a synthetic task becomes an `EvalSpec`.

    `epochs` overrides what the log ran with, so a test can put a 1-epoch log
    under a 3-epoch manifest task. `selection` does the same for `limit`,
    `sample_id` and `sample_shuffle` — which samples ran, as distinct from how
    many — and `sandbox`/`model_base_url` for what the run talked to.
    """
    return EvalSpec(
        created=created,
        task=task.name,
        task_id=task.task_id,
        task_file=task.file,
        task_args=task.args,
        task_args_passed=task.args,
        dataset=EvalDataset(samples=task.samples),
        model=task.model,
        sandbox=sandbox,
        model_base_url=model_base_url,
        # limits go on via `model_copy` rather than as keywords: `EvalConfig`
        # takes a hundred fields of every type, so a `**dict[str, int]` spread
        # is checked against all of them
        config=EvalConfig(
            epochs=epochs if epochs is not None else task.epochs
        ).model_copy(update={**task.limits, **(selection or {})}),
    )


def synth_manifest(tasks: Sequence[SynthTask], **options: Any) -> Manifest:
    """A manifest naming exactly these tasks.

    Args:
        tasks: Tasks the definition would resolve to.
        **options: Informational `eval_set()` options to record. The three selection keys are recorded whether named or not, because capture records them whether the definition set them or not — and a reader distinguishes *the definition asked for nothing* from *this manifest predates the field*, so a helper that omitted them would test the wrong one of the two.

    Returns:
        Manifest whose identifiers match the logs `write_log` writes for the same tasks.
    """
    options = {"limit": None, "sample_id": None, "sample_shuffle": None} | options
    rows = [
        ManifestTask(
            name=task.name,
            display_name=task.display_name,
            file=task.file,
            args=task.args,
            args_hash=task_args_hash(task.args),
            model=task.model,
            model_args={},
            sequence=sequence,
            identifier=task.identifier,
            samples=task.samples,
            epochs=task.epochs,
            key="",
        )
        for sequence, task in enumerate(tasks)
    ]
    keys = compute_display_keys(rows)
    return Manifest(
        version=MANIFEST_VERSION,
        identifier_version=TASK_IDENTIFIER_VERSION,
        source=ManifestSource(
            type="evalset",
            path=DEFINITION,
            content_hash="sha256:" + hashlib.sha256(b"synthetic").hexdigest(),
            args={},
        ),
        options=options,
        tasks=[
            row.model_copy(update={"key": key})
            for row, key in zip(rows, keys, strict=True)
        ],
    )


def write_log(
    log_dir: Path,
    task: SynthTask,
    *,
    status: Literal["started", "success", "cancelled", "error"] = "success",
    total: int | None = None,
    completed: int | None = None,
    invalidated: bool = False,
    error: str | None = None,
    epochs: int | None = None,
    created: str = CREATED,
    format: LogFormat = "json",
    scores: dict[str, dict[str, float]] | None = None,
    headline: HeadlineMetric | None = None,
    selection: dict[str, Any] | None = None,
    sandbox: SandboxEnvironmentSpec | None = None,
    model_base_url: str | None = None,
) -> Path:
    """Write one log for a task.

    Args:
        log_dir: Directory to write into (created if absent).
        task: Task the log is for.
        status: Log status.
        total: `results.total_samples` (defaults to samples × epochs).
        completed: `results.completed_samples` (defaults to `total`).
        invalidated: Whether samples in it were invalidated.
        error: Error message, which also implies `status="error"` unless one was given.
        epochs: Epochs the log ran with (defaults to the task's).
        created: `eval.created`, which orders attempts and names the file.
        format: `json` for a document, `eval` for a real zip.
        scores: Scorer name to metric name to value, e.g. `{"exact": {"accuracy": 0.75}}`.
        headline: Which of `scores` the task declared as its headline, as scoring resolves it onto `results.headline`. `None` leaves the log undeclared, where a reader falls back to the first metric of the first score.
        selection: `limit`, `sample_id` and `sample_shuffle` the log ran with — which samples, rather than how many.
        sandbox: The sandbox the log ran under, **as resolved** — which is what a log records, config file and all.
        model_base_url: The gateway the log's model calls went to.

    Returns:
        Path the log was written to.
    """
    if error is not None and status == "success":
        status = "error"

    results: EvalResults | None = None
    if status != "started":
        total = total if total is not None else task.required_samples
        results = EvalResults(
            total_samples=total,
            completed_samples=completed if completed is not None else total,
            scores=[
                EvalScore(
                    name=name,
                    scorer=name,
                    metrics={
                        metric: EvalMetric(name=metric, value=value)
                        for metric, value in metrics.items()
                    },
                )
                for name, metrics in (scores or {}).items()
            ],
            headline=headline,
        )

    log = EvalLog(
        status=status,
        eval=_eval_spec(
            task,
            created=created,
            epochs=epochs,
            selection=selection,
            sandbox=sandbox,
            model_base_url=model_base_url,
        ),
        plan=EvalPlan(),
        results=results,
        # a log that never finished has no completion time, exactly as the
        # mid-run header fallback produces
        stats=EvalStats(
            started_at=created, completed_at="" if status == "started" else created
        ),
        invalidated=invalidated,
        error=EvalError(message=error, traceback="", traceback_ansi="")
        if error is not None
        else None,
    )

    location = log_dir / f"{_file_stem(task, created)}.{format}"
    log_dir.mkdir(parents=True, exist_ok=True)
    write_eval_log(log, str(location), format=format)
    return location


def write_running_eval(
    log_dir: Path, task: SynthTask, *, created: str = CREATED
) -> Path:
    """Write an `.eval` that is still being written.

    Mid-run there is no `header.json` in the zip — the reader falls back to
    `_journal/start.json` — and that fallback is a property of the zip format
    that a `json` log cannot express. It is nonetheless one zip member, so it
    needs no eval to produce.

    Args:
        log_dir: Directory to write into (created if absent).
        task: Task the log is for.
        created: `eval.created`.

    Returns:
        Path the log was written to.
    """
    start = {
        "version": 2,
        "eval": json.loads(_eval_spec(task, created=created).model_dump_json()),
        "plan": json.loads(EvalPlan().model_dump_json()),
    }
    location = log_dir / f"{_file_stem(task, created)}.eval"
    log_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(location, "w") as zip:
        zip.writestr("_journal/start.json", json.dumps(start))
    return location


def write_unreadable(
    log_dir: Path, *, name: str = "broken", created: str = CREATED
) -> Path:
    """Write a file that will be listed as a log and cannot be read as one.

    Args:
        log_dir: Directory to write into (created if absent).
        name: Task name to put in the filename.
        created: Timestamp prefix, without which the file is not listed at all.

    Returns:
        Path the file was written to.
    """
    location = log_dir / f"{clean_filename_component(created)}_{name}_id.json"
    log_dir.mkdir(parents=True, exist_ok=True)
    location.write_text('{"version": 2, "status": "suc', encoding="utf-8")
    return location


def _file_stem(task: SynthTask, created: str) -> str:
    """`{created}_{task}_{id}`, as Inspect's recorders name logs.

    The timestamp prefix is not cosmetic: `is_log_file` will not list a `.json`
    file without it.
    """
    return "_".join(
        clean_filename_component(part) for part in (created, task.name, task.task_id)
    )
