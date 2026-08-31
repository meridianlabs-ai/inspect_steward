"""Reading `_steward.yaml`.

Three claims, and they are the whole point of the file: the settings are executed while `policies` is only carried, a setting that belongs elsewhere is refused by name rather than ignored, and the command line outranks the workspace.
"""

from pathlib import Path
from typing import Any

import pytest
from inspect_steward._schedule import DEFAULT_STALL_AFTER
from inspect_steward._workspace import (
    DEFAULT_TEND_INTERVAL,
    REFUSED,
    RESERVED,
    Directives,
    DirectivesError,
    create_workspace,
    parse_setting,
    read_directives,
    resolve_interval,
    resolve_log_root,
    resolve_log_store,
    resolve_pool,
)


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "_steward.yaml"
    path.write_text(text, encoding="utf-8")
    return path


PARSED: list[tuple[str, str, int | None]] = [
    ("an empty file", "", None),
    ("nothing but comments", "# max_workers: 8\n", None),
    ("a setting", "max_workers: 8\n", 8),
    ("a setting beside policies", "max_workers: 3\npolicies: never past 8.\n", 3),
    ("policies alone", "policies:\n  - never spend over $200.\n", None),
]


@pytest.mark.parametrize(
    ("text", "max_workers"),
    [(text, expected) for _, text, expected in PARSED],
    ids=[case for case, _, _ in PARSED],
)
def test_a_settings_document_is_read(
    text: str, max_workers: int | None, tmp_path: Path
) -> None:
    assert read_directives(written(tmp_path, text)).max_workers == max_workers


def test_a_workspace_with_no_file_expressed_no_preferences(tmp_path: Path) -> None:
    # absent is a workspace that said nothing, not a workspace that is broken
    assert read_directives(tmp_path / "_steward.yaml") == Directives()


def test_the_file_init_writes_parses(tmp_path: Path) -> None:
    # the template ships every setting commented out, so it must survive being
    # read as one -- a syntax error there would break every workspace at once
    workspace = create_workspace(tmp_path, git=False).workspace
    assert read_directives(workspace.directives) == Directives()


def test_a_workspace_still_holding_the_old_file_is_refused(tmp_path: Path) -> None:
    # the quiet failure this exists to prevent: an unconverted workspace parses
    # perfectly as one with no directives at all, and every standing rule in it
    # stops applying with nothing said
    (tmp_path / "_steward.md").write_text(
        "---\nmax_workers: 8\n---\n", encoding="utf-8"
    )

    with pytest.raises(DirectivesError, match="_steward.md"):
        read_directives(tmp_path / "_steward.yaml")


def test_a_converted_workspace_does_not_pay_for_the_check(tmp_path: Path) -> None:
    # a leftover _steward.md beside a real _steward.yaml is somebody's backup,
    # not an unconverted workspace -- the file that exists is the one that rules
    (tmp_path / "_steward.md").write_text(
        "---\nmax_workers: 8\n---\n", encoding="utf-8"
    )

    assert read_directives(written(tmp_path, "max_workers: 2\n")).max_workers == 2


