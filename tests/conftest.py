import contextlib
import importlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest
from inspect_steward._cli import main as cli
from inspect_steward._worker import scan_processes
from inspect_steward._workspace import LOG_DIR, PREFIX, RESERVED


@pytest.fixture(autouse=True)
def no_worker_outlives_its_test(tmp_path: Path) -> Iterator[None]:
    """Kill any worker the test left running.

    Workers are detached — `start_new_session`, so that a run survives the tend that started it — and that guarantee does not know it is in a test. Nothing kills them when pytest exits, and a worker held at a fault marker waits for a file that a finished test run will never write, so one escaped worker spins until somebody notices it in `ps`.

    The sweep is the production one, scoped to this test's workspace exactly as a tend scopes it to its own: a leak is by definition something no `finally` caught, so catching it needs a mechanism no test has to remember. Guarded on the workers directory existing, so the ~60ms costs only the tests that spawned something.

    Silent, because several tests legitimately leave a worker for teardown. What it guarantees is that none of them leaves one behind.
    """
    yield

    workers_dir = tmp_path / ".steward" / "workers"
    if not workers_dir.exists():
        return
    for found in scan_processes(workers_dir):
        with contextlib.suppress(OSError, psutil.Error):
            os.kill(found.pid, signal.SIGKILL)


CHANNELS = ("STEWARD_NOTIFICATION", "INSPECT_EVAL_NOTIFICATION")
"""The two spellings of a notification channel, which no test may inherit."""

SCAN_MODELS = ("STEWARD_SCAN_MODEL", "SCOUT_SCAN_MODEL")
"""The two spellings of a scan-side model, kept out for the same reasons."""

OVERRIDES = "INSPECT_EVAL_"
"""Inspect's own override namespace, which shapes what an eval set *is*.

Every name under it reaches a run — the model, the limit, the epochs, the slice
— and a fixture that committed a manifest against one value while the developer's
machine exported another is a fixture testing something nobody wrote down.
"""


def ambient(name: str) -> bool:
    """Whether a variable configures Steward or a run, and so may not arrive from a developer's machine.

    **The namespaces rather than a list of names**, which is what this guard
    used to be and what let the leak through. `PREFIX` is the whole of Steward's
    vocabulary by construction — `directives.PREFIX` says so, and refuses
    anything under it that is not a setting — so asking whether a name is under
    it is the same question as *does this change what Steward does*, and it
    stays right as settings are added. A four-name tuple did not: `log_root` and
    `log_store` arrived after it was written, went unguarded, and pointed the
    whole suite's launches at a real S3 bucket.

    `RESERVED` is excluded because those two are not settings — they are markers
    Steward writes into a worker's environment and reads back off the process
    table, so a test that spawns one wants them to survive.
    """
    return (
        (name.startswith(PREFIX) and name not in RESERVED)
        or name.startswith(OVERRIDES)
        or name in SCAN_MODELS
        # the one deployment name outside both namespaces: `launch` refuses it
        # rather than ignoring it, so an ambient one is a refusal in every
        # launch test rather than a wrong value in one
        or name == LOG_DIR
    )


