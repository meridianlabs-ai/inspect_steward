"""The launch's half of the scan bracket, at the functions the launch composes.

No launches and no workers: the merge, the verification, and the init are file work over serialized specs, so every hazard they guard is expressible with a `tmp_path` and a handful of dicts. The launch-shaped composition — refusal exit codes, nothing-written guarantees, the journal — is `tests/launch/test_launch_cli.py`'s; what the worker does with an injected spec is upstream's (`test_eval_set_scanner_selection.py`).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.manifest import ManifestScan
from inspect_steward._scan import (
    INTEGRITY_SCANNER,
    ScanError,
    builtin_scanners,
    initialize_scan,
    merged_scanners,
    scan_material,
    verify_scan,
)

MINE = {"name": "some_pkg/mine"}
"""An operator scanner reference, in the dict form `_steward.yaml` carries."""


def definition_scan(scanners: dict[str, dict[str, Any]]) -> ManifestScan:
    """Capture's material for a definition declaring these scanners."""
    return ManifestScan(spec={"scan_name": "eval_set", "scanners": scanners})


def test_the_builtin_scanner_rides_every_merge() -> None:
    material = scan_material(None, None)
    assert material.injected == builtin_scanners()
    assert set(merged_scanners(material)) == {INTEGRITY_SCANNER}
    assert material.spec is None


def test_operator_scanners_join_the_injection_beside_the_builtin() -> None:
    material = scan_material(None, {"mine": MINE})
    assert material.injected is not None
    assert set(material.injected) == {INTEGRITY_SCANNER, "mine"}


def test_the_definitions_own_scanners_survive_the_merge_untouched() -> None:
    material = scan_material(definition_scan({"declared": MINE}), {"mine": MINE})
    assert set(merged_scanners(material)) == {"declared", INTEGRITY_SCANNER, "mine"}
    assert material.spec == definition_scan({"declared": MINE}).spec


def test_an_invalid_scanner_reference_is_refused_by_name() -> None:
    with pytest.raises(ScanError, match="mine"):
        scan_material(None, {"mine": {"params": {}}})


def test_a_collision_with_the_builtin_is_refused() -> None:
    with pytest.raises(ScanError, match=INTEGRITY_SCANNER):
        scan_material(None, {INTEGRITY_SCANNER: MINE})


def test_a_collision_with_the_definitions_scanners_is_refused() -> None:
    with pytest.raises(ScanError, match="mine"):
        scan_material(definition_scan({"mine": MINE}), {"mine": MINE})


