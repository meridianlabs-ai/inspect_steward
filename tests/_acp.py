"""Publishing an ACP server the way a worker publishes one.

The discovery directory lives under `fake_home`, which is session-scoped — so a
file written here outlives the test that wrote it and would hand the next test
an address for a worker it never started. Hence the fixture rather than a plain
helper: what it really provides is the cleanup.

Not named `test_*`, so pytest does not collect it.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from inspect_ai._util.discovery import prepare_discovery_dir, write_discovery_file
from inspect_ai.agent._acp.discovery import discovery_dir

Publish = Callable[[int, Path], None]


@pytest.fixture
def publish() -> Iterator[Publish]:
    """Write a discovery file for an ACP server bound by `pid` at `socket`."""
    written: list[Path] = []

    def write(pid: int, socket: Path) -> None:
        directory = discovery_dir()
        prepare_discovery_dir(directory)
        written.append(
            write_discovery_file(
                directory,
                pid,
                {
                    "pid": pid,
                    "eval_id": f"E{pid}",
                    "socket_path": str(socket),
                    "host": None,
                    "port": None,
                    "started_at": 1.0,
                },
            )
        )

    yield write
    for path in written:
        path.unlink(missing_ok=True)