@pytest.fixture(autouse=True)
def no_ambient_settings() -> Iterator[None]:
    """Keep the developer's own machine out of the suite.

    `pytest-dotenv` loads the repository's `.env` into the session, so a developer who has configured a channel has one set in every test. Two things go wrong, and the second is the serious one.

    The tests that assert Steward *stays silent* fail, because a variable outranks the file by design — so `notification: false` in a fixture's `_steward.yaml` is correctly overridden by a value the test never wrote and cannot see.

    And a test that posts posts **to the real channel**. `establish_channel` falls back to the environment precisely so that declaring the channel inspect's way works, which means a CLI test that resolves one would reach whatever Slack workspace the developer configured. Every test that wants a channel sets its own.

    The scan-model spellings are cleared on the same grounds: `establish_scan_model` is reflexive with `SCOUT_SCAN_MODEL` by design, so an ambient value would configure a real (billed) model into any test that scans.

    **It guards the namespaces rather than a list of names, because the list was already wrong.** This was four names when the only settings worth fearing were a channel and a scan model. `log_root` and `log_store` arrived afterwards and nobody came back — so on a machine whose `.env` names an S3 bucket, which is the machine this feature exists for, every `launch` test resolved its log directory to that bucket and failed on credentials it was never given. That is the same defect as the channel one and worse in kind: the channel guard failed toward posting to a real Slack workspace, and this one failed toward *writing eval logs into a real bucket*. `ambient` asks whether a name is under Steward's prefix or inspect's override namespace, so a setting added next year is guarded on the day it is added.

    **Clearing them once is not enough, which is what the wrapper is for.** Every `steward` invocation calls `init_dotenv()` in its group callback — deliberately, because a scheduled tend runs under a stripped environment and needs to see what its workers see — and that reads `.env` again from the cwd upward and puts back exactly what this fixture removed. So any in-process CLI test running from inside the repository had a live channel restored underneath it, and the only thing standing between the suite and a real Slack workspace was which tests happened to resolve one.

    The load still happens, because everything else in `.env` — the credentials above all — is wanted. What the wrapper does is **restore the guarded names to whatever they were when it was called**: absent for most tests, and the test's own value for one that set a channel or a log root deliberately. Clearing them outright instead would take those away too, and *no test may have a channel* is a different rule from *no test may inherit one*.

    **It holds its own `MonkeyPatch` rather than the test's, and that is not tidiness.** They are the same object otherwise — the `monkeypatch` fixture is function-scoped and shared between a test and everything autouse around it — so a test calling `monkeypatch.undo()` to drop a patch of its own reverted this fixture too, put the developer's channel back from `.env`, and posted to a real Slack workspace from whatever it did next. Observed, from three tests that undid a stubbed store failure and then signed off again. A guard against an ambient channel cannot be revocable by the code it is guarding.
    """
    with pytest.MonkeyPatch.context() as guard:
        for name in [name for name in os.environ if ambient(name)]:
            guard.delenv(name, raising=False)

        loaded = cli.init_dotenv

        def guarded() -> None:
            held = {name: value for name, value in os.environ.items() if ambient(name)}
            loaded()
            # what the load put back that the caller did not have, rather than
            # a fixed list of names: the `.env` decides which settings it
            # carries, and a guard that only removed the ones it thought of is
            # the guard this replaced
            for name in [name for name in os.environ if ambient(name)]:
                if name not in held:
                    os.environ.pop(name, None)
            os.environ.update(held)

        guard.setattr(cli, "init_dotenv", guarded)
        yield


