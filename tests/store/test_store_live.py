"""The whole loop, for real: one project signs and publishes, the next reuses.

**Budget: two launches** (plan.md §10) — the least a claim about *cross-run* reuse can be made in, since the point is what one workspace's signoff does for a different workspace's launch. Everything else about the store is layer 1 beside this file.

What only a live pair can show is that the two halves meet: the identifier a capture enumerates has to be the identifier a landed log's header computes, or the store answers nothing and nobody finds out until somebody deploys one. Layer 1 asserts each half against synthesized logs whose identifiers were synthesized the same way, which is exactly the agreement this checks and cannot itself establish.
"""

import shutil
from pathlib import Path

from inspect_steward._launch import Launch, launch
from inspect_steward._signoff import Signoff, signoff
from inspect_steward._store import DirectoryStore
from inspect_steward._tend import TendResult
from inspect_steward._workspace import Workspace, create_workspace

from ..schedule.test_tend import settle, turn

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"

SIMPLE = FIXTURES / "simple_evalset.py"
"""`addition` (2 samples) and `echo` (1 sample × 2 epochs) on `mockllm/model`."""


def project(root: Path, store: Path) -> tuple[Workspace, Path]:
    """A workspace pointed at `store`, with the definition copied into it.

    Copied rather than referenced, because the manifest records the definition path and every later turn resolves it against the workspace root — two workspaces sharing one fixture path would each record somewhere the other is not.
    """
    root.mkdir(parents=True, exist_ok=True)
    create_workspace(root, git=False)
    workspace = Workspace.at(root)
    workspace.directives.write_text(f"log_store: {store}\n", encoding="utf-8")
    definition = workspace.root / "evalset.py"
    shutil.copy(SIMPLE, definition)
    return workspace, definition


def test_one_projects_signoff_satisfies_the_next_projects_launch(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"

    # **the first project runs it.** No store to read from, so both tasks run
    # the ordinary way and land real logs
    first, definition = project(tmp_path / "first", store)
    ran = launch(first, definition, timer=False)
    assert isinstance(ran, Launch)
    assert ran.committed
    assert ran.reused == []
    settle(first)
    turn(first)

    # signed and published, which is the only thing that ever writes a row
    signed = signoff(first, by="kaia", publish=True)
    assert isinstance(signed, Signoff)
    assert signed.signature is not None, signed.blockers
    assert signed.published is not None
    assert signed.published.count == 2

    # **the identifiers agree across the boundary**, which is the half layer 1
    # cannot establish: what a capture enumerated has to be what a landed log's
    # own header computes, or the store answers nothing
    wanted = {task.identifier for task in ran.manifest.tasks}
    assert set(DirectoryStore(str(store)).search(wanted)) == wanted

    # **the second project does not run them.** A different workspace, a fresh
    # log directory, the same definition -- and `_spawn_order` queues only
    # `MISSING` and `INCOMPLETE`, so the copied logs settle both tasks
    second, elsewhere = project(tmp_path / "second", store)
    reused = launch(second, elsewhere, timer=False)
    assert isinstance(reused, Launch)

    assert {one.identifier for one in reused.reused} == wanted
    assert isinstance(reused.turn, TendResult)
    assert reused.turn.queued == []
    assert reused.turn.summary.states.get("complete") == 2
    # and the source is recorded, because an identifier match says the
    # configuration was identical and nothing about the environment
    assert all(str(store) in one.source for one in reused.reused)