def prior_scan_dir(
    log_dir: Path,
    scanners: dict[str, dict[str, Any]],
    scan_id: str = "run-1",
    scans: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """A scan directory as an earlier launch would have left it."""
    (log_dir / ".eval-set-id").write_text(scan_id)
    scan_dir = (
        scans if scans is not None else log_dir / "scans"
    ) / f"scan_id={scan_id}"
    scan_dir.mkdir(parents=True)
    (scan_dir / "_scan.json").write_text(
        json.dumps(
            {
                "scan_id": scan_id,
                "scan_name": "eval_set",
                "scanners": scanners,
                **({"metadata": metadata} if metadata is not None else {}),
            }
        )
    )


def test_verification_passes_where_no_fleet_has_run(tmp_path: Path) -> None:
    verify_scan(scan_material(None, None), log_dir=str(tmp_path), eval_set_id=None)


def test_an_unchanged_set_verifies_against_its_own_directory(tmp_path: Path) -> None:
    material = scan_material(None, {"mine": MINE})
    prior_scan_dir(tmp_path, merged_scanners(material))
    verify_scan(material, log_dir=str(tmp_path), eval_set_id=None)


def test_a_changed_scanner_refuses(tmp_path: Path) -> None:
    prior_scan_dir(
        tmp_path,
        {**builtin_scanners(), "mine": {"name": "some_pkg/mine", "params": {"x": 1}}},
    )
    with pytest.raises(ScanError, match="changed: mine"):
        verify_scan(
            scan_material(None, {"mine": MINE}),
            log_dir=str(tmp_path),
            eval_set_id=None,
        )


def test_a_removed_scanner_refuses(tmp_path: Path) -> None:
    prior_scan_dir(tmp_path, {**builtin_scanners(), "mine": MINE})
    with pytest.raises(ScanError, match="removed: mine"):
        verify_scan(scan_material(None, None), log_dir=str(tmp_path), eval_set_id=None)


def test_an_added_scanner_is_admitted(tmp_path: Path) -> None:
    prior_scan_dir(tmp_path, builtin_scanners())
    verify_scan(
        scan_material(None, {"mine": MINE}), log_dir=str(tmp_path), eval_set_id=None
    )


HASH_KEY = "__inspect_scan_config_hash__"
"""Where capture stamps the config-wrapper hash into the spec's metadata (`_scan_config_hash` upstream — filter, scan model, generation settings; not the scanner list)."""


def wrapped_scan(hash: str) -> ManifestScan:
    """Capture's material for a definition whose config wrapper hashes to `hash`."""
    return ManifestScan(
        spec={
            "scan_name": "eval_set",
            "scanners": {"declared": MINE},
            "metadata": {HASH_KEY: hash},
        }
    )


def test_a_changed_config_wrapper_refuses(tmp_path: Path) -> None:
    """The filter, scan model, and generation settings live in no scanner's own spec — the hash capture stamped into the metadata is what carries them here."""
    material = scan_material(wrapped_scan("aaa"), None)
    prior_scan_dir(tmp_path, merged_scanners(material), metadata={HASH_KEY: "before"})
    with pytest.raises(ScanError, match="configuration"):
        verify_scan(material, log_dir=str(tmp_path), eval_set_id=None)


def test_an_unchanged_config_wrapper_passes(tmp_path: Path) -> None:
    material = scan_material(wrapped_scan("aaa"), None)
    prior_scan_dir(tmp_path, merged_scanners(material), metadata={HASH_KEY: "aaa"})
    verify_scan(material, log_dir=str(tmp_path), eval_set_id=None)


def test_a_wrapper_arriving_with_first_scanners_is_admitted(tmp_path: Path) -> None:
    """A run that scanned with the built-in alone has no hash on disk; the definition's first scanners bring one, and they are the admitted *added* case."""
    prior_scan_dir(tmp_path, builtin_scanners())
    verify_scan(
        scan_material(wrapped_scan("aaa"), None),
        log_dir=str(tmp_path),
        eval_set_id=None,
    )


def test_a_moved_redirect_with_recorded_rows_refuses(tmp_path: Path) -> None:
    """A redirect change lands verification on an empty location, so only the committed manifest's answer can surface the rows the move would strand."""
    committed = scan_material(None, None)
    prior_scan_dir(tmp_path, merged_scanners(committed))
    moved = committed.model_copy(update={"scans": str(tmp_path / "elsewhere")})
    with pytest.raises(ScanError) as err:
        verify_scan(moved, log_dir=str(tmp_path), eval_set_id=None, committed=committed)
    # the remedy is a move, so the message must name both ends of it
    assert str(tmp_path / "elsewhere" / "scan_id=run-1") in str(err.value)
    assert str(tmp_path / "scans" / "scan_id=run-1") in str(err.value)


def test_a_moved_redirect_with_nothing_recorded_passes(tmp_path: Path) -> None:
    """A fleet ran (the id is stamped) but never scanned at the committed location — there is nothing a new redirect could strand."""
    (tmp_path / ".eval-set-id").write_text("run-1")
    committed = scan_material(None, None)
    moved = committed.model_copy(update={"scans": str(tmp_path / "elsewhere")})
    verify_scan(moved, log_dir=str(tmp_path), eval_set_id=None, committed=committed)


def test_moving_the_rows_to_the_new_redirect_is_the_remedy(tmp_path: Path) -> None:
    """With the directory moved where the new redirect points, the same launch passes — the refusal is escapable by exactly the action it names."""
    committed = scan_material(None, None)
    moved = committed.model_copy(update={"scans": str(tmp_path / "elsewhere")})
    prior_scan_dir(tmp_path, merged_scanners(moved), scans=tmp_path / "elsewhere")
    verify_scan(moved, log_dir=str(tmp_path), eval_set_id=None, committed=committed)


def test_the_committed_redirect_resolves_against_the_committed_log_dir(
    tmp_path: Path,
) -> None:
    """When the log directory moved too, the recorded scan is beside the *old* logs — the committed redirect must resolve there, not against the new `log_dir`."""
    old_logs = tmp_path / "old"
    old_logs.mkdir()
    committed = scan_material(None, None)
    prior_scan_dir(old_logs, merged_scanners(committed))
    moved = committed.model_copy(update={"scans": str(tmp_path / "elsewhere")})
    with pytest.raises(ScanError) as err:
        verify_scan(
            moved,
            log_dir=str(tmp_path / "new"),
            eval_set_id=None,
            committed=committed,
            committed_log_dir=str(old_logs),
        )
    assert str(old_logs / "scans" / "scan_id=run-1") in str(err.value)


def test_package_version_drift_is_provenance_not_identity(tmp_path: Path) -> None:
    """A development install's version moves on every commit, and rows recorded yesterday are still the same scanner's."""
    aged = {
        name: {**entry, "package_version": "0.0.0"}
        for name, entry in builtin_scanners().items()
    }
    prior_scan_dir(tmp_path, aged)
    verify_scan(scan_material(None, None), log_dir=str(tmp_path), eval_set_id=None)


def test_initialize_lays_the_directory_down_and_stamps_the_id(tmp_path: Path) -> None:
    """The merged set — injection included, definition or not — is what the directory records, because finalize derives its orphan-cleanup names from exactly this file."""
    material = scan_material(None, {"mine": MINE})
    scan_dir = initialize_scan(material, log_dir=str(tmp_path), scan_id="run-9")

    spec = json.loads((Path(scan_dir) / "_scan.json").read_text())
    assert spec["scan_id"] == "run-9"
    assert set(spec["scanners"]) == {INTEGRITY_SCANNER, "mine"}

    # what init wrote is what verification reads: the round trip is the
    # re-launch path, and it must pass with nothing changed
    verify_scan(material, log_dir=str(tmp_path), eval_set_id="run-9")

    # a re-init attaches rather than resetting, and is equally verifiable
    again = initialize_scan(material, log_dir=str(tmp_path), scan_id="run-9")
    assert again == scan_dir
    verify_scan(material, log_dir=str(tmp_path), eval_set_id="run-9")
