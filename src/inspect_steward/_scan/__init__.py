"""Online scanning — Steward's half of the bracket.

Workers dispatch scanners per settled sample and write scout's per-transcript buffer; Steward owns the scan directory's lifecycle (init at launch, fold and finalize in the tend) as its single writer. See execution.md §4.2–4.4.
"""

from .bracket import (
    ScanError,
    initialize_scan,
    merged_scanners,
    scan_dir_location,
    scan_location,
    scan_material,
    verify_scan,
)
from .builtin import INTEGRITY_SCANNER, builtin_scanners
from .model import SCOUT_SCAN_MODEL, establish_scan_model
from .summary import finalize_scan, rebuild_summary

__all__ = [
    "INTEGRITY_SCANNER",
    "SCOUT_SCAN_MODEL",
    "ScanError",
    "builtin_scanners",
    "establish_scan_model",
    "finalize_scan",
    "initialize_scan",
    "merged_scanners",
    "rebuild_summary",
    "scan_dir_location",
    "scan_location",
    "scan_material",
    "verify_scan",
]
