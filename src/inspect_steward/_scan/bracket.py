"""The launch's half of the scan bracket: merge, verify, initialize.

Workers record and never bracket (execution.md §4.2): every hazard in the scan directory's lifecycle — the create-or-attach race, the finalize that prunes unlanded rows, the complete flag — belongs to a single writer, and the launch is where that writer does its first half. The other half (fold and finalize) is the tend's.

Three steps, in launch order. `scan_material` settles the merge: the definition's own scanners as capture serialized them, Steward's built-in, and the operator's `scanners` key — collisions refused by name, because either resolution silently changes what one of the two parties records. `verify_scan` compares the settled merge against a scan directory a previous launch may have left: a changed or removed scanner refuses (its recorded rows would mean something the new launch no longer says), an added one is admitted (rows begin from here; catch-up over already-settled samples is an open question, reported not solved — execution.md §13). `initialize_scan` lays the directory down from the serialized spec, after the archive gate and beside the other `log_dir` mutations, so a refused launch has written nothing.

Identity deliberately excludes `package_version`: it is provenance (which Steward or which package authored the rows), and under setuptools-scm it moves on every commit — reading it as identity would refuse every re-launch of a development install.
"""

import json
from typing import Any

# the scan bracket helpers are selection-mode machinery, deliberately not
# public API (the same posture as the capture and overrides models); the
# config-hash key is imported rather than copied so the two ends cannot drift
from inspect_ai._eval.task.scan import (
    _INSPECT_CONFIG_HASH_KEY,  # pyright: ignore[reportPrivateUsage]
    scan_init_from_spec,
)
from inspect_ai._util._async import run_coroutine
from inspect_ai._util.file import exists, file
from inspect_scout import ScannerSpec
from pydantic import ValidationError

from .._evalset.manifest import ManifestScan
from .builtin import builtin_scanners

EVAL_SET_ID_FILE = ".eval-set-id"
"""Where inspect persists a log directory's eval set id (`eval_set_id_for_log_dir`).

Read here and never written: `verify_scan` runs before the archive gate, where a refused launch must leave nothing behind, so it only *locates* a scan directory an earlier fleet could have created — and a directory without this file has had no fleet, hence no scan directory to verify against.
"""


class ScanError(Exception):
    """The run's scanning configuration cannot be honoured.

    A message for a person: a scanner reference that does not parse, a name collision, or a re-launch whose scanners disagree with rows already recorded. The launch wraps it (`LaunchError`) like every other definition-shaped refusal.
    """


def scan_material(
    captured: ManifestScan | None,
    scanners: dict[str, dict[str, Any]] | None,
) -> ManifestScan:
    """Settle what this run scans with: capture's word plus Steward's injection.

    Always returns material, because the built-in scanner rides every run — a scanner that names no model scans with the sample's own model under evaluation, so there is no configuration in which it could not run (`builtin.py`). The caller records the result as `Manifest.scan` unconditionally.

    Args:
        captured: What capture serialized (`read_eval_set`), or `None` where the definition declares no scanners.
        scanners: The operator's `scanners` key — scout `ScannerSpec` dicts keyed by merge name — or `None`.

    Returns:
        The material a launch commits: the captured spec and location untouched, with the injection settled.

    Raises:
        ScanError: An operator scanner reference is invalid, or a name collides — with the built-in or with the definition's own scanners.
    """
    injected: dict[str, dict[str, Any]] = builtin_scanners()
    for name, entry in (scanners or {}).items():
        if name in injected:
            raise ScanError(
                f"scanner '{name}' collides with Steward's built-in scanner of "
                "the same name. Rename it — or remove the entry, if the "
                "built-in is what was meant"
            )
        try:
            ScannerSpec.model_validate(entry)
        except ValidationError as ex:
            raise ScanError(
                f"scanner '{name}' is not a valid scanner reference:\n{ex}"
            ) from ex
        injected[name] = entry

    definition = set(_definition_scanners(captured))
    collisions = sorted(definition & set(injected))
    if collisions:
        raise ScanError(
            "scanner name(s) collide with the definition's own scanners: "
            f"{', '.join(collisions)}. Rename them — a collision cannot be "
            "resolved without silently changing what one of the two records"
        )

    return ManifestScan(
        spec=captured.spec if captured is not None else None,
        scans=captured.scans if captured is not None else None,
        injected=injected,
    )


