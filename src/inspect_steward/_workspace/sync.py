"""Propagating the workspace to the log directory.

A run's results go one place and everything that explains them goes another, and the second place is frequently a machine nobody can reach. Some runs happen with no git and no internet beyond an object store: on such a machine the bucket **is** the observability channel, and the alternative to propagating is shelling into the runner, which is exactly what an unattended overnight job should not require (workflow.md, *Syncing the workspace out*).

So each tend mirrors the workspace's files into the log directory. Somebody reading from another system finds `status.md` for progress, `journal.jsonl` for what has been decided, `steward.log` for whether the machinery is working, and the definition and `_steward.yaml` for what is being run and under what rules — beside the logs those files are about, with no second location to be told about and `inspect view` working against the same prefix.

**Always, rather than only when the log directory is remote.** The rule used to test for remoteness, which silently skipped a definition pointing at a mounted NAS or `/data/runs/oct/logs` — the same need, the same absent files, and a discriminator that answers a question nobody asked. The workspace follows its logs.

**Exclusionary, because the point is to carry out what nobody predicted.** An analysis an agent wrote, a note a human left, a report a scaffold generated: an allow-list leaves every one of those behind, which is the failure you notice last and regret most. So everything at the top level goes, minus a short deny list — and dotfiles are on it, which is what keeps a stray `.env` out of a bucket.

**What leaves is transcript-derived, and Steward does not pretend otherwise.** `journal.jsonl` holds error text; an agent's writeup quotes what it read. Usually the destination is the same place as the logs, so the audience is unchanged — but an `.eval` is a zip that needs tooling and a text file is greppable by anyone with read access, and easy extraction is what turns a theoretical exposure into a real one. There is no redactor here, deliberately: one that catches most secrets converts *this holds transcript material* into an implied guarantee that it does not (workflow.md §9.2).

**It is advisory and it never raises.** An eval must not fail because a bucket was briefly unreachable, so every failure is recorded and the turn carries on. The bound is a deadline between files rather than a cancellation: bounding one call is the storage client's job and it already does it, and what a deadline buys here is that a slow pipe becomes a reported fact rather than a mysteriously long tend. A tend running long is already answered — the claim is held while a turn runs, so the next timer fire is refused and the interval after that converges.

**Outbound only.** Editing `_steward.yaml` in the bucket does not change the run; two-way sync needs conflict resolution nobody wants for a monitoring channel.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from inspect_ai._util.constants import ALL_LOG_FORMATS
from inspect_ai._util.file import filesystem

# the exact predicate `list_eval_logs` applies, imported rather than reproduced:
# a copy of it here would be a second answer to *is this a log*, and the two
# would disagree the first time upstream changed either half of the rule
from inspect_ai.log._file import is_log_file

from .layout import Workspace
from .log import steward_log

SYNC_BUDGET = 60.0
"""Seconds a tend will spend propagating before it stops and says what is left.

A handful of small files and one that grows, so this is not a transfer rate — it is the point at which *the pipe is slow tonight* stops being invisible. Files that did not fit go on the next turn, since unchanged ones are skipped and this one made real progress.
"""

NEVER_SYNCED = frozenset({"AGENTS.md", "CLAUDE.md"})
"""Top-level files the deny list names outright.

