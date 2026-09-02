"""Which implementation a location gets, and what each of them does with it.

The dispatch is the whole of the `[flow]` question: a Delta table is read through `inspect_flow` and everything else is a directory of logs read through `observe_logs`, so the extra is required exactly where somebody deliberately built one and nowhere else. What makes the directory affordable is that `task_identifier`'s `EvalLog` branch computes an identifier from a log's own header — a directory of logs already holds what a table holds, and the table adds an index rather than information.
"""

from pathlib import Path

import pytest
from inspect_steward._store import (
    FLOW_STORE_MARKER,
    DirectoryStore,
    FlowTableStore,
    StoreError,
    copy,
    default_location,
    open_store,
    store_location,
)

from .._logs import SynthTask, write_log

ADDITION = SynthTask("addition", samples=4)
ECHO = SynthTask("echo", samples=2)


def store_dir(tmp_path: Path) -> Path:
    location = tmp_path / "store"
    location.mkdir()
    return location


class TestWhichImplementationALocationGets:
    """Dispatch on the target, never on the definition type."""

    def test_a_flow_marker_means_the_table(self, tmp_path: Path) -> None:
        location = store_dir(tmp_path)
        (location / FLOW_STORE_MARKER).mkdir()

        assert isinstance(open_store(str(location), root=tmp_path), FlowTableStore)

    def test_a_plain_directory_of_logs_means_the_directory(
        self, tmp_path: Path
    ) -> None:
        location = store_dir(tmp_path)
        write_log(location, ADDITION)

        assert isinstance(open_store(str(location), root=tmp_path), DirectoryStore)

    def test_and_so_does_a_location_nothing_has_created(self, tmp_path: Path) -> None:
        # what makes publication work against a fresh path without anybody
        # installing an extra: Steward never creates a table, so a location
        # that is not one yet is never going to become one here
        assert isinstance(
            open_store(str(tmp_path / "not-yet"), root=tmp_path), DirectoryStore
        )

    def test_auto_is_the_machines_store_and_not_a_directory_called_auto(
        self,
    ) -> None:
        # **it was taken literally**, so `log_store: auto` opened `./auto` — a
        # *relative* path, so the same setting named a different store for every
        # working directory a command happened to be run from
        store = open_store("auto", root=Path.cwd())

        assert store.location == default_location()
        assert Path(store.location).is_absolute()

    def test_the_marker_is_read_without_importing_flow(self, tmp_path: Path) -> None:
        # the reason the check is duplicated rather than delegated to flow's own
        # `store_exists`: deciding *which* implementation to use must not
        # require the implementation, or the common case pays for the uncommon
        # one. Opening is where the dependency is finally needed
        location = store_dir(tmp_path)
        (location / FLOW_STORE_MARKER).mkdir()

        store = open_store(str(location), root=tmp_path)

        assert store.location == str(location)