REJECTED: list[tuple[str, str, str]] = [
    ("a document that is not yaml", "max_workers: [8\n", "not valid YAML"),
    ("a document that is not a mapping", "- max_workers\n", "a mapping"),
    ("prose where settings belong", "never spend over $200.\n", "a mapping"),
    ("a key the definition owns", "log_dir: out/\n", "eval_set()"),
    ("sample concurrency", "max_samples: 40\n", "eval_set()"),
    # `notification` itself is Steward's own key now; `notify` is the near miss
    ("the channel under the wrong name", "notify: slack://t@c\n", "`notification`"),
    ("a channel that says nothing", "notification: true\n", "says nothing about"),
    ("a channel spelled as a word", "notification: none\n", "is `false` now"),
    ("an empty channel", 'notification: ""\n', "not an empty value"),
    ("a typo", "max_wokrers: 8\n", "not a setting Steward knows"),
    ("a meaningless ceiling", "max_workers: 0\n", "greater than 0"),
    ("a ceiling that is not a number", "max_workers: lots\n", "max_workers"),
    # YAML rewrites all four of these before pydantic ever sees them, and
    # pydantic's default would rewrite them again into a plausible integer --
    # `yes` all the way to 1, which would throttle a fleet to one worker and
    # say nothing. The error has to name the value that arrived, because that
    # is the only way the author learns what YAML did to what they typed.
    ("a ceiling YAML read as true", "max_workers: yes\n", "not True"),
    ("a ceiling YAML read as false", "max_workers: off\n", "not False"),
    ("a ceiling in quotes", 'max_workers: "8"\n', "not '8'"),
    ("a ceiling with a decimal point", "max_workers: 8.0\n", "not 8.0"),
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
    path = tmp_path / "_steward.yaml"
    path.write_bytes("policies: café\n".encode("latin-1"))

    with pytest.raises(DirectivesError, match="not valid UTF-8"):
        read_directives(path)


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_every_key_that_belongs_elsewhere_says_where(key: str, tmp_path: Path) -> None:
    with pytest.raises(DirectivesError) as caught:
        read_directives(written(tmp_path, f"{key}: something\n"))

    assert key in str(caught.value)
    assert REFUSED[key] in str(caught.value)


# --- policies --------------------------------------------------------------

POLICIES: list[tuple[str, str, str | list[str] | None]] = [
    (
        "a block of prose",
        "policies: |\n  first rule.\n\n  second rule.\n",
        "first rule.\n\nsecond rule.\n",
    ),
    ("one line", "policies: never past eight workers.\n", "never past eight workers."),
    ("a list of rules", "policies:\n  - first.\n  - second.\n", ["first.", "second."]),
    ("written and left empty", "policies:\n", None),
    ("an empty list", "policies: []\n", None),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [(text, expected) for _, text, expected in POLICIES],
    ids=[case for case, _, _ in POLICIES],
)
def test_policies_are_carried_however_they_are_written(
    text: str, expected: str | list[str] | None, tmp_path: Path
) -> None:
    # prose and a list are both first-class, because a project with three rules
    # wants three entries and one with a page of reasoning wants a block scalar
    assert read_directives(written(tmp_path, text)).policies == expected


def test_a_list_entry_that_is_not_text_is_refused_by_position(tmp_path: Path) -> None:
    # which entry, not just that the field is wrong -- a twelve-rule list needs
    # to say where to look
    with pytest.raises(DirectivesError, match="entry 2"):
        read_directives(written(tmp_path, "policies:\n  - fine.\n  - 3\n"))


def test_nothing_interprets_the_policy_text(tmp_path: Path) -> None:
    # Steward carries the words and never reads them, so text that looks like a
    # setting is still just text
    text = "policies: |\n  max_workers: 4000\n  log_dir: /tmp\n"

    directives = read_directives(written(tmp_path, text))

    assert directives.max_workers is None
    assert directives.policies == "max_workers: 4000\nlog_dir: /tmp\n"


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


def test_the_two_shape_knobs_are_independent() -> None:
    # they bound different things -- how much runs, and how few processes it
    # runs in -- so setting one must not imply anything about the other. Fleet
    # width reaches the pool only from the command line now: the file is not a
    # source for it, and the definition's value is read in `resolve_max_tasks`
    pool = resolve_pool(Directives(max_workers=2))

    assert (pool.max_workers, pool.max_tasks) == (2, None)
    assert resolve_pool(Directives(max_workers=2), max_tasks=3).max_tasks == 3


PATIENCE: list[tuple[str, str, int]] = [
    ("nobody expressed one", "max_workers: 8\n", DEFAULT_STALL_AFTER),
    ("the workspace did", "stall_after: 5\n", 5),
    ("a project with no patience at all", "stall_after: 1\n", 1),
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
        pytest.param("stall_after: 0\n", "greater than 0", id="never_try"),
        pytest.param("stall_after: yes\n", "not True", id="coerced"),
        pytest.param("stall_after: '2'\n", "not '2'", id="quoted"),
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
    ("nobody expressed one", None, "max_workers: 8\n", DEFAULT_TEND_INTERVAL),
    ("the workspace did", None, "tend_interval: 30m\n", 1800),
    ("the command line did", "5m", "tend_interval: 30m\n", 300),
    ("the command line, with nothing in the file", "1h", "", 3600),
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
    # converge is a standing property of the host, so the file is a real source.
    # The flag arrives parsed, by the same parser the file's value went through
    directives = read_directives(written(tmp_path, text))
    given = parse_setting("tend_interval", cli) if cli is not None else None

    assert resolve_interval(directives, tend_interval=given) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("tend_interval: 10\n", "unit", id="a_bare_number"),
        pytest.param("tend_interval: 10d\n", "10d", id="an_unknown_unit"),
        pytest.param("tend_interval: 0m\n", "zero", id="never"),
        pytest.param("tend_interval: yes\n", "unit", id="coerced"),
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
    directives = read_directives(written(tmp_path, "tend_interval: 2h\n"))

    assert directives.tend_interval == 7200


# --- the ramp envelope -----------------------------------------------------

RAMP: list[tuple[str, str, tuple[int, int] | bool | None]] = [
    ("a range", "samples_ramp: [60, 300]\n", (60, 300)),
    ("a one-step range", "samples_ramp: [50, 50]\n", (50, 50)),
    ("switched off", "samples_ramp: false\n", False),
    # YAML 1.1 reads `off` as a boolean, and refusing what it delivers would
    # refuse a perfectly natural spelling of the same instruction
    ("off, as YAML reads it", "samples_ramp: off\n", False),
    ("unset", "max_workers: 2\n", None),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [(text, expected) for _, text, expected in RAMP],
    ids=[case for case, _, _ in RAMP],
)
def test_the_ramp_envelope_parses(
    text: str, expected: tuple[int, int] | bool | None, tmp_path: Path
) -> None:
    assert read_directives(written(tmp_path, text)).samples_ramp == expected


NOT_A_RANGE: list[tuple[str, str, str]] = [
    ("true says nothing about how far", "samples_ramp: true\n", "how far"),
    ("one number is not a range", "samples_ramp: [40]\n", "two ordered"),
    ("an inverted range", "samples_ramp: [200, 40]\n", "ordered"),
    ("a zero floor", "samples_ramp: [0, 40]\n", "positive"),
    ("words", "samples_ramp: fast\n", "range"),
]


@pytest.mark.parametrize(
    ("text", "message"),
    [(text, message) for _, text, message in NOT_A_RANGE],
    ids=[case for case, _, _ in NOT_A_RANGE],
)
def test_a_meaningless_ramp_is_refused(text: str, message: str, tmp_path: Path) -> None:
    with pytest.raises(DirectivesError, match=message):
        read_directives(written(tmp_path, text))


def test_the_ramp_reaches_the_pool_and_defaults_to_none(tmp_path: Path) -> None:
    # `None` rather than the default range, so `resolve_samples_ramp` keeps the
    # *no preference* / *this range* distinction the max_samples chain also draws
    envelope = read_directives(written(tmp_path, "samples_ramp: [60, 300]\n"))

    assert resolve_pool(envelope).samples_ramp == (60, 300)
    assert resolve_pool(Directives()).samples_ramp is None
    assert resolve_pool(Directives(samples_ramp=False)).samples_ramp is False


# --- the log root ----------------------------------------------------------

ROOT: list[tuple[str, str | bool | None, str, str | None]] = [
    ("nobody named one", None, "", None),
    ("the workspace did", None, "log_root: /data/runs\n", "/data/runs"),
    ("the command line did", "/mine", "log_root: /data/runs\n", "/mine"),
    ("the workspace declined", None, "log_root: false\n", None),
    ("the launch declined", False, "log_root: /data/runs\n", None),
]


@pytest.mark.parametrize(
    ("cli", "text", "expected"),
    [(cli, text, expected) for _, cli, text, expected in ROOT],
    ids=[case for case, _, _, _ in ROOT],
)
def test_the_log_root_resolves_most_specific_first(
    cli: str | bool | None, text: str, expected: str | None, tmp_path: Path
) -> None:
    directives = read_directives(written(tmp_path, text))

    assert resolve_log_root(directives, log_root=cli) == expected


def test_the_machine_beats_the_project_on_where_logs_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the shipped ordering, applied to a key where it has teeth: a project that
    # declined a root still lands under the one the machine exported, and
    # --no-log-root is what overrules both
    monkeypatch.setenv("STEWARD_LOG_ROOT", "/data/runs")
    directives = read_directives(written(tmp_path, "log_root: false\n"))

    assert resolve_log_root(directives) == "/data/runs"
    assert resolve_log_root(directives, log_root=False) is None


# --- the log store ---------------------------------------------------------

STORE: list[tuple[str, str | bool | None, str, str | None]] = [
    ("nobody named one", None, "", None),
    (
        "the workspace did",
        None,
        "log_store: s3://project/store\n",
        "s3://project/store",
    ),
    ("the default location", None, "log_store: auto\n", "auto"),
    ("the command line did", "s3://mine", "log_store: s3://project\n", "s3://mine"),
    ("the workspace declined", None, "log_store: false\n", None),
    ("the launch declined", False, "log_store: s3://project\n", None),
]


@pytest.mark.parametrize(
    ("cli", "text", "expected"),
    [(cli, text, expected) for _, cli, text, expected in STORE],
    ids=[case for case, _, _, _ in STORE],
)
def test_the_log_store_resolves_most_specific_first(
    cli: str | bool | None, text: str, expected: str | None, tmp_path: Path
) -> None:
    # declining and never having one resolve alike, deliberately: both run
    # against no store, and recording the difference would record something
    # nothing reads
    directives = read_directives(written(tmp_path, text))

    assert resolve_log_store(directives, log_store=cli) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("log_store: true\n", "nothing about where", id="true"),
        pytest.param("log_store: ''\n", "empty value", id="empty"),
        pytest.param("log_store: none\n", "`false` now", id="the_retired_spelling"),
        pytest.param("log_store: 3\n", "should be", id="a_number"),
        pytest.param("sync: true\n", "nothing about where", id="sync_true"),
        pytest.param("sync: ''\n", "empty value", id="sync_empty"),
        pytest.param("sync: none\n", "`false` now", id="sync_retired_spelling"),
        pytest.param("log_root: true\n", "nothing about where", id="root_true"),
        pytest.param("log_root: ''\n", "empty value", id="root_empty"),
        pytest.param("log_root: none\n", "`false` now", id="root_retired_spelling"),
        # the one refusal `log_root` has and `log_store` does not: there is no
        # default root to name, and unset already spells the workspace's logs/
        pytest.param("log_root: auto\n", "no default location", id="root_auto"),
    ],
)
def test_a_location_that_is_not_one_is_refused(
    text: str, message: str, tmp_path: Path
) -> None:
    with pytest.raises(DirectivesError, match=message):
        read_directives(written(tmp_path, text))