def merged_scanners(material: ManifestScan) -> dict[str, dict[str, Any]]:
    """Every scanner this run records, keyed by name: the definition's own plus the injected."""
    return {**_definition_scanners(material), **(material.injected or {})}


def scan_location(material: ManifestScan, *, log_dir: str, scan_id: str) -> str:
    """Where this run's scan directory is, from its committed material."""
    return scan_dir_location(log_dir=log_dir, scan_id=scan_id, scans=material.scans)


def scan_dir_location(*, log_dir: str, scan_id: str, scans: str | None) -> str:
    """Where a run's scan directory is.

    Mirrors the computation `scan_init_from_spec` performs (and workers repeat through `verify_selection_scan_dir`): the `scans` redirect or `<log_dir>/scans`, then one directory per scan id.
    """
    base = (scans or f"{log_dir.rstrip('/')}/scans").rstrip("/")
    return f"{base}/scan_id={scan_id}"


def verify_scan(
    material: ManifestScan,
    *,
    log_dir: str,
    eval_set_id: str | None,
    committed: ManifestScan | None = None,
    committed_log_dir: str | None = None,
) -> None:
    """Refuse a re-launch whose scanners disagree with rows already recorded.

    Keyed off the scan directory itself rather than anything in `.steward/`, which this design tells people they may delete: the rows live beside the logs, so the comparison must too. A *changed* scanner refuses — its recorded verdicts would carry the old meaning under the new name, which is exactly the silent drift `manifest_digest` refuses for tasks. A *removed* one refuses on the same grounds: the finalize would treat its rows as belonging to nothing. An *added* one is admitted, recording from here forward. The config *wrapper* — filter, scan model, generation settings — is compared through the hash capture stamps into the spec's metadata, since those fields change what every scanner records without appearing in any scanner's own spec.

    **The `scans` redirect is verified against the committed manifest, read back rather than recomputed** — the `Manifest.log_dir` argument, one directory over. Checking only the *requested* location would compare a fresh directory against itself: a definition that moves its redirect lands on an empty location, verifies trivially, and strands every recorded row where nothing will look. So a changed redirect refuses while the committed location still holds a scan — and moving the directory to the new location is the remedy, after which the same check passes. A moved `log_dir` under an unchanged redirect is deliberately not this check's business: that relocation strands the logs too, and the launch's delta already gates it.

    Read-only, because it runs before the archive gate. Where the log directory has no eval set id and the manifest names none, no fleet has run and there is nothing to verify.

    Args:
        material: The merge as this launch settled it.
        log_dir: The run's log directory, as this launch resolved it.
        eval_set_id: The manifest's eval set id, if the definition named one.
        committed: The committed manifest's scan material, or `None` where this workspace has not launched (or predates scanning) — the redirect check needs the previous answer, and only the committed manifest remembers it.
        committed_log_dir: Where the committed run's results actually are (`_launch._previous`), against which the committed redirect resolves.

    Raises:
        ScanError: A requested scanner differs from, or is missing against, the one recorded on disk — or the redirect moved while rows remain at the committed location.
        OSError: The scan directory exists but its spec cannot be read.
    """
    scan_id = _existing_eval_set_id(log_dir) or eval_set_id
    if committed is not None and committed.scans != material.scans:
        previous_dir = committed_log_dir or log_dir
        previous_id = _existing_eval_set_id(previous_dir) or eval_set_id
        if previous_id is not None:
            recorded = scan_dir_location(
                log_dir=previous_dir, scan_id=previous_id, scans=committed.scans
            )
            if exists(f"{recorded}/_scan.json"):
                requested_dir = scan_dir_location(
                    log_dir=log_dir, scan_id=previous_id, scans=material.scans
                )
                raise ScanError(
                    f"the definition moved its scan output to {requested_dir} "
                    f"while the run's recorded scan is at {recorded}. A launch "
                    "that quietly started a second scan would leave everything "
                    "recorded so far where nothing looks. Move the scan "
                    "directory to the new location, or restore the previous "
                    "`scans`"
                )
    if scan_id is None:
        return
    scan_json = (
        f"{scan_location(material, log_dir=log_dir, scan_id=scan_id)}/_scan.json"
    )
    if not exists(scan_json):
        return
    with file(scan_json, "r") as f:
        prior_spec = json.loads(f.read())
    prior = prior_spec.get("scanners", {})

    requested = merged_scanners(material)
    changed = sorted(
        name
        for name, entry in requested.items()
        if name in prior and _identity(prior[name]) != _identity(entry)
    )
    removed = sorted(name for name in prior if name not in requested)
    if changed or removed:
        details: list[str] = []
        if changed:
            details.append(f"changed: {', '.join(changed)}")
        if removed:
            details.append(f"removed: {', '.join(removed)}")
        raise ScanError(
            "this launch's scanners disagree with what the run has already "
            f"recorded ({'; '.join(details)}). A scanner's recorded verdicts "
            "carry the configuration they were made under, so changing or "
            "removing one mid-run would silently change what its rows mean. "
            "Restore the previous configuration, or start a fresh run"
        )

    # the config wrapper too: a `ScannerConfig`'s filter, scan-side model,
    # and generation config live in no `ScannerSpec`, yet changing them
    # changes what every scanner records. Capture hashes exactly those fields
    # into the spec's metadata (`_scan_config_hash` upstream — the scanner
    # list and the labels are deliberately outside it, so an added scanner
    # still passes here), and the comparison rides the hash rather than
    # re-deriving the fields. One-sided absence passes: a wrapper appearing
    # with a definition's first scanners is those scanners arriving, which is
    # the admitted case
    prior_metadata: dict[str, Any] = prior_spec.get("metadata") or {}
    new_metadata: dict[str, Any] = (material.spec or {}).get("metadata") or {}
    prior_hash = prior_metadata.get(_INSPECT_CONFIG_HASH_KEY)
    new_hash = new_metadata.get(_INSPECT_CONFIG_HASH_KEY)
    if prior_hash is not None and new_hash is not None and prior_hash != new_hash:
        raise ScanError(
            "this launch's scanner configuration (the filter, scan model, "
            "or generation settings around the scanners) disagrees with what "
            "the run has already recorded under. Rows recorded so far carry "
            "the old configuration's meaning, so changing it mid-run would "
            "silently mix the two. Restore the previous configuration, or "
            "start a fresh run"
        )


