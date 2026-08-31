"""The scanners Steward brings to every merge.

One scanner today: `scoring_integrity` (`integrity.py`), an LLM reviewer. It joins the merge unconditionally, because it can almost always find a reviewer: a scanner that names no model of its own scans with the sample's model under evaluation, or — for a "none"-model eval — the first model role (ambient defaults installed by upstream's scan-model context; `scan_model` is the explicit override that outranks both). Only an eval with no model *and* no roles has nothing to inherit, and there a `scan_model` must be configured explicitly or every dispatch records a scan error saying so.

The spec is built in `ScannerSpec` dict form — the shape the selection document carries and `scanners_from_spec_dict` resolves — with `file` unset, because the scanner lives in Steward's installed package and resolves by registry name (`_registry` guarantees the registering import in every process). `package_version` records which Steward authored the rows; it is provenance, not identity, and re-launch verification must not read it as a changed scanner (setuptools-scm moves it every commit).
"""

import importlib.metadata
from typing import Any

from inspect_scout import ScannerSpec

INTEGRITY_SCANNER = "scoring_integrity"
"""Merge key of the built-in scanner — the name collisions are refused against."""


def builtin_scanners() -> dict[str, dict[str, Any]]:
    """Steward's own scanners, as `ScannerSpec` dicts keyed by merge name.

    Returns:
        Specs to inject beside the operator's.
    """
    return {
        INTEGRITY_SCANNER: ScannerSpec(
            name="inspect_steward/scoring_integrity",
            package_version=importlib.metadata.version("inspect_steward"),
        ).model_dump(mode="json", exclude_none=True)
    }
