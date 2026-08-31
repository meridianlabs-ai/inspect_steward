import contextlib
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
from inspect_steward._worker import scan_processes


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


@pytest.fixture(autouse=True)
def no_ambient_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own notification channel out of the suite.

    `pytest-dotenv` loads the repository's `.env` into the session, so a developer who has configured a channel has one set in every test. Two things go wrong, and the second is the serious one.

    The tests that assert Steward *stays silent* fail, because a variable outranks the file by design — so `notification: false` in a fixture's `_steward.yaml` is correctly overridden by a value the test never wrote and cannot see.

    And a test that posts posts **to the real channel**. `establish_channel` falls back to the environment precisely so that declaring the channel inspect's way works, which means a CLI test that resolves one would reach whatever Slack workspace the developer configured. Every test that wants a channel sets its own.

    The scan-model spellings are cleared on the same grounds: `establish_scan_model` is reflexive with `SCOUT_SCAN_MODEL` by design, so an ambient value would configure a real (billed) model into any test that scans.
    """
    for name in CHANNELS + SCAN_MODELS:
        monkeypatch.delenv(name, raising=False)


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