Agent bootstrap: static, and meaningless to a remote reader. Everything else excluded is excluded by kind — directories, because `logs/` is the destination and `.steward/` is machine state, and dotfiles, because that is what keeps `.env` on the machine it belongs to.
"""

_LOG_EXTENSIONS = [f".{format}" for format in ALL_LOG_FORMATS]


@dataclass(frozen=True)
class SyncReport:
    """What one propagation moved, and what it did not."""

    target: str
    """Where the files went."""

    carried: list[str] = field(default_factory=list[str])
    """Files written this turn, by name."""

    skipped: list[str] = field(default_factory=list[str])
    """Files the destination already has, unchanged since it got them."""

    refused: list[str] = field(default_factory=list[str])
    """Files inspect would read as eval logs, left where they are — see `_refused`."""

    unfollowed: list[str] = field(default_factory=list[str])
    """Symlinks, left where they are — see the loop in `sync_workspace`."""

    failures: list[str] = field(default_factory=list[str])
    """Files that could not be written, with the reason."""

    remaining: list[str] = field(default_factory=list[str])
    """Files the budget ran out before reaching. Empty in the ordinary case."""


def sync_target(sync: str | bool | None, log_dir: str) -> str | None:
    """Where this workspace propagates to.

    Args:
        sync: `Directives.sync` — a location, `auto`, `false`, or `None` for no preference.
        log_dir: The run's log directory, which is what `auto` resolves to.

    Returns:
        The destination, or `None` where the workspace propagates nowhere.
    """
    if sync is False:
        return None
    if isinstance(sync, str) and sync != "auto":
        return sync
    return log_dir


def sync_workspace(
    workspace: Workspace, target: str, *, budget: float = SYNC_BUDGET
) -> SyncReport:
    """Mirror the workspace's files to a destination.

    Never raises. Every failure is recorded in `steward.log` and reported back, because a caller here has just finished a turn and must not lose it to a bucket.

    Args:
        workspace: The workspace to propagate.
        target: Where to write, as `sync_target` resolved it.
        budget: Seconds to spend before stopping and saying what is left.

    Returns:
        What moved, what was already there, and what did not fit.
    """
    report = SyncReport(target=target)
    carried = _carried(workspace)
    if not carried:
        return report

    try:
        fs = filesystem(target)
        fs.mkdir(target, exist_ok=True)
    except Exception as ex:
        # the whole destination, rather than one file: a bucket that is gone,
        # a credential that expired, a mount that went away
        _failed(
            workspace, report, f"could not reach {target}: {type(ex).__name__}: {ex}"
        )
        return report

    known = _known(workspace, target)
    deadline = time.monotonic() + budget
    for index, path in enumerate(carried):
        if path.is_symlink():
            # **the deny list is a rule about names, and a symlink is a name
            # that means a different name.** A top-level `public-config`
            # pointing at `.env` is not a dotfile and is a perfectly good file,
            # so every check above it passes and `put_file` dereferences it --
            # which puts the credential in a bucket by the one route the rule
            # exists to close. Nothing outside the workspace is any better. So
            # they are not followed at all, and are named rather than dropped
            report.unfollowed.append(path.name)
            continue
        if _refused(path):
            report.refused.append(path.name)
            continue
        stamp = _stamp(path)
        if stamp is not None and known.get(path.name) == stamp:
            report.skipped.append(path.name)
            continue
        if time.monotonic() > deadline:
            report.remaining.extend(later.name for later in carried[index:])
            break
        try:
            fs.put_file(str(path), f"{target.rstrip('/')}{fs.sep}{path.name}")
        except Exception as ex:
            _failed(
                workspace,
                report,
                f"could not sync {path.name}: {type(ex).__name__}: {ex}",
            )
            continue
        report.carried.append(path.name)
        if stamp is not None:
            known[path.name] = stamp

    if report.refused:
        steward_log(
            workspace.log,
            f"did not sync {', '.join(report.refused)}: inspect would read "
            f"{'them' if len(report.refused) > 1 else 'it'} as an eval log in {target}",
        )
    if report.unfollowed:
        steward_log(
            workspace.log,
            f"did not sync {', '.join(report.unfollowed)}: a symlink is not "
            f"followed, since what it points at is not what the deny list saw",
        )
    if report.remaining:
        steward_log(
            workspace.log,
            f"sync ran out of time after {budget:.0f}s with "
            f"{len(report.remaining)} file(s) to go ({', '.join(report.remaining)}); "
            f"the next turn carries them",
        )
    _remember(workspace, target, known)
    return report


def _carried(workspace: Workspace) -> list[Path]:
    """Every file this workspace propagates, in a stable order.

    The exclusionary rule over the top level, plus the two machine logs by name. Those two are under `.steward/` because that is the category they belong to — disposable, and truncatable in a way `journal.jsonl` must never be — and naming them here is what keeps that from costing a remote reader the answer to *is the machinery working*. They land flat: the destination is an observability surface rather than a workspace somebody restores from.
    """
    try:
        top = sorted(
            path
            for path in workspace.root.iterdir()
            # a symlink counts as a candidate so that the loop can *refuse* it
            # by name; testing `is_file()` alone would follow it here and drop a
            # dangling or directory one silently, which is the same silence the
            # exclusionary policy exists to avoid
            if (path.is_symlink() or path.is_file())
            and not path.name.startswith(".")
            and path.name not in NEVER_SYNCED
        )
    except OSError:
        return []
    return top + [
        path
        for path in (workspace.log, workspace.timer_log)
        if path.is_symlink() or path.is_file()
    ]


def _refused(path: Path) -> bool:
    """Whether inspect would read this file as an eval log where it is going.

    The destination is the log directory, so a carried file that looks like a log *becomes* one — listed by `observe_logs`, read as a header, and reported as damage every turn when it will not parse. Narrow in practice: `.eval` counts unconditionally, and anything else needs both an ISO-timestamp prefix and a `.json` suffix. Not narrow enough to leave to luck, though, because the policy above is exclusionary — the whole point is that files nobody anticipated are carried, and one of them will eventually be named like a log.
    """
    return is_log_file(path.name, _LOG_EXTENSIONS)


def _stamp(path: Path) -> tuple[int, float] | None:
    """A file's size and modification time, or `None` where it cannot be read.

    Local times only, compared against a local record of them. Nothing here subtracts a remote store's clock from this machine's, which is the one clock rule that is a correctness constraint rather than hygiene (execution.md, *Clocks*).
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime)


