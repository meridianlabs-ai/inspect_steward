"""The Delta table `flow store import` builds, read and written through `inspect_flow`.

**Nothing about the store is actually Flow's** — it is keyed on inspect_ai's `task_identifier` and `store_factory` accepts a bare path string rather than a `FlowSpec`, so the mechanism was already definition-type agnostic and only its *configuration surface* belonged to Flow (execution.md §5.4). What is Flow's is this implementation of it, and the dependency it carries: `inspect_flow` is an optional extra, so every import here is lazy and every failure to make one is a `StoreError` a caller turns into a warning.

**Steward never creates one.** A table exists because somebody ran `flow store import`, and `open_store` reaches this module only where the marker is already on disk — so the extra is required exactly where it was deliberately opted into, and a fresh `log_store` path gets the directory implementation instead.

**Two upstream shapes decide the code below.** `add_run_logs` is quiet and computes the identifier from each log's own header, which makes it the write path — where `import_log_path` looks like the write path and is not, printing flow's own display through the middle of a signoff. And `remove_log_prefix` prints two lines whatever its `verbose`, so withdrawal is skipped entirely when there is nothing to withdraw, which is the common case and keeps another tool's voice out of a signoff that had no rows to clear.
"""

from collections.abc import Sequence, Set
from typing import TYPE_CHECKING, Any

from inspect_ai.log import read_eval_log

from .store import Published, StoreError

if TYPE_CHECKING:
    from inspect_flow._store.store import FlowStoreInternal


class FlowTableStore:
    """A flow Delta table, holding `log_path → task_identifier` rows."""

    def __init__(self, location: str) -> None:
        """Open a handle on a table, without touching it yet.

        Args:
            location: The table, **already resolved** — `open_store` is the only constructor and `store_location` runs first, which is what lets `base_dir` below be a value nothing depends on.
        """
        self._location = location
        self._opened: "FlowStoreInternal | None" = None

    @property
    def location(self) -> str:
        return self._location

    def search(self, identifiers: Set[str]) -> dict[str, list[str]]:
        """Query the table for the logs it holds per wanted identifier, best first.

        Upstream reads each candidate's header, applies any store-level filter, picks the best by `is_better_log` and skips one that will not read — so the degradation this wants is already upstream's behaviour.

        **And the ones it did not pick are already in the answer**, as `StoreLogMatch.duplicate_logs`, which is what makes ordered candidates free here rather than a second query. Upstream's pick leads because it is upstream's pick; the rest follow in the order the table gave them, because nothing here knows a better one and inventing a rank would be Steward re-sorting a list it did not compute.

        Args:
            identifiers: What this run needs and does not have.

        Returns:
            Candidates per identifier the table could answer for, upstream's choice first.

        Raises:
            StoreError: The table could not be opened or queried.
        """
        if not identifiers:
            return {}
        store = self._open()
        try:
            matched = store.search_for_logs(set(identifiers))
        except Exception as ex:
            raise StoreError(f"{self._location} could not be queried: {ex}") from ex
        return {
            identifier: [match.log_file, *match.duplicate_logs]
            for identifier, match in matched.items()
        }

    def publish(self, locations: Sequence[str]) -> Published:
        """Append a row for each of these logs.

        **A log that will not read is stepped over rather than fatal.** By the time signoff publishes, the gate has already refused over every unreadable log or had one acknowledged by name — and an acknowledged one is precisely a log whose absence from the results was accepted, so failing the whole publication over it would lose every other row to a file somebody already decided about.

        **Stepped over and *named*, which it was not.** The skip was silent, so a publication that indexed eight of ten logs reported the eight and nothing else, and the two that a caller might want to go and look at existed in no record anywhere. They come back in `failed` now, on the same terms the directory implementation reports a copy that would not land.

        **The read is guarded broadly on purpose**, for the reason the module docstring gives about every other call here: the log being read may live on an fsspec backend whose failures are its own — a credential error is not an `OSError` — and one unreadable log must not become a traceback out of a signoff that has already recorded its signature.

        Args:
            locations: Logs to publish, as `observe_logs` reported them.

        Returns:
            What was indexed, and what would not read. `count` is logs published rather than rows appended: upstream drops an entry whose path it already holds, so a second signoff over an unchanged directory appends nothing and has still published everything it named.

        Raises:
            StoreError: The table could not be opened or written. The write is one call over the whole batch, so unlike a directory publish it does not partly land.
        """
        if not locations:
            return Published(kind="indexed", count=0)
        store = self._open()
        logs: list[Any] = []
        read: list[str] = []
        failed: list[str] = []
        for location in locations:
            try:
                logs.append(read_eval_log(location, header_only=True))
            except Exception:
                failed.append(location)
            else:
                read.append(location)
        if not logs:
            return Published(kind="indexed", count=0, failed=tuple(failed))
        try:
            store.add_run_logs(logs)
        except Exception as ex:
            raise StoreError(
                f"{self._location} could not be published to: {ex}"
            ) from ex
        # **`written` is everything, where a directory has to distinguish.** A
        # row here points at *this* run's own path, so a row for one of these
        # locations can only have been written by this project -- there is no
        # equivalent of finding somebody else's copy already under the name
        return Published(
            kind="indexed",
            count=len(logs),
            failed=tuple(failed),
            written=tuple(read),
        )

    def withdraw(self, locations: Sequence[str]) -> None:
        """Remove the rows pointing at these logs.

        Exact paths as prefixes, which is what upstream's prefix matcher reduces to for a full path and avoids inventing a second removal API for one caller.

        Args:
            locations: Logs that have left `logs/`.

        Raises:
            StoreError: The table could not be opened or written.
        """
        if not locations:
            return
        store = self._open()
        try:
            store.remove_log_prefix(list(locations), verbose=False)
        except Exception as ex:
            raise StoreError(
                f"rows in {self._location} could not be withdrawn: {ex}"
            ) from ex

    def _open(self) -> "FlowStoreInternal":
        """The table, opened once and kept.

        Raises:
            StoreError: `inspect_flow` is not installed, or the table would not open.
        """
        if self._opened is not None:
            return self._opened
        try:
            from inspect_flow._store.store import store_factory
        except ImportError as ex:
            raise StoreError(
                f"{self._location} is a flow log store and inspect_flow is not "
                f"installed — `pip install inspect_steward[flow]`, or point "
                f"log_store at a plain directory of logs, which needs nothing"
            ) from ex
        try:
            store = store_factory(self._location, base_dir=".", quiet=True)
        except Exception as ex:
            raise StoreError(f"{self._location} could not be opened: {ex}") from ex
        if store is None:  # pragma: no cover - the marker was there a moment ago
            raise StoreError(
                f"{self._location} carries a flow store marker and no table"
            )
        self._opened = store
        return store


__all__ = ["FlowTableStore"]
