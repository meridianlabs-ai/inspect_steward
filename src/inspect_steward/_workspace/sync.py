"""Propagating the workspace to the log directory.

A run's results go one place and everything that explains them goes another, and the second place is frequently a machine nobody can reach. Some runs happen with no git and no internet beyond an object store: on such a machine the bucket **is** the observability channel, and the alternative to propagating is shelling into the runner, which is exactly what an unattended overnight job should not require (workflow.md, *Syncing the workspace out*).

So each tend mirrors the workspace's files into the log directory. Somebody reading from another system finds `status.md` for progress, `journal.jsonl` for what has been decided, `steward.log` for whether the machinery is working, and the definition and `_steward.yaml` for what is being run and under what rules — beside the logs those files are about, with no second location to be told about and `inspect view` working against the same prefix.

**Always, rather than only when the log directory is remote.** The rule used to test for remoteness, which silently skipped a definition pointing at a mounted NAS or `/data/runs/oct/logs` — the same need, the same absent files, and a discriminator that answers a question nobody asked. The workspace follows its logs.

**Exclusionary, because the point is to carry out what nobody predicted.** An analysis an agent wrote, a note a human left, a report a scaffold generated: an allow-list leaves every one of those behind, which is the failure you notice last and regret most. So everything at the top level goes, minus a short deny list — and dotfiles are on it, which is what keeps a stray `.env` out of a bucket.

**What leaves is transcript-derived, and Steward does not pretend otherwise.** `journal.jsonl` holds error text; an agent's writeup quotes what it read. Usually the destination is the same place as the logs, so the audience is unchanged — but an `.eval` is a zip that needs tooling and a text file is greppable by anyone with read access, and easy extraction is what turns a theoretical exposure into a real one. There is no redactor here, deliberately: one that catches most secrets converts *this holds transcript material* into an implied guarantee that it does not (workflow.md §9.2).

**One key is omitted, and it is not a redactor.** `notification` may hold an Apprise URL, which is a bearer token, and this is the one path that would put one in an object store. What that argument rules out is a *heuristic* pass over arbitrary content; this is one key Steward itself defined, found by name, in one file Steward itself writes the template for. It promises nothing about the rest of the file and the paragraph above still stands (`_omitted`).

**It is advisory and it never raises.** An eval must not fail because a bucket was briefly unreachable, so every failure is recorded and the turn carries on. The bound is a deadline between files rather than a cancellation: bounding one call is the storage client's job and it already does it, and what a deadline buys here is that a slow pipe becomes a reported fact rather than a mysteriously long tend. A tend running long is already answered — the claim is held while a turn runs, so the next timer fire is refused and the interval after that converges.

**Outbound only.** Editing `_steward.yaml` in the bucket does not change the run; two-way sync needs conflict resolution nobody wants for a monitoring channel.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml
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

    withheld: list[str] = field(default_factory=list[str])
    """Files held back because a credential could not be taken out of them — see `_omitted`. Empty in every ordinary case."""

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
            source = _omitted(workspace, path)
            if source is None:
                # a credential is the one thing worth losing a remote reader's
                # copy over, so this fails closed rather than sending it anyway
                report.withheld.append(path.name)
                continue
            fs.put_file(str(source), f"{target.rstrip('/')}{fs.sep}{path.name}")
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
    if report.withheld:
        steward_log(
            workspace.log,
            f"did not sync {', '.join(report.withheld)}: the notification "
            f"channel could not be taken out of "
            f"{'them' if len(report.withheld) > 1 else 'it'}, and a channel is "
            f"a credential — fix the file, or move the value to .env",
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


OMITTED_VALUE = "omitted by steward: this copy travels to the log store"
"""What the channel is replaced by in the propagated `_steward.yaml`.

