import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

# the capture models are a versioned wire format, deliberately not public API
from inspect_ai._eval.eval_set_manifest import EvalSetCaptureTask
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
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

    capture_rss: int | None = None
    """Peak resident memory of the capture process tree, in bytes, or `None` where nothing measured it.

    A fact about *reading* this definition rather than about the eval set, which is why it lives here beside the hash and the path. It is carried because it also bounds running one: capture constructs every task in the set, where a worker constructs only its own, so this is the most a worker's startup can cost and the fleet's is it times the width (`_evalset/cost.py`).

    `MANIFEST_VERSION` deliberately did not move for this. The version gate refuses a manifest whose schema the reader would have to guess at; a field added with a default whose absence means *not measured* is not one, and bumping would have made every committed manifest unreadable to say so.
    """


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
    """Informational `eval_set()` options as the *definition* passed them (e.g. `log_dir`, `retry_attempts`, `limit`)."""

    overrides: EvalSetOverrides | None = None
    """Inspect's words as this run said them, or `None` where the run is the definition's own.

    **The durable copy, and the only one.** A run's overrides are resolved once, at launch, from flags and the environment — and neither survives to the 02:00 tend that spawns the next worker. They cannot live in `.steward/` either, which this design tells people they may delete. So they are captured *with* the manifest, by the same subprocess that honoured them, and every later tend reads them back out of the committed file: the enumeration and the fleet cannot disagree, because the fleet's copy is the one the enumeration was made under.

    `MANIFEST_VERSION` deliberately did not move for this, and the `capture_rss` reasoning only half covers it. That argument is about a *new* reader meeting an old manifest, where an absent field means *not measured* and nothing is lost. The other direction is not so comfortable: an **older** reader meeting this field drops it silently, accepts the manifest as version 1, and tends the run on the definition's own values — a different eval than the one that was captured, with nothing said. What makes that acceptable is only that no such reader exists in the wild; the package is unreleased. The moment one does, this field is the reason to bump.
    """

    tasks: list[ManifestTask]
    """Resolved tasks in the eval set."""


def manifest_digest(manifest: Manifest) -> str:
    """Hash the work a committed manifest asks for: which tasks, and how much of each.

    **What identifies a *result set*, where `ManifestSource.content_hash` identifies a *file*.** The two are different questions and only one of them is answerable by hashing the definition. A hash of the top-level file misses an argument passed alongside it (`ManifestSource.args` for a Flow spec), an imported module that changed, and an `include:` fragment — all of which produce a different eval set from a byte-identical file. It also *changes* on an edit that has not been launched, where the results on disk have not moved at all.

    **Identifiers alone are not enough either, and the reason is a deliberate property of the identifier.** `task_identifier` covers the solver plan, generate config, model args, roles, version and execution limits — and pointedly not the sample count or the epochs, so that raising either leaves existing logs resumable rather than orphaning them. Steward relies on exactly that: `observe` computes `samples × epochs` separately and calls a task `SHORT` when its log has fewer. So a ten-sample run relaunched for twenty is the *same* identifier and a genuinely different set of results, and a digest over identifiers alone would let the first acceptance cover the second silently.

    **Nor is the count enough, which is the same argument one step further in.** `limit`, `sample_id` and `sample_shuffle` are identity-neutral *and* count-neutral: `(0, 5)` and `(5, 10)` name five samples each, so two genuinely different result sets hashed identically and an acknowledgment of the first silently covered the second. `sandbox` and `model_base_url` do not move the count either and make every answer a different one. So the run's shaping fields go into the digest beside the per-task rows — they belong to the eval set rather than to any task, which is why they are hashed once rather than per row.

    Sorted, so that a capture which enumerates the same tasks in a different order is the same result set rather than a new one.

    Args:
        manifest: A committed manifest.

    Returns:
        `sha256:<hex>` over each task's identifier, sample count and epochs, and the run's effective shaping fields.
    """
    rows = sorted(
        f"{task.identifier}\t{task.samples}\t{task.epochs}" for task in manifest.tasks
    )
    shape = [f"{name}\t{_shaping(manifest, name)!r}" for name in SHAPING]
    joined = "\n".join([*rows, *shape])
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


SELECTION = ("limit", "sample_id", "sample_shuffle")
"""The `eval_set()` arguments that decide *which* samples run, rather than how many.

All three are identity-neutral, all three are recorded in a log's own config, and all three are recorded in a manifest's `options` — which is what makes the comparison in `_reshaped` both necessary and possible. `epochs` is not here because the count check already covers it.
"""


REDIRECTION = ("sandbox", "model_base_url")
"""The `eval_set()` arguments that decide what a task *talks to*, rather than which samples it runs.

Both are identity-neutral and both plainly change the answer. Unlike `SELECTION` they are not in a manifest's `options`, so only an override moves them — see `_redirected`, and `_launch.delta` which reports the same change at the moment somebody types it.
"""


SHAPING: tuple[str, ...] = (*SELECTION, *REDIRECTION)
"""Everything that changes a run's results without changing a task identifier or a sample count.

The union of the two comparisons `observe` makes per task. Named here as one tuple because the digest's question is different from theirs — not *is this log stale* but *is this the same set of results somebody already accepted* — and both answers have to come from the same list or an acceptance covers a run it never saw.
"""


def _shaping(manifest: Manifest, name: str) -> Any:
    """One shaping field's effective value: the override, or what the definition passed."""
    override = getattr(manifest.overrides, name, None)
    return override if override is not None else manifest.options.get(name)


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
