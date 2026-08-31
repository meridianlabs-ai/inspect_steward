"""The diff between the shell that arms a timer and the environment it installs.

The check exists because the failure it prevents is the worst one available in
this step: every interval all night, a worker starts, authenticates against
nothing, and writes a log that says so, while `status.md` reports a fleet
dutifully failing. What makes it safe to ship is that it is a **diff** and not a
guess — Steward never decides which credentials an eval needs, only which ones
this shell has that a scheduled tend will not.
"""

from pathlib import Path

import pytest
from inspect_steward._timer import unavailable_credentials
from inspect_steward._timer.env import credentials, dotenv_names, explain

SHELL = {
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "AWS_ACCESS_KEY_ID": "AKIAsecret",
    "PATH": "/usr/bin",
    "HOME": "/home/jj",
}


def env(tmp_path: Path, content: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


# --- what counts as a credential ----------------------------------------


NAMES: list[tuple[str, str, bool]] = [
    ("a provider key", "ANTHROPIC_API_KEY", True),
    ("an unknown provider's key", "SOMEVENDOR_API_KEY", True),
    ("a bearer token", "HF_TOKEN", True),
    ("a secret", "AZURE_CLIENT_SECRET", True),
    # ends in neither a key nor a token, and an S3 log_dir is the case that
    # made this check worth building at all
    ("an aws id", "AWS_ACCESS_KEY_ID", True),
    ("a service account path", "GOOGLE_APPLICATION_CREDENTIALS", True),
    ("a setting that merely mentions keys", "API_KEY_ROTATION_DAYS", False),
    ("the path", "PATH", False),
    ("a log directory", "INSPECT_LOG_DIR", False),
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [(name, expected) for _, name, expected in NAMES],
    ids=[case for case, _, _ in NAMES],
)
def test_which_variables_are_credentials(name: str, expected: bool) -> None:
    assert (name in credentials({name: "value"})) is expected


def test_an_exported_but_empty_variable_carries_nothing() -> None:
    # its absence under cron loses nothing, so refusing over it would be a
    # refusal nobody can act on
    assert credentials({"OPENAI_API_KEY": ""}) == set()


# --- what a .env is read for --------------------------------------------


def test_only_names_are_read_out_of_a_dotenv(tmp_path: Path) -> None:
    # Steward has no use for the secret and every reason not to hold one; the
    # whole question is which keys exist
    path = env(tmp_path, "ANTHROPIC_API_KEY=sk-ant-supersecret\n")

    assert dotenv_names(path) == {"ANTHROPIC_API_KEY"}


DOTENVS: list[tuple[str, str, set[str]]] = [
    ("plain", "A=1\nB=2\n", {"A", "B"}),
    ("exported", "export A=1\n", {"A"}),
    ("commented", "# A=1\nB=2\n", {"B"}),
    ("blank lines", "\n\nA=1\n\n", {"A"}),
    ("spaced", "  A = 1  \n", {"A"}),
    ("quoted value with an equals in it", 'A="x=y"\n', {"A"}),
    ("a line that is not an assignment", "nonsense\nA=1\n", {"A"}),
    # six ways of writing something that looks like an assignment and arrives as
    # nothing. python-dotenv resolves all of them, which is why it does the
    # reading rather than a split on `=`
    ("named with nothing after the equals", "A=\nB=2\n", {"B"}),
    ("named with only whitespace", "A=   \nB=2\n", {"B"}),
    ("empty double-quoted", 'A=""\nB=2\n', {"B"}),
    ("empty single-quoted", "A=''\nB=2\n", {"B"}),
    ("interpolating something unset", "A=${NOTHING_DEFINES_THIS}\nB=2\n", {"B"}),
    ("a bare name with no equals at all", "A\nB=2\n", {"B"}),
]


@pytest.mark.parametrize(
    ("content", "expected"),
    [(content, expected) for _, content, expected in DOTENVS],
    ids=[case for case, _, _ in DOTENVS],
)
def test_a_dotenv_yields_the_names_it_defines(
    content: str, expected: set[str], tmp_path: Path
) -> None:
    assert dotenv_names(env(tmp_path, content)) == expected


def test_no_dotenv_defines_nothing(tmp_path: Path) -> None:
    assert dotenv_names(tmp_path / ".env") == set()


# --- the diff itself ----------------------------------------------------


def test_a_key_this_shell_has_and_the_file_does_not_is_named(tmp_path: Path) -> None:
    missing = unavailable_credentials(env(tmp_path, "ANTHROPIC_API_KEY=x\n"), SHELL)

    assert missing == ["AWS_ACCESS_KEY_ID"]


def test_every_missing_key_is_named_rather_than_the_first(tmp_path: Path) -> None:
    # the one left out is the one that breaks the night
    missing = unavailable_credentials(tmp_path / ".env", SHELL)

    assert missing == ["ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID"]


def test_a_dotenv_naming_a_key_without_a_value_does_not_satisfy_the_check(
    tmp_path: Path,
) -> None:
    # the two halves of the diff have to agree about empty. `credentials` already
    # ignores an exported-but-empty variable, so counting `ANTHROPIC_API_KEY=` as
    # defined would let a `.env` pass the check while handing the scheduler an
    # unusable credential -- precisely the overnight failure the check exists to
    # prevent, now with a clean bill of health attached
    path = env(tmp_path, 'ANTHROPIC_API_KEY=""\nAWS_ACCESS_KEY_ID=y\n')

    assert unavailable_credentials(path, SHELL) == ["ANTHROPIC_API_KEY"]


def test_a_dotenv_holding_everything_passes(tmp_path: Path) -> None:
    path = env(tmp_path, "ANTHROPIC_API_KEY=x\nAWS_ACCESS_KEY_ID=y\n")

    assert unavailable_credentials(path, SHELL) == []


def test_a_shell_with_no_credentials_has_nothing_to_lose(tmp_path: Path) -> None:
    # the mockllm case, and every test in this suite: nothing to warn about
    assert unavailable_credentials(tmp_path / ".env", {"PATH": "/usr/bin"}) == []


def test_a_key_in_the_file_and_not_in_the_shell_is_not_a_problem(
    tmp_path: Path,
) -> None:
    # the timer will have it and this shell will not, which is the direction
    # that works -- the check is one-way on purpose
    path = env(tmp_path, "OPENAI_API_KEY=x\n")

    assert unavailable_credentials(path, {"PATH": "/usr/bin"}) == []


def test_a_setting_inspect_reads_for_itself_is_named_too(tmp_path: Path) -> None:
    """The variables the manifest cannot carry overnight.

    Everything Steward reads is resolved at launch and recorded, which is what
    keeps an exported `INSPECT_EVAL_LIMIT` in force at 02:00. These reach a
    worker through the environment instead, so under a scheduler they are
    simply gone — and `INSPECT_EVAL_MODEL` disappearing means a definition that
    named no model resolves a different one, computes a different identifier
    than the manifest recorded, and writes a log no tend looks for.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-real\n")

    missing = unavailable_credentials(
        env_file,
        {"ANTHROPIC_API_KEY": "sk-real", "INSPECT_EVAL_MODEL": "openai/gpt-4o"},
    )

    assert missing == ["INSPECT_EVAL_MODEL"]
    # and it is not called a credential, which would read as a bug in the check
    message = explain(missing, env_file)
    assert "credential" not in message
    assert "INSPECT_EVAL_MODEL" in message


CHANNELS = ["INSPECT_EVAL_NOTIFICATION", "STEWARD_NOTIFICATION"]


@pytest.mark.parametrize("name", CHANNELS)
def test_a_notification_url_is_treated_as_the_token_it_carries(name: str) -> None:
    # `slack://xoxb-.../...` is a bearer token with a scheme in front of it, and
    # the name matches none of the credential suffixes. Both spellings, because
    # they configure each other: an arming shell that exports either and a
    # `.env` that names neither is a 02:00 turn that cannot reach anybody --
    # which is the failure notification exists to prevent, arriving through the
    # one door notification cannot watch
    assert credentials({name: "slack://xoxb-secret/C123"}) == {name}


@pytest.mark.parametrize("name", CHANNELS)
def test_a_channel_the_scheduler_would_not_inherit_is_refused(
    name: str, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-real\n", encoding="utf-8")

    missing = unavailable_credentials(env_file, {name: "slack://xoxb-secret/C123"})

    assert missing == [name]
    assert name in explain(missing, env_file)


@pytest.mark.parametrize("name", CHANNELS)
def test_either_spelling_in_the_env_file_covers_the_other(
    name: str, tmp_path: Path
) -> None:
    # the two names are one capability: either configures Steward and its fleet
    # alike, so a shell holding one and a `.env` holding the other is a 02:00
    # turn that can reach somebody -- and refusing it refuses over a difference
    # that has stopped existing by the time it would matter
    other = next(spelling for spelling in CHANNELS if spelling != name)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{other}=slack://xoxb-secret/C123\n", encoding="utf-8")

    exported = {name: "slack://xoxb-different/C456"}

    assert unavailable_credentials(env_file, exported) == []
