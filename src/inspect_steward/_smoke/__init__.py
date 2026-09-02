"""The rehearsal that runs before the sweep.

`steward launch --smoke`: a couple of samples per task under a wall-clock cap, into a log directory of its own, to find out whether the thing works at all before committing a night and real money to it (workflow.md §7.1).

**Bounded and untended, which is what keeps it small.** It spawns once, watches, stops on its deadline, folds its scan and reports — no manifest committed, no timer armed, no reconcile. Nothing tends it, so nothing respawns the workers its cap stops, and the per-identifier *do not start this again* record that a cancelled log would otherwise need stays with `steward stop`, where it actually bites.
"""

from .checks import (
    CHECKS,
    CONTEXT_WINDOW,
    REASONING,
    REASONING_API,
    SCAN_COVERAGE,
    Check,
    Probe,
    Verdict,
    Window,
    probe,
    scan_coverage,
    window,
)
from .digest import (
    Outcome,
    Smoke,
    digest_markdown,
    echo_smoke,
    findings,
    journal_fields,
    outcome,
)

__all__ = [
    "CHECKS",
    "CONTEXT_WINDOW",
    "REASONING",
    "REASONING_API",
    "SCAN_COVERAGE",
    "Check",
    "Outcome",
    "Probe",
    "Smoke",
    "Verdict",
    "Window",
    "digest_markdown",
    "echo_smoke",
    "findings",
    "journal_fields",
    "outcome",
    "probe",
    "scan_coverage",
    "window",
]
