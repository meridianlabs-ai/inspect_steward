"""Reading `_steward.md`.

Three claims, and they are the whole point of the file: the front matter is read and the prose is not, a setting that belongs elsewhere is refused by name rather than ignored, and the command line outranks the workspace.
"""

from pathlib import Path

import pytest
from inspect_steward._schedule import DEFAULT_STALL_AFTER
from inspect_steward._workspace import (
    DEFAULT_TEND_INTERVAL,
    REFUSED,
    Directives,
    DirectivesError,
    create_workspace,
    read_directives,
    resolve_interval,
    resolve_pool,
)


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "_steward.md"
    path.write_text(text, encoding="utf-8")
    return path


PARSED: list[tuple[str, str, int | None]] = [
    ("no front matter at all", "# _steward.md\n\nnever spend over $200.\n", None),
    ("an empty front matter", "---\n---\n\nprose.\n", None),
    ("nothing but comments", "---\n# max_workers: 8\n---\n", None),
    ("a setting", "---\nmax_workers: 8\n---\n\nprose.\n", 8),
    ("no body", "---\nmax_workers: 3\n---\n", 3),
    ("a rule in the body", "---\nmax_workers: 4\n---\n\nfirst\n\n---\n\nsecond\n", 4),
    ("trailing space on the fences", "--- \nmax_workers: 5\n--- \n", 5),
]


@pytest.mark.parametrize(
    ("text", "max_workers"),
    [(text, expected) for _, text, expected in PARSED],
    ids=[case for case, _, _ in PARSED],
)
def test_the_front_matter_is_read_and_the_body_is_not(
    text: str, max_workers: int | None, tmp_path: Path
) -> None:
    assert read_directives(written(tmp_path, text)).max_workers == max_workers


def test_a_workspace_with_no_file_expressed_no_preferences(tmp_path: Path) -> None:
    # absent is a workspace that said nothing, not a workspace that is broken
    assert read_directives(tmp_path / "_steward.md") == Directives()


def test_the_file_init_writes_parses(tmp_path: Path) -> None:
    # the template ships a commented-out front matter, so it must survive being
    # read as one -- a broken fence there would break every workspace at once
    workspace = create_workspace(tmp_path, git=False).workspace
    assert read_directives(workspace.directives) == Directives()


REJECTED: list[tuple[str, str, str]] = [
    ("a fence that never closes", "---\nmax_workers: 8\n\nprose\n", "never closed"),
    ("front matter that is not yaml", "---\nmax_workers: [8\n---\n", "not valid YAML"),
    ("front matter that is not a mapping", "---\n- max_workers\n---\n", "a mapping"),
    ("a key the definition owns", "---\nlog_dir: out/\n---\n", "eval_set()"),
    ("sample concurrency", "---\nmax_samples: 40\n---\n", "eval_set()"),
    (
        "a notification url",
        "---\nnotify: slack://tok@chan\n---\n",
        "INSPECT_EVAL_NOTIFICATION",
    ),
    ("a typo", "---\nmax_wokrers: 8\n---\n", "not a setting Steward knows"),
    ("a meaningless ceiling", "---\nmax_workers: 0\n---\n", "greater than 0"),
    ("a ceiling that is not a number", "---\nmax_workers: lots\n---\n", "max_workers"),
    # YAML rewrites all four of these before pydantic ever sees them, and
    # pydantic's default would rewrite them again into a plausible integer --
    # `yes` all the way to 1, which would throttle a fleet to one worker and
    # say nothing. The error has to name the value that arrived, because that
    # is the only way the author learns what YAML did to what they typed.
    ("a ceiling YAML read as true", "---\nmax_workers: yes\n---\n", "not True"),
    ("a ceiling YAML read as false", "---\nmax_workers: off\n---\n", "not False"),
    ("a ceiling in quotes", '---\nmax_workers: "8"\n---\n', "not '8'"),
    ("a ceiling with a decimal point", "---\nmax_workers: 8.0\n---\n", "not 8.0"),
]


@pytest.mark.parametrize(
    ("text", "says"),
    [(text, says) for _, text, says in REJECTED],
    ids=[case for case, _, _ in REJECTED],
)
def test_a_file_that_cannot_be_trusted_is_refused(
    text: str, says: str, tmp_path: Path
) -> None:
    # loudly, and on the first read: a setting quietly ignored is the failure
    # this file's whole rule exists to prevent
    with pytest.raises(DirectivesError, match=says):
        read_directives(written(tmp_path, text))


def test_a_file_in_the_wrong_encoding_is_refused_by_name(tmp_path: Path) -> None:
    # an editor that saved as latin-1 is the ordinary way to get one, and
    # `UnicodeDecodeError` is a ValueError rather than an OSError -- so without
    # its own branch this is a traceback instead of a message naming the file
    path = tmp_path / "_steward.md"
    path.write_bytes("---\nmax_workers: 8\n---\n\ncafé\n".encode("latin-1"))

    with pytest.raises(DirectivesError, match="not valid UTF-8"):
        read_directives(path)


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_every_key_that_belongs_elsewhere_says_where(key: str, tmp_path: Path) -> None:
    with pytest.raises(DirectivesError) as caught:
        read_directives(written(tmp_path, f"---\n{key}: something\n---\n"))

    assert key in str(caught.value)
    assert REFUSED[key] in str(caught.value)