class TestADirectoryOfLogs:
    """`search`, `publish`, and the withdrawal that has to actually withdraw."""

    def test_the_best_log_per_wanted_identifier(self, tmp_path: Path) -> None:
        # flow's own rule, so both implementations answer one question one way:
        # most completed samples, and the more recent of a tie
        location = store_dir(tmp_path)
        write_log(location, ADDITION, completed=1, created="2026-01-01T00:00:00+00:00")
        best = write_log(
            location, ADDITION, completed=4, created="2026-01-02T00:00:00+00:00"
        )
        write_log(location, ADDITION, completed=2, created="2026-01-03T00:00:00+00:00")

        found = DirectoryStore(str(location)).search({ADDITION.identifier})

        # a `file://` URI, as `observe_logs` reports every location and as
        # flow's table stores every path — one form on both sides of the store
        assert found[ADDITION.identifier][0].endswith(best.name)

    def test_every_candidate_comes_back_best_first(self, tmp_path: Path) -> None:
        # **the store's rank cannot see the question, so the caller gets the
        # list.** A manifest-blind index can only order by size and recency, and
        # the log it puts first may be the one answering a different slice while
        # the one behind it matches exactly — so returning a single answer made
        # the caller's check a veto rather than a filter
        location = store_dir(tmp_path)
        small = write_log(
            location, ADDITION, completed=1, created="2026-01-01T00:00:00+00:00"
        )
        big = write_log(
            location, ADDITION, completed=4, created="2026-01-02T00:00:00+00:00"
        )

        found = DirectoryStore(str(location)).search({ADDITION.identifier})

        assert [Path(one).name for one in found[ADDITION.identifier]] == [
            big.name,
            small.name,
        ]

    def test_an_identifier_with_nothing_here_is_absent(self, tmp_path: Path) -> None:
        # absent rather than mapped to nothing, so a caller iterates matches
        # instead of filtering misses
        location = store_dir(tmp_path)
        write_log(location, ADDITION)

        found = DirectoryStore(str(location)).search(
            {ADDITION.identifier, ECHO.identifier}
        )

        assert set(found) == {ADDITION.identifier}

    def test_a_location_that_does_not_exist_answers_nothing(
        self, tmp_path: Path
    ) -> None:
        # an empty observation rather than an error: a store whose absence costs
        # time and never correctness cannot start by raising
        store = DirectoryStore(str(tmp_path / "missing"))

        assert store.search({ADDITION.identifier}) == {}

    def test_publishing_copies_and_says_so(self, tmp_path: Path) -> None:
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"

        published = DirectoryStore(str(location)).publish([str(landed)])

        assert published.kind == "copied"
        assert published.count == 1
        assert (location / landed.name).exists()

    def test_publishing_twice_writes_once(self, tmp_path: Path) -> None:
        # idempotent on the log's own name, which is safe because that name is
        # not arbitrary -- a timestamp, the task and a hash, so a name already
        # there is this log
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        store = DirectoryStore(str(tmp_path / "store"))

        store.publish([str(landed)])
        again = store.publish([str(landed)])

        assert again.count == 1
        assert len(list((tmp_path / "store").iterdir())) == 1

    def test_a_published_log_is_findable(self, tmp_path: Path) -> None:
        # the round trip the whole feature is: publish here, search there
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        store = DirectoryStore(str(tmp_path / "store"))

        store.publish([str(landed)])

        assert set(store.search({ADDITION.identifier})) == {ADDITION.identifier}

    def test_a_withdrawn_log_is_moved_rather_than_deleted(self, tmp_path: Path) -> None:
        # `logs-archive/`'s bargain one level out: a published copy is the only
        # one of itself here, so deleting it would destroy a result -- and
        # leaving it in place would hand out a result somebody withdrew
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"
        store = DirectoryStore(str(location))
        store.publish([str(landed)])

        store.withdraw([str(source / landed.name)])

        assert not (location / landed.name).exists()
        assert (location / "withdrawn" / landed.name).exists()

    def test_and_is_then_out_of_the_search_it_used_to_win(self, tmp_path: Path) -> None:
        # **the reason a no-op withdrawal was wrong, as the case that shows it.**
        # Quality is completed samples before recency, so a revoked 4/4 result
        # outranks the 2/4 that supersedes it -- and the 2/4 is short precisely
        # because somebody accepted a hole in it. Ranking alone never recovers
        source = tmp_path / "logs"
        revoked = write_log(
            source, ADDITION, completed=4, created="2026-01-01T00:00:00+00:00"
        )
        replacement = write_log(
            source, ADDITION, completed=2, created="2026-02-01T00:00:00+00:00"
        )
        store = DirectoryStore(str(tmp_path / "store"))
        store.publish([str(revoked), str(replacement)])
        assert store.search({ADDITION.identifier})[ADDITION.identifier][0].endswith(
            revoked.name
        )

        store.withdraw([str(revoked)])

        assert [
            Path(one).name
            for one in store.search({ADDITION.identifier})[ADDITION.identifier]
        ] == [replacement.name]

    def test_withdrawing_a_log_that_was_never_published_does_nothing(
        self, tmp_path: Path
    ) -> None:
        store = DirectoryStore(str(tmp_path / "store"))

        store.withdraw([str(tmp_path / "logs" / "never-here.eval")])

        assert not (tmp_path / "store" / "withdrawn").exists()

    def test_withdrawing_twice_leaves_nothing_searchable(self, tmp_path: Path) -> None:
        # **publish, withdraw, publish, withdraw** — what a re-signoff over a
        # re-published directory does. Declining the second move because
        # `withdrawn/` already held that name left the *newly republished* copy
        # sitting at the searchable root, withdrawn twice and gone neither time
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"
        store = DirectoryStore(str(location))

        for _ in range(2):
            store.publish([str(landed)])
            store.withdraw([str(source / landed.name)])

        assert store.search({ADDITION.identifier}) == {}
        assert not (location / landed.name).exists()

    def test_and_keeps_both_copies_it_withdrew(self, tmp_path: Path) -> None:
        # moving over the top of the first would destroy a result to tidy one
        # away, so they sit side by side exactly as `logs-archive/` keeps two
        # attempts that reused a timestamp
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"
        store = DirectoryStore(str(location))

        for _ in range(2):
            store.publish([str(landed)])
            store.withdraw([str(source / landed.name)])

        assert len(list((location / "withdrawn").iterdir())) == 2

    def test_publishing_nothing_is_not_an_error(self, tmp_path: Path) -> None:
        published = DirectoryStore(str(tmp_path / "store")).publish([])

        assert published.count == 0

    def test_a_copy_that_will_not_land_does_not_take_the_batch_with_it(
        self, tmp_path: Path
    ) -> None:
        # **it raised, which announced a partial success as its opposite.** The
        # copies are sequential and nothing wraps them, so a batch that stopped
        # partway had already put logs where a reader would find them -- and the
        # caller, which turns a `StoreError` into *nothing was published*, then
        # said exactly that about a store holding all but the last few
        source = tmp_path / "logs"
        missing = source / "never-written.eval"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"

        # the bad one first, so this asserts the rest are still attempted rather
        # than that the good one happened to come before the failure
        published = DirectoryStore(str(location)).publish([str(missing), str(landed)])

        assert published.count == 1
        assert published.failed == (str(missing),)
        assert (location / landed.name).exists()

    def test_and_a_publication_with_nothing_to_report_reports_nothing(
        self, tmp_path: Path
    ) -> None:
        # the ordinary case, and the only one worth saying nothing about
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)

        published = DirectoryStore(str(tmp_path / "store")).publish([str(landed)])

        assert published.failed == ()


