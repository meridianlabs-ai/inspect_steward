"""Capture, without the subprocess.

`read_eval_set` executes the definition in a child interpreter, which is the
right thing and costs seconds. Everything it does is already covered in
`tests/evalset/test_read.py`, and `test_tend_live.py` runs it for real through
`launch` — so the shell-contract cases fake it at exactly the seam
`tests/timer/_fake.py` fakes `run_command` at: the one call that leaves the
process. Everything on this side of it is real.

**The content hash comes from the definition on disk**, not from the manifest
handed in. A real capture hashes the file it just executed, so drift is `False`
immediately after a launch and flips when somebody edits the file — a fake
returning a fixed hash would make every launch's own first turn report drift.

Not named `test_*`, so pytest does not collect it.
"""

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.detect import DefinitionType
from inspect_steward._evalset.manifest import Manifest, definition_hash


@dataclass(frozen=True)
class Captured:
    """One call to `read_eval_set`, as the launch made it."""

    definition: Path
    cwd: Path | None
    args: dict[str, Any] | None
    type: DefinitionType | None


@dataclass
class FakeCapture:
    """A capture that returns a manifest instead of running anything."""

    manifest: Manifest
    """What the next capture returns. Reassign it between launches to make the definition appear to have changed."""

    calls: list[Captured] = field(default_factory=list[Captured])
    """Every capture, in order — which is how a test asserts *no capture happened at all*, the claim that makes a pre-capture refusal a pre-capture refusal."""

    def __call__(
        self,
        definition: str | Path,
        args: dict[str, Any] | None = None,
        *,
        type: DefinitionType | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Manifest:
        path = Path(definition)
        self.calls.append(
            Captured(
                definition=path,
                cwd=Path(cwd) if cwd is not None else None,
                args=args,
                type=type,
            )
        )
        return self.manifest.model_copy(
            update={
                "source": self.manifest.source.model_copy(
                    update={"content_hash": definition_hash(path)}
                )
            }
        )


def fake_capture(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> FakeCapture:
    """Replace the capture with one that returns this manifest.

    Patched through `import_module` rather than by dotted string, for the reason
    `tests/timer/_fake.py` documents: the package re-exports the `launch`
    *function*, which shadows the module a dotted path would resolve.

    Args:
        monkeypatch: Pytest's patcher.
        manifest: What the capture returns.

    Returns:
        The fake, whose `manifest` can be reassigned and whose `calls` record
        what was asked of it.
    """
    fake = FakeCapture(manifest=manifest)
    monkeypatch.setattr(
        import_module("inspect_steward._launch.launch"), "read_eval_set", fake
    )
    return fake
