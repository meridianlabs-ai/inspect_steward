"""A plain directory of `.eval` logs, read as a store.

**A directory already holds everything a table holds.** `task_identifier`'s `EvalLog` branch computes an identifier from a log's own header — which is exactly how `flow store import` rebuilds its Delta table in the first place — so what the table adds is an index, not information (execution.md §5.4). `observe_logs` groups any directory by identifier, headers only and concurrently, and its own docstring says it "knows nothing about what was *supposed* to run, which is what lets it serve `logs-archive/` and any other directory". This module is that sentence taken up on its offer.

**Which is what drops the `[flow]` extra for the common case.** A team that wants a Delta table builds one and gets one; everyone else points `log_store` at a shared prefix and needs nothing installed.

**Flat, because `observe_logs` is flat.** A log directory is one level by design (execution.md, *One flat directory*), and a store read here inherits that: logs in subdirectories of the store are not found. Stated rather than worked around — a nested store is a different shape from the one this reuses, and quietly recursing would make `search` and `publish` disagree about what the store contains. It is also what `withdrawn/` is built on: a withdrawn log leaves every reader's reach by being moved one level down, with nothing having to filter it.
"""

from collections.abc import Sequence, Set

from inspect_ai._util.file import FileSystem, basename, filesystem

from .._evalset.observe import LogAttempt, observe_logs
from .copy import copy_log
from .store import Published, StoreError

WITHDRAWN = "withdrawn"
"""Where a withdrawn log goes — inside the store, and invisible to a flat read."""

_MAX_COLLISIONS = 1000
"""How many logs of one name `withdrawn/` will hold, matching `_evalset.archive`."""


class DirectoryStore:
    """A directory of logs, indexed by reading their headers."""

    def __init__(self, location: str) -> None:
        self._location = location

    @property
    def location(self) -> str:
        return self._location

    def search(self, identifiers: Set[str]) -> dict[str, list[str]]:
        """Every log this directory holds for each wanted identifier, best first.

        **Ordered by flow's own rule**, so the two implementations rank one question one way: the most completed samples, and the more recent of a tie. The caller gets the whole list because that rank is manifest-blind and the caller is not — the log this ranks first can be the one that answers a different slice while the one behind it matches exactly.

        A log that would not read is skipped by `observe_logs` and reported nowhere here — a store degrades by having fewer answers, and an unreadable log in somebody else's directory is not this run's problem to name.

        Args:
            identifiers: What this run needs and does not have.

        Returns:
            Candidates per identifier, best first; an identifier with nothing here is absent.

        Raises:
            StoreError: The directory would not read.
        """
        if not identifiers:
            return {}
        try:
            observed = observe_logs(self._location)
        except Exception as ex:
            raise StoreError(
                f"{self._location} could not be read as a log store: {ex}"
            ) from ex
        found: dict[str, list[str]] = {}
        for identifier in identifiers:
            attempts = observed.attempts.get(identifier)
            if attempts:
                found[identifier] = [
                    attempt.location
                    for attempt in sorted(attempts, key=_quality, reverse=True)
                ]
        return found

    def publish(self, locations: Sequence[str]) -> Published:
        """Copy these logs into the directory.

        **Idempotent on the log's own name**, which is safe because that name is not arbitrary: inspect writes a timestamp, the task, and a hash, so a name already here is this log and copying it again would write the same bytes over themselves. A log genuinely superseded arrives under a *different* name and sits beside its predecessor — which is what `withdraw` is for, since nothing about sitting beside it makes the predecessor stop being chosen.

        **Idempotent on a *finished* copy, which is the part that was assumed rather than checked** — `copy_log` verifies the destination and stages every write, because a name is only evidence of a log where nothing was interrupted putting it there.

        **What was already here is reported apart from what this put here**, because in a directory store the file *is* the row: a log this run reused from this same store is found under its own name, skipped, and published — by its producer, not by this project. Counting it as ours is what let a later signoff withdraw somebody else's copy.

        **One log at a time, so one log's failure is one log's failure.** The copies are sequential and there is no transaction over them, which means a batch that stops partway has already put logs in the store that a reader can find. Raising on the first bad copy discarded that: the caller reported *nothing was published* about a store holding every log up to the ninth, journalled nothing, and left the operator with no record of what had actually landed — the shape of the error being **a failure reported as its own opposite**. So a copy that will not land is collected and the rest are attempted, and the two answers come back together. Only a directory that could not be created at all is a `StoreError`, because then there is genuinely nothing to report.

        Args:
            locations: Logs to publish, as `observe_logs` reported them.

        Returns:
            What was copied, which of it this call actually wrote, and what would not copy.

        Raises:
            StoreError: The directory could not be created.
        """
        if not locations:
            return Published(kind="copied", count=0)
        try:
            fs = filesystem(self._location)
            fs.mkdir(self._location, exist_ok=True)
        except Exception as ex:
            raise StoreError(
                f"{self._location} could not be created to publish into: {ex}"
            ) from ex
        copied = 0
        written: list[str] = []
        failed: list[str] = []
        for location in locations:
            target = self._location.rstrip(fs.sep) + fs.sep + basename(location)
            try:
                if copy_log(location, target, fs):
                    written.append(location)
                copied += 1
            except Exception:
                failed.append(location)
        return Published(
            kind="copied",
            count=copied,
            failed=tuple(failed),
            written=tuple(written),
        )

    def withdraw(self, locations: Sequence[str]) -> None:
        """Move these logs into `withdrawn/`, out of every reader's reach.

        **A move, on `logs-archive/`'s bargain exactly**: a published copy is the only one of itself in this store, so deleting it would be Steward destroying a result — and leaving it would be Steward handing out a result somebody withdrew. Moving it does neither, and `withdrawn/` sits *inside* the store rather than beside it because the store's own location is the only path this object is given.

        **Out of reach is what a flat read buys.** `observe_logs` does not recurse, so a subdirectory is invisible to `search` without anything having to filter it — the same property that makes a log directory's scan output safe to nest.

        **An earlier version of this did nothing at all**, arguing that `search`'s quality rule would outrank a superseded copy. It does not: quality is completed samples before recency, so a revoked four-sample log beats the two-sample one that supersedes it — including the case that matters most, where the replacement is short *because* a person accepted a hole in it. Every project reading the store would go on getting the withdrawn result.

        Args:
            locations: Logs that have left `logs/`, by the location they had there. Matched here on the filename, which is what `publish` wrote.

        Raises:
            StoreError: The store could not be written.
        """
        if not locations:
            return
        try:
            fs = filesystem(self._location)
            root = self._location.rstrip(fs.sep)
            present = {
                basename(location): f"{root}{fs.sep}{basename(location)}"
                for location in locations
            }
            withdrawing = {
                name: source for name, source in present.items() if fs.exists(source)
            }
            if not withdrawing:
                return
            destination = f"{root}{fs.sep}{WITHDRAWN}"
            fs.mkdir(destination, exist_ok=True)
            for name, source in withdrawing.items():
                fs.mv(source, _free(fs, destination, name))
        except Exception as ex:
            raise StoreError(
                f"logs could not be withdrawn from {self._location}: {ex}"
            ) from ex