CEILING: list[tuple[str, int | None, int | None, int | None]] = [
    ("nobody expressed one", None, None, None),
    ("the workspace did", None, 8, 8),
    ("the command line did", 3, None, 3),
    ("the command line outranks the workspace", 3, 8, 3),
]


@pytest.mark.parametrize(
    ("cli", "file", "expected"),
    [(cli, file, expected) for _, cli, file, expected in CEILING],
    ids=[case for case, _, _, _ in CEILING],
)
def test_the_worker_count_resolves_most_specific_first(
    cli: int | None, file: int | None, expected: int | None
) -> None:
    pool = resolve_pool(Directives(max_workers=file), max_workers=cli)
    assert pool.max_workers == expected


@pytest.mark.parametrize(
    ("cli", "file", "expected"),
    [(cli, file, expected) for _, cli, file, expected in CEILING],
    ids=[case for case, _, _, _ in CEILING],
)
def test_task_concurrency_resolves_the_same_way(
    cli: int | None, file: int | None, expected: int | None
) -> None:
    # the same chain as `max_workers`, and unbounded where nobody said
    # otherwise -- `None` is the answer rather than the absence of one
    pool = resolve_pool(Directives(max_tasks=file), max_tasks=cli)
    assert pool.max_tasks == expected


def test_the_two_shape_knobs_are_independent() -> None:
    # they bound different things -- how much runs, and how few processes it
    # runs in -- so setting one must not imply anything about the other
    pool = resolve_pool(Directives(max_workers=2))

    assert (pool.max_workers, pool.max_tasks) == (2, None)


PATIENCE: list[tuple[str, str, int]] = [
    ("nobody expressed one", "---\nmax_workers: 8\n---\n", DEFAULT_STALL_AFTER),
    ("the workspace did", "---\nstall_after: 5\n---\n", 5),
    ("a project with no patience at all", "---\nstall_after: 1\n---\n", 1),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [(text, expected) for _, text, expected in PATIENCE],
    ids=[case for case, _, _ in PATIENCE],
)
def test_how_much_patience_a_project_wants_is_its_own(
    text: str, expected: int, tmp_path: Path
) -> None:
    # passes the same test max_workers does: respawning a task is Steward's
    # invention, so there is no eval_set() argument for this to contradict
    assert (
        resolve_pool(read_directives(written(tmp_path, text))).stall_after == expected
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("---\nstall_after: 0\n---\n", "greater than 0", id="never_try"),
        pytest.param("---\nstall_after: yes\n---\n", "not True", id="coerced"),
        pytest.param("---\nstall_after: '2'\n---\n", "not '2'", id="quoted"),
    ],
)
def test_a_meaningless_threshold_is_refused(
    text: str, message: str, tmp_path: Path
) -> None:
    with pytest.raises(DirectivesError, match=message):
        read_directives(written(tmp_path, text))


def test_the_file_is_never_a_source_of_sample_concurrency() -> None:
    # max_samples belongs to the definition, so the operator's value passes
    # straight through and `None` keeps meaning *no preference* -- which is what
    # lets `resolve_max_samples` fall to the definition rather than to a default
    assert resolve_pool(Directives()).max_samples is None
    assert resolve_pool(Directives(), max_samples=20).max_samples == 20


INTERVAL: list[tuple[str, str | None, str, int]] = [
    ("nobody expressed one", None, "---\nmax_workers: 8\n---\n", DEFAULT_TEND_INTERVAL),
    ("the workspace did", None, "---\ntend_interval: 30m\n---\n", 1800),
    ("the command line did", "5m", "---\ntend_interval: 30m\n---\n", 300),
    ("the command line, with nothing in the file", "1h", "---\n---\n", 3600),
]


@pytest.mark.parametrize(
    ("cli", "text", "expected"),
    [(cli, text, expected) for _, cli, text, expected in INTERVAL],
    ids=[case for case, _, _, _ in INTERVAL],
)
def test_the_tend_interval_resolves_most_specific_first(
    cli: str | None, text: str, expected: int, tmp_path: Path
) -> None:
    # the `max_workers` chain rather than the `max_samples` one: how often to
    # converge is a standing property of the host, so the file is a real source
    directives = read_directives(written(tmp_path, text))

    assert resolve_interval(directives, interval=cli) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("---\ntend_interval: 10\n---\n", "unit", id="a_bare_number"),
        pytest.param("---\ntend_interval: 10d\n---\n", "10d", id="an_unknown_unit"),
        pytest.param("---\ntend_interval: 0m\n---\n", "zero", id="never"),
        pytest.param("---\ntend_interval: yes\n---\n", "unit", id="coerced"),
    ],
)
def test_an_interval_that_is_not_one_is_refused(
    text: str, message: str, tmp_path: Path
) -> None:
    # the one key where strict typing is not enough on its own: `10` is a
    # perfectly good integer and means two different things to two readers
    with pytest.raises(DirectivesError, match=message):
        read_directives(written(tmp_path, text))


def test_an_interval_is_stored_as_seconds(tmp_path: Path) -> None:
    directives = read_directives(written(tmp_path, "---\ntend_interval: 2h\n---\n"))

    assert directives.tend_interval == 7200
