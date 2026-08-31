"""Talking to a live worker, through the CLI inspect already ships.

Every worker runs a control server on an AF_UNIX socket, and Steward reaches it by running `inspect ctl` rather than by speaking HTTP to the socket itself. That looks like the more expensive choice and is not, for one measured reason: **`inspect ctl task list` spans every live process in a single call**, reading them concurrently and stamping each row with its pid. A whole-fleet read is therefore one invocation of ~1.3s, not one per worker — which was the entire case for an in-process client.

What comes with it is worth more than the round trip it costs. The control server shares the eval's event loop, and a busy eval monopolizes that loop for seconds at a time, so **a timeout means *alive but busy* and a connection error means *gone*** — a distinction the CLI already draws, retrying reads and never retrying mutations, and reporting the outcome in a closed vocabulary of error kinds. Reimplementing that here would mean copying four constants and their reasoning, then drifting from them.

**Mutations are this module's; the status table's reads are not.** The argument above held while a whole-fleet read was one invocation, and one column broke that: the model connection pool is `inspect ctl config`, which resolves a *single* task, so a fleet of ten costs ten invocations of ~1.6s — nineteen seconds for a table meant to be run constantly. `live.py` reads those three endpoints over the worker's own socket instead, at milliseconds each and concurrently, and deliberately reverses the retry policy: it reports a slow worker as busy rather than waiting for it. Everything that *changes* a worker stays here, which is where the risk is — a retune that half-lands is a real problem and a status column missing for one turn is not.

**Primitives are `inspect ctl`'s; compositions are Steward's.** One directive against one target, recoverable if wrong, is something an agent runs itself — Steward builds no wrapper, the same way it prints an `inspect acp` command rather than building an ACP client. What Steward builds commands for is operations spanning several directives or surfaces, or carrying a precondition nobody should re-derive from a prompt: invalidate-and-resume, in-flight requeue, a fleet-wide model latch that has to survive tends. The functions here are the substrate those run on, and are **called from Steward's own code only** — the test for adding one is whether Steward calls it, not whether someone might want it.

See execution.md, *Supervising workers*.
"""

import json
import subprocess
import sys
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

CTL = (sys.executable, "-m", "inspect_ai._cli.main", "ctl")
"""How the CLI is invoked: as a module in *this* interpreter.

Never the `inspect` console script. pip puts it beside the interpreter, a directory that is only on `PATH` when the venv happens to be activated — the same gap `definition_command` closes for hawk's `uv`. Running the module needs no `PATH` at all, and guarantees the worker and the client that inspects it are the same inspect.
"""

TIMEOUT = 180.0
"""Seconds to wait for one invocation.

Deliberately longer than the CLI's own budget, which is ~2 minutes for a read it retries through a busy event loop and the same for a mutation whose log flush may be going to S3. A shorter timeout here would cut short the policy this module exists to defer to; what this one catches is a CLI that is itself wedged.
"""

AUTHOR = "steward"
"""Recorded against every change Steward applies. Inspect writes it into each affected eval log with the timestamp and the old and new values, which is what makes an unattended retune reviewable afterwards."""


@dataclass(frozen=True)
class Unavailable:
    """A call that did not produce an answer.

    A value rather than an exception, because a worker being gone is *expected* and is computed on — a tend runs for seconds while workers run for hours, so any of them may finish between one call and the next. Reconcile decides what it means. Genuine defects still raise.
    """

    kind: str
    """Upstream's closed vocabulary — `busy`, `connect_error`, `read_timeout`, `not_found`, `ambiguous`, `http_error`, `invalid_request`, `invalid_response`, `internal` — plus `absent` for the one case it spells with a bare `null`."""

    detail: str


ABSENT = "absent"
"""Nothing to target: a scoped command with no matching task prints `null` and exits **zero**, so the exit code alone under-reports. Not an upstream kind — upstream has no name for it, because for the CLI it is a successful read of nothing."""


class Samples(BaseModel):
    """A task's sample counts."""

    model_config = ConfigDict(extra="allow")

    total: int = 0
    completed: int = 0
    errored: int = 0
    cancelled: int = 0
    in_flight: int = 0
    queued: int = 0


