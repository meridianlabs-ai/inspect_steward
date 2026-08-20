from typing import Any, Literal

# the capture models are a versioned wire format, deliberately not public API
from inspect_ai._eval.eval_set_manifest import EvalSetCaptureTask
from pydantic import BaseModel

MANIFEST_VERSION = 1


class ManifestSource(BaseModel):
    """The source a manifest was read from: an eval set definition and the arguments it was invoked with."""

    type: Literal["evalset", "flow", "hawk"]
    """Definition type: an `evalset` definition is a Python file culminating in a call to `eval_set()`; a `flow` definition is an Inspect Flow spec (Python or YAML); a `hawk` definition is a Hawk eval set config (YAML)."""

    path: str
    """Definition file path (as provided by the caller)."""

    content_hash: str
    """Hash of the definition file contents (`sha256:<hex>`), for staleness detection. Covers only the top-level file (not includes or imports)."""

    args: dict[str, Any]
    """Arguments passed to the definition (flow spec function args; empty otherwise)."""


class ManifestTask(EvalSetCaptureTask):
    """A resolved task in an eval set manifest."""

    key: str
    """Human-facing display key (`task[solver]@model`, disambiguated when tasks collide). Unique within a manifest, but not stable across definition edits — use `identifier` for stable matching."""


class Manifest(BaseModel):
    """Static enumeration of an eval set read from a definition."""

    version: int
    """Manifest schema version."""

    eval_set_id: str | None = None
    """Eval set id as passed to `eval_set()` (if any)."""

    source: ManifestSource
    """The definition this manifest was read from."""

    options: dict[str, Any]
    """Informational `eval_set()` options (e.g. `log_dir`, `retry_attempts`, `limit`)."""

    tasks: list[ManifestTask]
    """Resolved tasks in the eval set."""
