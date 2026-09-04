"""*Where have these logs already been run* — asked at launch, answered at signoff.

A store is an index of completed logs keyed on inspect_ai's `task_identifier`, holding pointers rather than data. Steward reads it once at launch, so a task another project already ran is copied in rather than run again, and writes to it once at signoff, so what it holds is **results an operator accepted** rather than logs that happen to exist (execution.md §5.3–5.6). Its absence costs time and never correctness, which is what makes every failure on these paths a warning.

**Two targets, dispatched on what is there rather than on what the definition is.** A `flow_store/` marker means somebody built a Delta table with `flow store import`, and it is read and written through `inspect_flow`. Anything else — an existing directory of `.eval` logs, or a location nothing has created yet — is a plain directory, which is a store too: `task_identifier`'s `EvalLog` branch computes an identifier from a log's own header, so a directory of logs already holds everything a table holds and the table adds an index rather than information. `observe_logs` reads one directly.

**The marker is checked here rather than through flow's own `store_exists`, and the small duplication is the point.** Deciding *which* implementation to use must not require the implementation: a workspace with no `inspect_flow` installed still has to be able to tell a table from a directory, or the common case pays for the uncommon one. So a missing `[flow]` extra can only ever be reported against a store somebody deliberately built with flow, which is the wrinkle execution.md §5.6 otherwise has to keep explaining.

**Reads unify and writes do not.** Publishing to a table appends a pointer; publishing to a directory copies the log. Both are *publication*, and a caller reporting what it did has to say which — so `Published` carries the act and not only the count. Withdrawal splits the same way and reaches the same place: a table drops the row, a directory *moves* the file into `withdrawn/`, which is `logs-archive/`'s idiom one level out — nothing is destroyed, and nothing that was superseded stays reusable.

**Every method normalizes its failures to `StoreError`, including the ones nothing here raised.** This is the rare place a bare `except Exception` is the correct instrument rather than a shrug: the operations underneath run on fsspec backends that raise their own hierarchies — `botocore.exceptions.NoCredentialsError` for an S3 store on a machine with no credentials configured, which is an ordinary Tuesday rather than an exotic failure — and none of them are `OSError`. Every caller of this module has promised to degrade a store failure to a warning, and it makes those promises at two of the worst possible moments: after a launch has committed its manifest, and after a signoff has recorded its signature. A leaked provider exception at either point is a traceback where a sentence belonged.
"""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import platformdirs
from inspect_ai._util.file import filesystem

AUTO = "auto"
"""What `log_store: auto` says: the machine's own store, wherever that is."""

FLOW_PACKAGE = "inspect_flow"
"""The package whose data directory `auto` resolves to. A string rather than an import, exactly as `FLOW_STORE_MARKER` is."""

FLOW_STORE_MARKER = "flow_store"
"""The subdirectory `flow store import` creates, and the whole of the dispatch.

Flow's own `store_exists` checks for exactly this (`inspect_flow._store.store`), and this is the one fact about the table implementation worth knowing without importing it.
"""


class StoreError(Exception):
    """A store was configured and could not be opened.

    A message for an operator, never a traceback: everything reachable here is a condition of the machine — a location that will not read, or a Delta table with no `inspect_flow` to read it. Every caller turns one of these into a warning rather than a failure, because a store is an optimisation.
    """