# --- the environment -------------------------------------------------------

ENVIRONMENT: list[tuple[str, str, str, str, Any]] = [
    ("a ceiling", "STEWARD_MAX_WORKERS", "4", "max_workers", 4),
    ("patience", "STEWARD_STALL_AFTER", "7", "stall_after", 7),
    ("a ramp range", "STEWARD_SAMPLES_RAMP", "[60, 300]", "samples_ramp", (60, 300)),
    ("a ramp switched off", "STEWARD_SAMPLES_RAMP", "false", "samples_ramp", False),
    ("an interval", "STEWARD_TEND_INTERVAL", "30m", "tend_interval", 1800),
    ("a store", "STEWARD_LOG_STORE", "s3://team/store", "log_store", "s3://team/store"),
    ("no store", "STEWARD_LOG_STORE", "false", "log_store", False),
    ("a root", "STEWARD_LOG_ROOT", "s3://team/runs", "log_root", "s3://team/runs"),
    ("no root", "STEWARD_LOG_ROOT", "false", "log_root", False),
    ("a destination", "STEWARD_SYNC", "s3://team/run", "sync", "s3://team/run"),
    ("propagating nowhere", "STEWARD_SYNC", "false", "sync", False),
    (
        "a rule",
        "STEWARD_POLICIES",
        "never past eight.",
        "policies",
        "never past eight.",
    ),
    (
        "a list of rules",
        "STEWARD_POLICIES",
        "[first., second.]",
        "policies",
        ["first.", "second."],
    ),
]


