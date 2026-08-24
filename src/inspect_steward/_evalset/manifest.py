import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

# the capture models are a versioned wire format, deliberately not public API
from inspect_ai._eval.eval_set_manifest import EvalSetCaptureTask
from pydantic import BaseModel

from .detect import DefinitionType

MANIFEST_VERSION = 1


class ManifestError(Exception):
    """A committed manifest could not be read.

    Distinct from its absence, which is `FileNotFoundError` and means *this workspace has not launched yet* rather than *something is wrong*.
    """


class ManifestSource(BaseModel):
    """The source a manifest was read from: an eval set definition and the arguments it was invoked with."""

    type: DefinitionType
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

    identifier_version: int
    """Version of the `task_identifier` computation that produced `tasks[].identifier`. A manifest outlives the inspect_ai it was read with, and an identifier computed under a different version cannot be matched against a log — so this records which computation the identifiers came from rather than leaving a silent mismatch to read as "nothing has run yet"."""

    eval_set_id: str | None = None
    """Eval set id as passed to `eval_set()` (if any)."""

    source: ManifestSource
    """The definition this manifest was read from."""

    options: dict[str, Any]
    """Informational `eval_set()` options (e.g. `log_dir`, `retry_attempts`, `limit`)."""

    tasks: list[ManifestTask]
    """Resolved tasks in the eval set."""


def definition_hash(path: Path) -> str:
    """Hash a definition file's contents.

    One function rather than two expressions, because the two callers have to agree exactly: capture stores this in `ManifestSource.content_hash`, and every tend recomputes it to notice that the file changed underneath a committed manifest (workflow.md, *One trigger, and one gate on it*). Two literals would be a drift check that silently always fires, or silently never does.

    Covers the top-level file and nothing else. A changed import or an `include:` fragment is invisible here, and deliberately so: the transitive closure of a Python import graph cannot be resolved statically, and the launch-time delta is the safety net that catches what this misses (configuration.md, *Reproducibility is the author's concern*).

    Args:
        path: The definition file.

    Returns:
        `sha256:<hex>`.

    Raises:
        OSError: If the file cannot be read.
    """
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def write_manifest(manifest: Manifest, path: Path) -> None:
    """Commit a manifest as desired state.

    Written through a temporary file in the same directory and renamed, so a concurrent reader sees either the previous manifest or this one. A tend reads this on every turn while a launch may be rewriting it, and half a manifest would read as a run that mostly does not exist.

    Args:
        manifest: The manifest to commit.
        path: Where to write it (`.steward/manifest.json`). Its directory is created if absent.

    Raises:
        OSError: If it cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(manifest.model_dump_json(indent=2))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_manifest(path: Path) -> Manifest:
    """Read the committed manifest.

    Args:
        path: `.steward/manifest.json`.

    Returns:
        Desired state, as the last launch committed it.

    Raises:
        FileNotFoundError: If nothing has been committed yet. An answer rather than damage — it is what a workspace that has never launched looks like.
        ManifestError: If what is there is not a manifest, or is one this version cannot read.
        OSError: If it exists and cannot be read.
    """
    try:
        manifest = Manifest.model_validate_json(path.read_bytes())
    except ValueError as ex:
        # ValidationError is a ValueError, and so is a JSON decode failure --
        # both mean the same thing to a caller, which is that desired state is
        # unreadable and no amount of retrying changes that
        raise ManifestError(
            f"{path} is not a valid manifest (re-capture it with "
            f"`steward launch`):\n{ex}"
        ) from ex

    if manifest.version != MANIFEST_VERSION:
        # the same argument `ManifestVersionError` makes about identifiers, one
        # layer earlier: this is desired state, so a schema the reader is
        # guessing at drives spawning and archiving. A field this version has
        # never heard of would be dropped in silence and a field it expects
        # would be defaulted, and either one is a decision made on a manifest
        # nobody wrote. The journal reads unknown types precisely because it is
        # history rather than input (workflow.md, *An event type a reader does
        # not recognise is data*); this is input
        raise ManifestError(
            f"{path} is a version {manifest.version} manifest and this Steward "
            f"reads version {MANIFEST_VERSION}. Re-capture it with "
            f"`steward launch`."
        )
    return manifest
