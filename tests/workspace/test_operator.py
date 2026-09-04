"""Who a decision is recorded against when nobody typed a name.

The workspace's own git identity wins, and the login name is the floor. The machine running the tests has an identity of its own, so both cases point git's global and system configuration at files that do not exist.
"""

import getpass
import shutil
import subprocess
from pathlib import Path

import pytest
from inspect_steward._workspace import operator_name


def _quiet_machine_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-gitconfig"))


def test_the_workspace_repository_names_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    _quiet_machine_identity(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run([git, "init", "-q", str(root)], check=True)
    subprocess.run(
        [git, "-C", str(root), "config", "user.name", "Kaia Example"], check=True
    )

    assert operator_name(root) == "Kaia Example"


def test_without_a_git_identity_the_login_name_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_machine_identity(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()

    assert operator_name(root) == getpass.getuser()