@dataclass(frozen=True)
class Published:
    """What a publication did, which is not the same act at both targets."""

    kind: Literal["indexed", "copied"]
    """`indexed` for a pointer appended to a table, `copied` for a log copied into a directory. Reported rather than inferred, so `--publish` can say what happened to somebody's disk."""

    count: int
    """Logs published.

    **Logs handed over, not rows appended.** A table publish is idempotent — flow drops an entry whose path it already holds for that identifier — so a second signoff over an unchanged directory adds nothing and has still published every log it named. Counting rows would report `0` for a publication that succeeded completely, which is the wrong sentence about the right outcome.
    """

    failed: tuple[str, ...] = ()
    """Logs this publication did not put in the store, if any.

    **A partial publication is a real outcome and used to have no way to be reported.** The directory implementation copies one log at a time, so a permission error on the ninth of ten leaves eight logs in the store and raises — and the caller, which turns a `StoreError` into *nothing was published*, then said exactly that about a store holding eight new logs. Neither half of that is recoverable from the other: the count is what the signature covers and the failures are what somebody has to go and look at, so both are returned and the caller reports both.

    Empty is the ordinary case and the only one worth saying nothing about.
    """

    written: tuple[str, ...] = ()
    """Logs this call actually put in the store, as opposed to found already there.

    **The difference is provenance, and without it a project can withdraw somebody else's copy.** A directory store's row *is the file*, keyed on the log's own name — so publishing a log this run **reused** from that same store finds the name already taken, skips the copy, and counts it published. It was published, by whoever produced it. Recording it as this project's own is what let a later signoff, archiving that attempt as superseded, move the producer's shared copy into `withdrawn/` and end reuse of it for everybody.

    So `count` says what the signature covers and this says what this project owns. A table publish writes a row pointing at *this* run's own path and never at anybody else's, so there the two coincide.
    """


class LogStore(Protocol):
    """An index of completed logs, keyed on `task_identifier`."""

    @property
    def location(self) -> str:
        """Where the store is, as it was configured."""
        ...

    def search(self, identifiers: Set[str]) -> dict[str, list[str]]:
        """Find the logs a store holds for each wanted identifier, best first.

        **Every candidate, not the best one, and the difference is a whole class of missed reuse.** A store is manifest-blind by construction — it indexes on `task_identifier`, which deliberately excludes the sample count, the epochs and the selection — so *best* here can only mean *most samples, most recently*. The caller is the one holding the manifest, and the log this store ranks first may be the one log that answers a different question while the one behind it matches exactly. Returning a single answer made the caller's check a veto rather than a filter: it rejected the front candidate and never learned the store had what it asked for.

        Args:
            identifiers: What this run needs and does not have.

        Returns:
            Per identifier, its candidates in descending order of size and recency. An identifier with nothing indexed is **absent** rather than mapped to an empty list, so a caller iterates matches rather than filtering misses.

        Raises:
            StoreError: The store could not be read at all. A single unreadable log is skipped instead — a store degrades by having fewer answers, never by refusing to have any.
        """
        ...

    def publish(self, locations: Sequence[str]) -> Published:
        """Put these logs into the store.

        **A publication that only partly lands reports itself rather than raising**, and the distinction an implementation has to draw is between *this store did not take anything* and *this store took some of it*. Only the first is a `StoreError`. The second returns, with the logs that did not land named in `Published.failed`, because raising would discard the count of what did — and the caller's honest report of a store now holding eight of ten new logs cannot be assembled from an exception that mentions one of them.

        Args:
            locations: Logs to publish, as `observe_logs` reported them.

        Returns:
            What was done, how much of it, and what did not land.

        Raises:
            StoreError: The store could not be written **at all** — it would not open, or the whole batch was refused as one.
        """
        ...

    def withdraw(self, locations: Sequence[str]) -> None:
        """Take these logs back out of the store, so nothing reuses them.

        Called where signoff has just archived a superseded attempt: an earlier signoff may have published it, and what it points at is no longer the result.

        **It has to actually remove them from reach, which an earlier version of this got wrong.** That version made the directory implementation a no-op, on the argument that `search` ranks by quality so a superseded copy would be outranked by whatever replaced it. It is not: quality is completed samples before recency, so a revoked four-sample result outranks the two-sample one that supersedes it, and the store goes on handing out the log this project explicitly withdrew. The directory therefore *moves* the file into `withdrawn/`, which is `logs-archive/`'s bargain one level out — reversible, nothing destroyed, and out of `search`'s reach because a store is read flat.

        **Returns nothing, because neither implementation can honestly say how many.** Upstream's removal reports its count to its own display and not to its caller. Its success is that it did not raise.

        Args:
            locations: Logs that have left `logs/`, by the location they had there. Matched into the store on the log's own filename, which is what publication put there.

        Raises:
            StoreError: The store could not be written.
        """
        ...