@pytest.fixture(autouse=True)
def no_ambient_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own Docker daemon out of every launch.

    `launch` asks the local daemon what it allocates bridge networks from, and the answer decides whether it prints advice. Two things go wrong if that reaches a real daemon, and they are the two this suite always guards against.

    It is **not deterministic**: whether the advice fires depends on the machine's processor count against its address pools, so the same launch test passes on a laptop and fails on a 64-core CI runner — which is precisely the machine the feature exists for and the one nobody debugs on.

    And it is **a subprocess per launch**, on a daemon that may be absent, wedged, or ten seconds slow. The probe is written not to raise on any of those, so the cost would be silent rather than visible.

    Neutralized at `advise` rather than at `subprocess.run`, because the tests that mean to exercise the probe (`tests/launch/test_pools.py`) patch the subprocess themselves and must keep reaching the real function.
    """
    # by name rather than `from inspect_steward._launch import launch`, which
    # resolves to the *function* the package re-exports under that name
    module = importlib.import_module("inspect_steward._launch.launch")

    def roomy(wanted: int, timeout: float = 0.0) -> None:
        return None

    monkeypatch.setattr(module, "advise", roomy)


AWS_CREDENTIAL_SOURCES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)
"""Every environment variable botocore takes credentials from; the files and the instance metadata service are closed separately."""


@pytest.fixture(autouse=True)
def no_ambient_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's AWS credentials out of the suite.

    A store at an `s3://` location is opened by asking the bucket a question, and what a bucket that does not exist answers depends on who is asking. With no credentials, botocore raises before the request leaves the process and `open_store` turns that into a `StoreError`. With credentials — an instance role on an EC2 box, keys in a developer's shell — S3 answers *not found*, `exists` returns `False`, and the store opens. `test_a_remote_location_that_will_not_open_is_a_store_error` was written on a laptop and encoded the first answer; on the box it observed the second.

    The suite must not read the machine's credentials, for the two reasons the channel and the daemon are kept out: the answer is not deterministic across machines, and a test that reached a real bucket would be writing into it. Every source botocore consults is closed — the environment keys and profile, the shared credentials and config files, the container endpoints, and the instance metadata service, which it would otherwise reach from inside an instance. `s3fs` caches one filesystem per set of constructor arguments and resolves credentials on first use, so with this around every test the cached instance never holds any.
    """
    for name in AWS_CREDENTIAL_SOURCES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(scope="session")
def fake_home() -> Iterator[Path]:
    """A home directory short enough to hold a unix socket.

    Deliberately not `tmp_path_factory`, whose paths are long: inspect binds its control socket at `<data dir>/inspect_ai/control/<pid>.sock`, and under a home like `/private/var/folders/../pytest-of-user/pytest-93/popen-gw6/home0` that exceeds the 104-byte `sun_path` limit. Inspect's response is a warning and an eval that runs without a control surface — so a test asking anything of the control channel fails for a reason nowhere near the socket.
    """
    home = Path(tempfile.mkdtemp(prefix="stw-", dir="/tmp"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="session")
def uv_cache_dir() -> str | None:
    """Where uv really keeps its cache, or `None` if there is no uv to ask.

    Resolved once, before any test moves `HOME` out from under it.
    """
    # pip puts uv beside the interpreter, a directory that is on PATH only when
    # the venv happens to be activated — the same gap `definition_command`
    # closes for the hawk child
    uv = shutil.which(
        "uv",
        path=os.pathsep.join(
            [str(Path(sys.executable).parent), os.environ.get("PATH", "")]
        ),
    )
    if uv is None:
        return None
    result = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True)
    return result.stdout.strip() or None if result.returncode == 0 else None


@pytest.fixture(autouse=True)
def isolated_user_data(
    request: pytest.FixtureRequest,
    fake_home: Path,
    uv_cache_dir: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep definitions Steward launches out of the user's real home directory.

    Flow records the log directory of every `flow run` as a global `last_log_dir` under `platformdirs.user_data_dir("inspect_flow")`, so any test that launches a flow definition — a read, a selection run, a worker — rewrites a file in the user's home and points their next `flow run --resume` at a pytest temp directory. There is no narrower knob than the home directory: that path derives from `XDG_DATA_HOME` where it is set and from `HOME` otherwise, so both are redirected.

    Autouse rather than opt-in, because the tests that pollute are the ones nobody thinks of as touching the home directory. Network tests are exempt: they reach real services and need the credentials the real home holds.
    """
    # `request.keywords` is the typed accessor for the node's markers, and like
    # `get_closest_marker` it sees the parents' as well as this node's own
    if "network" in request.keywords:
        return
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local" / "share"))
    if uv_cache_dir is not None:
        # a moved home moves uv's cache with it, and both frontends shell out to
        # uv on their way to eval_set(). Pinning the cache keeps this fixture to
        # the application data it is for: without it the flow tests take 3x.
        monkeypatch.setenv("UV_CACHE_DIR", uv_cache_dir)