A sentence rather than a blank, because a reader comparing the copy against the original has to be able to tell *nobody set one* from *it was taken out on the way here*.
"""

OMITTED = f"notification: '{OMITTED_VALUE}'"
"""The whole replacement line. Still a valid setting, so the copy is still a file that parses."""

_NOTIFICATION = re.compile(
    r"""^[ \t]*(?:notification|["']notification["'])[ \t]*:""", re.MULTILINE
)
"""A `notification` key on a line of its own, quoted or not, at whatever indentation.

**The rewriter, and never the test.** What this finds, `_without_notification` takes out; whether anything is left is decided by a parse, because a pattern standing in for a parse is a pattern somebody's typo can walk around.
"""

HARMLESS = frozenset({"tag:yaml.org,2002:bool", "tag:yaml.org,2002:null"})
"""Value tags a `notification` key may carry without carrying a credential.

`false` declines a channel and `null` sets none. Everything else is treated as one — including a list and a mapping, which no spelling of the setting accepts but both of which can hold a URL, and a file Steward refuses to *read* is not thereby a file it may ship.
"""


def _omitted(workspace: Workspace, path: Path) -> Path | None:
    """The file to upload for this one — itself, unless it is `_steward.yaml`.

    **Line-based rather than a YAML round-trip**, because the file is mostly comments: re-emitting it from a parse would land a copy in the log store that shares none of the original's explanation of itself. The value is replaced in place and any continuation lines under it are dropped, which covers the block scalar nobody should write and one person eventually will.

    **And then verified, which is what makes it a guarantee rather than a pattern that has held so far.** A rewrite driven by a regex is a rewrite that can be evaded — a flow mapping (`{notification: slack://…}`) is valid YAML on one line and matches nothing line-shaped — and the cost of a miss here is a bearer token in an object store. So the scrubbed text is parsed back, and a file still carrying a channel is **withheld rather than sent**. Failing closed is right for this one file: what is lost is a remote reader's copy of settings they can also see in `status.md`, and what is prevented is a credential nobody can recall.

    The scrubbed copy lives under `.steward/`, beside the rest of the machinery, and is overwritten every turn.

    Args:
        workspace: The workspace being propagated.
        path: The file about to be uploaded.

    Returns:
        `path` where nothing needs removing, a temporary file with the channel taken out, or `None` where it could not be taken out and the file must not travel.
    """
    if path.name != workspace.directives.name:
        return path
    text = path.read_text(encoding="utf-8")
    if not _carries_channel(text):
        return path
    scrubbed_text = _without_notification(text)
    if _carries_channel(scrubbed_text):
        return None
    scrubbed = workspace.state / f"{path.name}.sync"
    scrubbed.parent.mkdir(parents=True, exist_ok=True)
    scrubbed.write_text(scrubbed_text, encoding="utf-8")
    return scrubbed


def _carries_channel(text: str) -> bool:
    """Whether this `_steward.yaml` text still names a notification channel.

    **The parse is the only authority, and text that will not parse answers *yes*.** Loading the document decides exactly, including for shapes no line-based rule sees — and the line-based rule was the hole: `{notification: slack://…` in a file with an unclosed brace fails to load, matches no pattern anchored to the start of a line, and travels to the log store with the credential in it. A pattern standing in for a parse is a pattern somebody's typo can walk around, which for a bearer token is not a fallback but a leak with a schedule.

    So an unparseable file is treated as carrying one whether or not it does. It is a file `read_directives` has already rejected — the run is degraded and reporting why — so a remote reader loses a copy of settings that were not in force anyway.

    **Every pair is read, not the mapping the loader builds from them**, and that is the second hole of the same shape. Duplicate keys are legal YAML and the last one wins, so

    ```yaml
    notification: slack://xoxb-…/…
    notification: false
    ```

    loads to `False` — a declined channel, and a file that would have travelled with the token still on line one. Composing the document exposes both pairs, and any of them naming a value that is not plainly a decline is enough to act on.
    """
    try:
        node = _composed(text)
    except yaml.YAMLError:
        return True
    if not isinstance(node, yaml.MappingNode):
        return False
    return any(
        _is_channel(value)
        for key, value in node.value
        if isinstance(key, yaml.ScalarNode) and key.value == "notification"
    )


def _composed(text: str) -> yaml.Node | None:
    """The document as nodes, one step short of the mapping a load would build.

    The step that is skipped is the one that discards a duplicate key.
    """
    loader = yaml.SafeLoader(text)
    try:
        return loader.get_single_node()
    finally:
        loader.dispose()


def _is_channel(value: yaml.Node) -> bool:
    """Whether one `notification` value has to be taken out before the file travels."""
    if not isinstance(value, yaml.ScalarNode):
        return True
    # the note saying the value was taken out is what this module's own rewrite
    # leaves behind, and re-reading it as a channel would withhold every file it
    # had just cleaned
    return value.tag not in HARMLESS and value.value != OMITTED_VALUE


def _without_notification(text: str) -> str:
    """`_steward.yaml` with the channel replaced by a note saying so.

    Continuation lines under the key are dropped with it, which covers the block scalar nobody should write and one person eventually will. Blank lines are kept, so the copy still reads like the file it came from.
    """
    lines: list[str] = []
    dropping: int | None = None
    for line in text.splitlines(keepends=True):
        if dropping is not None:
            if not line.strip():
                lines.append(line)
                continue
            # **deeper than the key, not merely indented.** A block mapping may
            # itself be indented, so testing for any leading space would eat
            # every sibling key below this one and leave a copy of the file
            # missing most of itself
            if _indent(line) > dropping:
                continue
            dropping = None
        if _NOTIFICATION.match(line) and not line.lstrip().startswith("#"):
            # the key's own indentation is kept, or the replacement would break
            # the mapping it sits in and land an unparseable copy
            lines.append(f"{' ' * _indent(line)}{OMITTED}\n")
            dropping = _indent(line)
            continue
        lines.append(line)
    return "".join(lines)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


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


__all__ = [
    "OMITTED",
    "OMITTED_VALUE",
    "SYNC_BUDGET",
    "SyncReport",
    "sync_target",
    "sync_workspace",
]