def default_location() -> str:
    """Where `auto` means, which is the machine's store rather than a new one.

    **Flow's data directory, named as a path convention rather than reached through an import** — the same trade `FLOW_STORE_MARKER` makes and for the same reason: the common case must not pay the `[flow]` extra to find out where it is pointing. Resolving `auto` to a directory of Steward's own would be worse than useless on the machine `auto` is *for*: somebody who already ran `flow store import` has a table, and a default that could not see it would quietly index a second empty store beside the one they meant.
    """
    return str(Path(platformdirs.user_data_dir(FLOW_PACKAGE)))


def store_location(location: str, root: Path) -> str:
    """What `log_store` actually names, resolved once so every reader agrees.

    **A store is addressed from more than one working directory, which is the whole reason this exists.** `log_store` is resolved at launch and again at signoff, and a Steward command runs from anywhere at or below the workspace (the root is found by walking up). So a configured `../shared` meant one directory to a launch typed at the root and a different one to a signoff typed inside `logs/` — the same setting, two stores, and reuse that silently stopped finding what publication had put there. Resolving against the **workspace root** rather than the process's cwd is what makes the setting name one place: the root is the thing the setting is written down in, and it does not move.

    Three cases, and only the middle one is new:

    - **`auto`** is the machine's own store rather than a relative directory called `auto`, which is what a literal reading opened.
    - **A relative path** is relative to the workspace, and comes back absolute.
    - **A URI** (`s3://…`, `gs://…`, and `file://` too) is already absolute and is left exactly as written — joining a bucket onto a local root would produce a path that is neither.

    **A scheme is what tells the third case from the second**, rather than asking the filesystem layer: `filesystem()` calls a `file://` URI local, which is true and is not the question — the question is whether this string still needs somewhere to be relative *to*, and a URI never does. Testing the scheme also cannot raise, which matters because two callers resolve a location before anything is in a position to turn a failure into a warning.

    Args:
        location: Where the store is, as `resolve_log_store` gave it.
        root: The workspace root, which relative paths resolve against.

    Returns:
        The location, resolved. Idempotent: resolving an already-resolved location returns it unchanged.
    """
    if location.strip().lower() == AUTO:
        return default_location()
    if urlparse(location).scheme:
        return location
    return str(Path(root, location).resolve())


def open_store(location: str, *, root: Path) -> LogStore:
    """Open the store at `location`, choosing the implementation from what is there.

    Args:
        location: Where the store is, as `resolve_log_store` gave it — a path, or `auto` for the machine's own.
        root: The workspace root, which a relative location resolves against. Required rather than defaulted, because the default that suggests itself is the process's cwd and that is precisely the bug `store_location` exists to close.

    Returns:
        The table implementation where the location already carries flow's marker, and the directory implementation otherwise — **including a location nothing has created yet**, which is what makes publication work against a fresh path without anybody installing an extra.

    Raises:
        StoreError: The location would not read, or it is a Delta table and `inspect_flow` is not installed.
    """
    resolved = store_location(location, root)
    if _flow_table(resolved):
        from .flow import FlowTableStore

        return FlowTableStore(resolved)

    from .directory import DirectoryStore

    return DirectoryStore(resolved)


def _flow_table(location: str) -> bool:
    """Whether this location is a Delta table somebody built with `flow store import`.

    Raises:
        StoreError: The location would not read. Answered here rather than left to the implementation, because a location that will not answer this question will not answer any of them.
    """
    try:
        fs = filesystem(location)
        return fs.exists(location.rstrip(fs.sep) + fs.sep + FLOW_STORE_MARKER)
    except Exception as ex:
        raise StoreError(f"{location} could not be read as a log store: {ex}") from ex


__all__ = [
    "AUTO",
    "FLOW_PACKAGE",
    "FLOW_STORE_MARKER",
    "LogStore",
    "Published",
    "StoreError",
    "default_location",
    "open_store",
    "store_location",
]