def _free(fs: FileSystem, destination: str, name: str) -> str:
    """A name in `destination` that nothing is using, suffixing on collision.

    **The collision is real and the first attempt at this got it wrong by skipping.** Publish, withdraw, publish again, withdraw again is what a re-signoff over a directory somebody re-published does, and the second withdrawal meets its own earlier copy already sitting in `withdrawn/`. Declining to move then left the *newly republished* copy at the store root — searchable, chosen, and withdrawn twice over. Moving over the top of the first copy is the other wrong answer, since it destroys a result to tidy one away. So both are kept, exactly as `logs-archive/` keeps two attempts that reused a timestamp.

    Raises:
        OSError: If `destination` already holds `_MAX_COLLISIONS` logs of this name.
    """
    target = f"{destination}{fs.sep}{name}"
    if not fs.exists(target):
        return target
    stem, dot, extension = name.rpartition(".")
    stem, extension = (stem, f"{dot}{extension}") if stem else (name, "")
    for suffix in range(1, _MAX_COLLISIONS):
        candidate = f"{destination}{fs.sep}{stem}-{suffix}{extension}"
        if not fs.exists(candidate):
            return candidate
    raise OSError(f"{destination} already holds {_MAX_COLLISIONS} copies of {name}")


def _quality(attempt: LogAttempt) -> tuple[int, str]:
    """Flow's `is_better_log`, over what a header read already carries.

    Completed samples first, then recency. Flow breaks its tie on `stats.completed_at` where this uses `eval.created` — the one timestamp that survives `observe`'s mid-run header fallback, and the same ordering key the rest of Steward compares attempts on.
    """
    return (attempt.completed_samples, attempt.created)


__all__ = ["DirectoryStore"]