def _known(workspace: Workspace, target: str) -> dict[str, tuple[int, float]]:
    """What **this** destination was last given, by name.

    **Scoped to the target, and the scoping is not decoration.** The record answers *does the far end already have this*, which is a question about one far end: a record kept by filename alone would report every file as already sent the first time somebody pointed `--sync` somewhere new, and leave the new destination empty. So a target that does not match the recorded one starts from nothing, which costs one full propagation — the right price for a question whose answer is genuinely unknown.

    Disposable, like everything else under `.steward/`: losing it costs the same one propagation. Which is also why nothing here raises — a record that cannot be read is the same as one that does not exist yet.
    """
    try:
        loaded = json.loads(workspace.synced.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    record = cast(dict[str, Any], loaded)
    if record.get("target") != target:
        return {}
    files = record.get("files")
    if not isinstance(files, dict):
        return {}
    known: dict[str, tuple[int, float]] = {}
    for name, stamp in cast(dict[Any, Any], files).items():
        if not isinstance(name, str) or not isinstance(stamp, list):
            continue
        recorded = cast(list[Any], stamp)
        if len(recorded) == 2:
            size, mtime = recorded
            if isinstance(size, int) and isinstance(mtime, (int, float)):
                known[name] = (size, float(mtime))
    return known


def _remember(
    workspace: Workspace, target: str, known: dict[str, tuple[int, float]]
) -> None:
    """Record what this destination now has. Never raises."""
    try:
        workspace.synced.parent.mkdir(parents=True, exist_ok=True)
        workspace.synced.write_text(
            json.dumps(
                {
                    "target": target,
                    "files": {name: list(stamp) for name, stamp in known.items()},
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def _failed(workspace: Workspace, report: SyncReport, message: str) -> None:
    report.failures.append(message)
    steward_log(workspace.log, message)


__all__ = ["SYNC_BUDGET", "SyncReport", "sync_target", "sync_workspace"]