@pytest.mark.parametrize(
    ("name", "value", "key", "expected"),
    [(name, value, key, expected) for _, name, value, key, expected in ENVIRONMENT],
    ids=[case for case, _, _, _, _ in ENVIRONMENT],
)
def test_every_setting_can_arrive_from_the_environment(
    name: str,
    value: str,
    key: str,
    expected: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the environment is the file, one key at a time -- same yaml.safe_load, so
    # a range, a boolean, and a duration all mean there what they mean there
    monkeypatch.setenv(name, value)

    assert getattr(read_directives(tmp_path / "_steward.yaml"), key) == expected


def test_the_environment_reaches_a_workspace_with_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the arrangement a machine-level variable exists to serve, and the one an
    # early return for the absent file would have quietly broken
    monkeypatch.setenv("STEWARD_MAX_WORKERS", "6")

    assert read_directives(tmp_path / "_steward.yaml").max_workers == 6


def test_the_environment_outranks_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # narrower scope wins: the file is what this project wants, a variable is
    # what this machine or this shell wants
    monkeypatch.setenv("STEWARD_MAX_WORKERS", "2")

    assert read_directives(written(tmp_path, "max_workers: 8\n")).max_workers == 2


def test_an_exported_but_empty_variable_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # refusing a shell profile that exports an empty value would be refusing a
    # correct setup -- the same reading `_timer.env` gives a credential
    monkeypatch.setenv("STEWARD_MAX_WORKERS", "")

    assert read_directives(written(tmp_path, "max_workers: 8\n")).max_workers == 8


def test_a_coerced_value_is_refused_by_the_variable_that_carried_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the coercion hazard on a third surface, and the message has to name the
    # variable: an author staring at a file that does not contain the value
    # would otherwise be sent to the wrong place
    monkeypatch.setenv("STEWARD_MAX_WORKERS", "yes")

    with pytest.raises(DirectivesError) as caught:
        read_directives(tmp_path / "_steward.yaml")

    assert "STEWARD_MAX_WORKERS" in str(caught.value)
    assert "not True" in str(caught.value)


@pytest.mark.parametrize(
    ("name", "says"),
    [
        pytest.param("STEWARD_SAMPLE_RAMP", "not a setting", id="a_typo"),
        pytest.param("STEWARD_LOG_DIRR", "not a setting", id="a_near_miss"),
    ],
)
def test_an_unrecognised_variable_is_refused_rather_than_ignored(
    name: str, says: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a misspelled variable that sits inert is a setting somebody believes is
    # in force, which is the whole failure the strict posture exists to prevent
    monkeypatch.setenv(name, "40")

    with pytest.raises(DirectivesError, match=says):
        read_directives(tmp_path / "_steward.yaml")


@pytest.mark.parametrize("name", sorted(RESERVED))
def test_stewards_own_worker_markers_are_not_read_as_settings(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # they are in the environment of exactly the processes that would otherwise
    # refuse them, so the namespace rule has to know its own internals
    monkeypatch.setenv(name, "something")

    assert read_directives(tmp_path / "_steward.yaml") == Directives()


SETTING: list[tuple[str, str, str, Any]] = [
    ("a ceiling", "max_workers", "4", 4),
    ("a ramp range", "samples_ramp", "[40, 300]", (40, 300)),
    ("a ramp switched off", "samples_ramp", "false", False),
    ("an interval", "tend_interval", "10m", 600),
]


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [(key, value, expected) for _, key, value, expected in SETTING],
    ids=[case for case, _, _, _ in SETTING],
)
def test_a_setting_typed_on_the_command_line_is_read_as_the_file_would(
    key: str, value: str, expected: Any
) -> None:
    # one parser for all three spellings, which is what keeps them from being
    # three parsers that agree by coincidence
    assert parse_setting(key, value) == expected


@pytest.mark.parametrize(
    ("key", "value", "says"),
    [
        pytest.param("samples_ramp", "true", "how far", id="a_meaningless_ramp"),
        pytest.param("tend_interval", "10", "unit", id="a_bare_number"),
        pytest.param("max_workers", "yes", "not True", id="coerced"),
    ],
)
def test_a_meaningless_value_on_the_command_line_earns_the_files_refusal(
    key: str, value: str, says: str
) -> None:
    with pytest.raises(DirectivesError, match=says):
        parse_setting(key, value)
