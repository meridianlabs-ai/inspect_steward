"""Online scanning — Steward's half of the bracket.

Workers dispatch scanners per settled sample and write scout's per-transcript buffer; Steward owns the scan directory's lifecycle (init at launch, fold and finalize in the tend) as its single writer. See execution.md §4.2–4.4.
"""

from .bracket import (
    ScanError,
    existing_eval_set_id,
    initialize_scan,
    merged_scanners,
    scan_digest,
    scan_dir_location,
    scan_location,
    scan_material,
    verify_scan,
)
from .builtin import INTEGRITY_SCANNER, builtin_scanners
from .findings import ScanFindings, scan_findings
from .model import SCOUT_SCAN_MODEL, establish_scan_model
from .summary import finalize_scan, rebuild_summary, sync_scan

__all__ = [
    "INTEGRITY_SCANNER",
    "SCOUT_SCAN_MODEL",
    "ScanError",
    "ScanFindings",
    "builtin_scanners",
    "establish_scan_model",
    "existing_eval_set_id",
    "finalize_scan",
    "initialize_scan",
    "merged_scanners",
    "rebuild_summary",
    "scan_dir_location",
    "scan_findings",
    "scan_location",
    "scan_digest",
    "scan_material",
    "sync_scan",
    "verify_scan",
]
