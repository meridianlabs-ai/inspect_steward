import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fake_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("home")


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