class TestWhereTheSettingActuallyPoints:
    """One `log_store`, one store, from wherever the command was typed.

    A Steward command runs from anywhere at or below the workspace, and `log_store` is resolved at launch and again at signoff — so a relative setting used to name one directory to a launch typed at the root and a different one to a signoff typed inside `logs/`. The same setting, two stores, and reuse that quietly stopped finding what publication had put there.
    """

    def test_a_relative_path_is_relative_to_the_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "workspace"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert store_location("../shared", root) == str(tmp_path / "shared")

    def test_and_says_the_same_thing_from_two_working_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the defect itself: the root is the thing the setting is written down
        # in, and it does not move
        root = tmp_path / "workspace"
        (root / "logs").mkdir(parents=True)

        monkeypatch.chdir(root)
        from_root = store_location("./store", root)
        monkeypatch.chdir(root / "logs")
        from_below = store_location("./store", root)

        assert from_root == from_below == str(root / "store")

    def test_an_absolute_path_is_already_where_it_says(self, tmp_path: Path) -> None:
        assert store_location(str(tmp_path / "store"), tmp_path / "elsewhere") == str(
            tmp_path / "store"
        )

    @pytest.mark.parametrize(
        "location", ["s3://bucket/prefix", "gs://bucket/prefix", "file:///var/store"]
    )
    def test_a_uri_is_never_joined_onto_a_local_root(
        self, location: str, tmp_path: Path
    ) -> None:
        # a bucket joined onto a local root is a path that is neither, and
        # `file://` is the one a filesystem-level *is this local* check gets
        # wrong -- it is local and it still needs nothing to be relative to
        assert store_location(location, tmp_path) == location

    def test_auto_is_the_machines_store_wherever_the_root_is(
        self, tmp_path: Path
    ) -> None:
        assert store_location("auto", tmp_path) == default_location()

    def test_resolving_an_already_resolved_location_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        # it runs at the caller and again inside `open_store`, so it has to be
        # idempotent or the second pass would rebase the first pass's answer
        once = store_location("./store", tmp_path)

        assert store_location(once, tmp_path) == once


