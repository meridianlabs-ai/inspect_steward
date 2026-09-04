"""Who is deciding, when nobody typed a name.

`rule` and `signoff` record an operator, and the operator is almost always the one whose shell this is. Git already knows them: every workspace is a repository (`create.py`) and `user.name` is the identity its commits carry, so the journal naming the same operator as the commit beside it is the right default. The login name is the fallback for a machine with no git identity, and an empty answer is left to the caller to refuse — `--by` exists for exactly the case where the resolver is wrong.
"""

import getpass
import shutil
import subprocess
from pathlib import Path


def operator_name(root: Path) -> str:
    """The name to record for a decision nobody signed by name.

    Args:
        root: The workspace root. Git's `user.name` is read from there, so a per-repository identity wins over a global one.

    Returns:
        Git's `user.name` as seen from the workspace, else the login name, else empty.
    """
    git = shutil.which("git")
    if git is not None:
        try:
            result = subprocess.run(
                [git, "-C", str(root), "config", "user.name"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    try:
        return getpass.getuser()
    except OSError:
        return ""