class TaskRow(BaseModel):
    """One running task, as the fleet listing reports it.

    Modelled rather than left as a dict because inspect publishes no type for this — every producer returns `dict[str, Any]` and the CLI parses dicts too, so the contract is held by upstream's tests rather than by a shape Steward could import. Validating the fields Steward depends on at the boundary is the only place a change to that contract surfaces loudly, instead of as a `KeyError` three frames later.
    """

    model_config = ConfigDict(extra="allow")

    pid: int
    """Which process is running it — the join back to `RunningWorker`."""

    task_id: str
    """Stable across retry attempts, and the selector every other command takes."""

    task: str
    status: str

    log_location: str | None = None
    """The log this task is writing, which is how a row correlates to what `observe_logs` will later read."""

    model: str | None = None
    solver: str | None = None
    epochs: int = 1
    attempts: int = 1
    samples: Samples = Samples()

    paused: list[str] | None = None
    """Which scopes have latched this task, or `None` for none.

    Read but never silently corrected: Steward does not undo a latch it did not set, because a worker someone paused deliberately is not drift (execution.md, *Interacting with a detached run*).
    """

    paused_now: list[str] | None = None
    total_tokens: int = 0
    api_version: int = 0


class Knob(BaseModel):
    """One retunable limit."""

    model_config = ConfigDict(extra="allow")

    scope: str = ""

    limit: int | None = None
    """The setpoint, or `None` where the knob does not have a single one."""

    in_use: int = 0
    """How much of it is held right now. What makes a *lowering* decision informed: the ratchet only stops new acquires, so a limit dropped below this drains rather than preempts."""

    adjustable: bool = True