def initialize_scan(material: ManifestScan, *, log_dir: str, scan_id: str) -> str:
    """Lay the scan directory down — or re-attach to it — from the serialized spec.

    The first half of the bracket, run once per launch after the archive gate: workers require the directory to exist (`verify_selection_scan_dir`) and only ever record into it. Attach semantics (a fresh spec over a preserved transcript snapshot, the finalized flag invalidated) live upstream in `scan_init_from_spec`; the verification that makes attaching safe already ran (`verify_scan`).

    Returns:
        The scan directory.
    """
    spec: dict[str, Any] = dict(material.spec or {"scan_name": "eval_set"})
    spec["scanners"] = merged_scanners(material)
    return run_coroutine(
        scan_init_from_spec(
            spec, scan_id=scan_id, log_dir=log_dir, scans=material.scans
        )
    )


def _definition_scanners(material: ManifestScan | None) -> dict[str, dict[str, Any]]:
    """The definition's own scanners from the captured spec, empty where it declares none."""
    if material is None or material.spec is None:
        return dict[str, dict[str, Any]]()
    return dict(material.spec.get("scanners", {}))


def _existing_eval_set_id(log_dir: str) -> str | None:
    """The id a previous fleet stamped into the log directory, or `None`."""
    id_file = f"{log_dir.rstrip('/')}/{EVAL_SET_ID_FILE}"
    if not exists(id_file):
        return None
    with file(id_file, "r") as f:
        return f.read().strip() or None


def _identity(entry: dict[str, Any]) -> dict[str, Any]:
    """A scanner reference reduced to what makes it the same scanner.

    Normalized through `ScannerSpec` so that defaulted and spelled-out forms compare equal, with `package_version` excluded — provenance, not identity (see the module docstring).
    """
    return ScannerSpec.model_validate(entry).model_dump(
        mode="json", exclude={"package_version"}, exclude_none=True
    )