class TestFailuresThatMustNotEscapeAsThemselves:
    """Every caller promises to degrade a store failure to a warning, and makes that promise after a manifest is committed or a signature recorded.

    The operations underneath run on fsspec backends with their own exception hierarchies — `botocore.exceptions.NoCredentialsError` for an S3 store on a machine with no credentials, which is an ordinary Tuesday — and none of them are `OSError`. A leaked provider exception at either of those moments is a traceback where a sentence belonged.
    """

    @pytest.mark.parametrize("location", ["s3://", "s3://no-such-bucket-xyz/prefix"])
    def test_a_remote_location_that_will_not_open_is_a_store_error(
        self, location: str, tmp_path: Path
    ) -> None:
        with pytest.raises(StoreError):
            open_store(location, root=tmp_path)

    def test_a_directory_that_is_a_file_is_a_store_error(self, tmp_path: Path) -> None:
        blocked = tmp_path / "store"
        blocked.write_text("not a directory", encoding="utf-8")
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)

        with pytest.raises(StoreError):
            DirectoryStore(str(blocked)).publish([str(landed)])


class TestAFlowTable:
    """The Delta implementation, against a real table."""

    def table(self, tmp_path: Path) -> str:
        """A store flow itself created, which is the only way one ever exists."""
        deltalake = pytest.importorskip("inspect_flow._store.deltalake")
        location = str(tmp_path / "store")
        deltalake.DeltaLakeStore(location, create=True)
        return location

    def test_a_published_log_comes_back_from_a_search(self, tmp_path: Path) -> None:
        location = self.table(tmp_path)
        landed = write_log(tmp_path / "logs", ADDITION)
        store = FlowTableStore(location)

        published = store.publish([str(landed)])

        assert published.kind == "indexed"
        assert set(store.search({ADDITION.identifier})) == {ADDITION.identifier}

    def test_withdrawal_takes_the_row_back_out(self, tmp_path: Path) -> None:
        location = self.table(tmp_path)
        landed = write_log(tmp_path / "logs", ADDITION)
        store = FlowTableStore(location)
        store.publish([str(landed)])

        store.withdraw([str(landed)])

        assert store.search({ADDITION.identifier}) == {}
        # a pointer removed and the log left exactly where it was
        assert landed.exists()

    def test_a_log_that_will_not_read_is_stepped_over(self, tmp_path: Path) -> None:
        # by the time signoff publishes, the gate has refused over every
        # unreadable log or had one acknowledged by name -- and an acknowledged
        # one is a log whose absence from the results was accepted, so losing
        # every other row to it would be the wrong trade
        location = self.table(tmp_path)
        landed = write_log(tmp_path / "logs", ADDITION)
        torn = tmp_path / "logs" / "torn.eval"
        torn.write_text("not a log", encoding="utf-8")

        published = FlowTableStore(location).publish([str(landed), str(torn)])

        assert published.count == 1

    def test_publishing_the_same_log_twice_still_reports_it(
        self, tmp_path: Path
    ) -> None:
        # `count` is logs published rather than rows appended: upstream drops an
        # entry whose path it already holds, so counting rows would report `0`
        # for a publication that succeeded completely
        location = self.table(tmp_path)
        landed = write_log(tmp_path / "logs", ADDITION)
        store = FlowTableStore(location)

        store.publish([str(landed)])

        assert store.publish([str(landed)]).count == 1

    def test_a_marker_with_no_table_under_it_is_reported(self, tmp_path: Path) -> None:
        pytest.importorskip("inspect_flow._store.store")
        location = store_dir(tmp_path)
        (location / FLOW_STORE_MARKER).mkdir()

        with pytest.raises(StoreError):
            FlowTableStore(str(location)).search({ADDITION.identifier})