class ConfigTarget(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: str = ""
    task_id: str | None = None
    task: str | None = None


class ConfigKnobs(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_samples: Knob = Knob()


class ConfigView(BaseModel):
    """A task's retunable configuration, and what a retune did to it."""

    model_config = ConfigDict(extra="allow")

    target: ConfigTarget = ConfigTarget()
    knobs: ConfigKnobs = ConfigKnobs()
    warnings: list[str] = []

    applied: bool = False
    dry_run: bool = False

    persisted: dict[str, bool] | None = None
    """Which applied knobs reached the eval log's record, by name. `None` when nothing was applied — a retune can succeed against the running eval and still fail to be written down, and the two are reported separately because only the second is what makes the change reviewable later."""

    requested: dict[str, Any] | None = None

    @property
    def max_samples(self) -> int | None:
        """The sample-concurrency setpoint."""
        return self.knobs.max_samples.limit


def list_tasks(pids: Collection[int]) -> list[TaskRow] | Unavailable:
    """Read every task the given processes are running.

    One invocation covers the whole fleet, because the listing already spans processes. The filter is `pids` rather than a per-worker call: the listing sees every Inspect process on the machine, including another workspace's workers and a hand-run `inspect eval`, and none of those are this workspace's business — the same bound `scan_processes` applies, expressed against the same set of workers.

    Args:
        pids: The processes to keep. Steward's own, from `resolve_inflight`.

    Returns:
        One row per running task in those processes, or why there is no answer. An empty list is a real answer — no process is running a task — and is not the same as `Unavailable`.
    """
    result = _ctl("task", "--json")
    if isinstance(result, Unavailable):
        return result
    rows = result.get("tasks")
    if not isinstance(rows, list):
        return Unavailable("invalid_response", "task listing carried no 'tasks'")
    wanted = set(pids)
    return [
        TaskRow.model_validate(row)
        for row in cast(list[Any], rows)
        if isinstance(row, dict) and cast(dict[str, Any], row).get("pid") in wanted
    ]


def cancel_task(task_id: str, *, dry_run: bool = False) -> dict[str, Any] | Unavailable:
    """Ask a running task to stop, keeping what it has already done.

    The graceful half of `_worker.stop`: completed samples are kept, in-flight ones are interrupted, the log is finalized, and an eval set will not retry a cancelled task. **It returns once the cancel is accepted, not once the worker is gone** — finalizing a log is unbounded and nothing that holds a claim may wait on it.

    `--action cancel` rather than `score` or `error`, because a sample stopped halfway through is not a result: scoring the work so far would put a number in the log that no complete sample produced, which is worse than a missing one for anybody reading the directory later.

    Args:
        task_id: The task, from `list_tasks`.
        dry_run: Report what would be cancelled without doing it. What keeps `status` a genuine `tend --dry-run` if anything on that path ever cancels.

    Returns:
        The CLI's mutation envelope, or why the task could not be asked. `ABSENT` is the notable one: the process is there and is running no such task, which for a Steward worker means it is either pre-boundary or already leaving.
    """
    args = ["task", "cancel", task_id, "--json"]
    if dry_run:
        args.append("--dry-run")
    return _ctl(*args)


def task_config(
    task_id: str,
    *,
    max_samples: int | None = None,
    max_connections: int | None = None,
    reason: str | None = None,
    dry_run: bool = False,
) -> ConfigView | Unavailable:
    """Read a task's retunable configuration, or retune it.

    One function rather than two, because the CLI has one: `inspect ctl config TASK` with no set options *is* the read, and splitting it here would invent a distinction the surface does not make. A retune returns the resulting view, so a caller never needs a second call to see what happened.

    Args:
        task_id: The task, from `list_tasks`.
        max_samples: New sample-concurrency setpoint, or `None` to read.
        max_connections: New scaling ceiling for the adaptive connection controllers, or `None` to leave it. Process-scoped — the task only selects the process — and asymmetric by mechanism: lowering it clamps live connection concurrency at once, which is the tuning loop's fast half on overshoot, while raising it only lets the controllers climb on later clean rounds.
        reason: Why. Required alongside a change, because it is what annotates the record inspect writes into the eval log — an unattended retune with no reason is one nobody can review later.
        dry_run: Report the change without applying it. What keeps `status` a genuine `tend --dry-run` once anything here has an effect.

    Returns:
        The resulting configuration, or why there is no answer.

    Raises:
        ValueError: If a change is requested with no reason. A programming error rather than a runtime condition, so it raises where `Unavailable` would hide it.
    """
    args = ["config", task_id, "--json"]
    if max_samples is not None or max_connections is not None:
        if not reason:
            raise ValueError(
                "A `reason` is required to change a worker's configuration: it is "
                "recorded in the eval log alongside the change, and is what makes "
                "an unattended retune reviewable."
            )
        if max_samples is not None:
            args += ["--max-samples", str(max_samples)]
        if max_connections is not None:
            args += ["--max-connections", str(max_connections)]
        args += ["--author", AUTHOR, "--reason", reason]
    if dry_run:
        args.append("--dry-run")

    result = _ctl(*args)
    if isinstance(result, Unavailable):
        return result
    return ConfigView.model_validate(result)


def _ctl(*args: str, timeout: float = TIMEOUT) -> dict[str, Any] | Unavailable:
    """Run one `inspect ctl` command and decode its JSON.

    Args:
        *args: Command and options, always including `--json`.
        timeout: Seconds to wait.

    Returns:
        The decoded document, or why there is no answer. Every command Steward runs answers with an object, so anything else is read as a broken contract rather than unpacked.

    Raises:
        RuntimeError: On a usage error (exit 2), raised by `_decode` — it means Steward built a command line the CLI does not accept, which is a defect here rather than a condition out there.
    """
    try:
        completed = subprocess.run(
            [*CTL, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        # past the CLI's own retry budget, so this is not a merely busy eval —
        # but it is still alive as far as anything here can tell
        return Unavailable("busy", f"`ctl {args[0]}` did not return in {timeout:.0f}s")
    return _decode(
        completed.returncode,
        completed.stdout,
        completed.stderr,
        command=" ".join(args),
    )


def _decode(
    returncode: int, stdout: str, stderr: str, *, command: str
) -> dict[str, Any] | Unavailable:
    """Turn one finished invocation into a document or a reason there is none.

    Split from the call so the classification — which is the part with the interesting behaviour — can be exercised against outcomes a test cannot manufacture: no eval is reliably wedged on demand, and no CLI reliably emits a malformed body.

    Args:
        returncode: Exit status.
        stdout: What it printed. `--json` puts both the document and the error envelope here.
        stderr: Narration, which is where the CLI reports its own busy retries.
        command: The arguments, for the message.

    Returns:
        The decoded document, or why there is no answer.

    Raises:
        RuntimeError: On a usage error (exit 2).
    """
    if returncode == 2:
        raise RuntimeError(
            f"`inspect ctl {command}` was rejected as a usage error: {stderr.strip()}"
        )

    body = stdout.strip()
    if not body:
        return Unavailable("internal", stderr.strip() or "no output and no error")
    try:
        document: Any = json.loads(body)
    except ValueError as ex:
        return Unavailable("invalid_response", f"undecodable output ({ex})")

    if document is None:
        # a scoped command with nothing to target: exit 0, body `null`
        return Unavailable(ABSENT, f"`ctl {command}` matched nothing")
    if not isinstance(document, dict):
        return Unavailable(
            "invalid_response", f"expected an object, got {type(document).__name__}"
        )
    fields = cast(dict[str, Any], document)
    if "error" in fields:
        return _failure(fields["error"])
    return fields


def _failure(error: Any) -> Unavailable:
    """Read the CLI's `{"error": {kind, exception, message, status}}` envelope."""
    if not isinstance(error, dict):
        return Unavailable("internal", str(error))
    fields = cast(dict[str, Any], error)
    kind, message = fields.get("kind"), fields.get("message")
    return Unavailable(
        kind if isinstance(kind, str) else "internal",
        message if isinstance(message, str) else str(fields),
    )