class TestACopyThatWasInterrupted:
    """A name is only evidence of a log where nothing was interrupted putting it there.

    Publication is idempotent on the log's own name, and rightly so: inspect writes a timestamp, the task and a hash, so a name already there is this log. What that reasoning never checked is that the file under the name is a *finished* one. An interrupted copy sits at the final path under the final name, so the next publication called it already published and reported a success — while `observe_logs` could not read it and the store answered no query about it.
    """

    def wrecked(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """A store holding the truncated remains of a copy of `landed`."""
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"
        location.mkdir()
        whole = landed.read_bytes()
        (location / landed.name).write_bytes(whole[: len(whole) // 3])
        return landed, location, location / landed.name

    def test_a_partial_file_is_replaced_rather_than_counted(
        self, tmp_path: Path
    ) -> None:
        landed, location, target = self.wrecked(tmp_path)

        published = DirectoryStore(str(location)).publish([str(landed)])

        assert published.count == 1
        assert target.read_bytes() == landed.read_bytes()

    def test_and_the_log_is_findable_where_it_was_not(self, tmp_path: Path) -> None:
        # the reproduction exactly: `count=1, failed=()` over a store that
        # answered nothing at all
        landed, location, _ = self.wrecked(tmp_path)
        store = DirectoryStore(str(location))

        store.publish([str(landed)])

        assert set(store.search({ADDITION.identifier})) == {ADDITION.identifier}

    def test_a_complete_copy_already_there_is_left_exactly_alone(
        self, tmp_path: Path
    ) -> None:
        # overwriting it would be Steward rewriting a result to no purpose, and
        # the idempotence the publish docstring promises rests on not doing it
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"
        store = DirectoryStore(str(location))
        store.publish([str(landed)])
        target = location / landed.name
        stamped = target.stat().st_mtime_ns

        store.publish([str(landed)])

        assert target.stat().st_mtime_ns == stamped

    def test_nothing_is_left_at_the_final_path_by_a_copy_that_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **the failure that produced the wreckage in the first place.** The
        # copy lands beside the target under a `.part` name and is renamed into
        # place, so a reader sees the log or nothing and never a prefix of it
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        location = tmp_path / "store"

        def truncating(source_path: str, target_path: str) -> None:
            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            Path(target_path).write_bytes(Path(source_path).read_bytes()[:64])
            raise OSError("the connection dropped")

        monkeypatch.setattr(copy, "copy_file", truncating)

        published = DirectoryStore(str(location)).publish([str(landed)])

        assert published.count == 0
        assert published.failed == (str(landed),)
        assert not (location / landed.name).exists()
        # and the staging file is cleared rather than left to accumulate
        assert list(location.iterdir()) == []


class TestWhoseCopyItIs:
    """A directory store's row *is* the file, so publishing cannot mean the same thing for one that was already there.

    A log this run reused from this same store is found under its own name and skipped — published, by whoever produced it. Recording it as this project's own is what let a later signoff, archiving that attempt as superseded, move the producer's shared copy out of everybody's reach.
    """

    def test_a_log_this_call_wrote_is_claimed(self, tmp_path: Path) -> None:
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)

        published = DirectoryStore(str(tmp_path / "store")).publish([str(landed)])

        assert published.written == (str(landed),)

    def test_a_log_already_there_is_published_and_not_claimed(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "logs"
        landed = write_log(source, ADDITION)
        store = DirectoryStore(str(tmp_path / "store"))
        store.publish([str(landed)])

        again = store.publish([str(landed)])

        # published — it is in the store and the signature covers it — and not
        # this call's to own, which is what withdrawal later reads
        assert again.count == 1
        assert again.written == ()
