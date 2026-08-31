"""inspect_ai entry point: imports here register steward's components with the inspect_ai registry.

Workers resolve Steward's injected scanners from `ScannerSpec`s by registry name alone (`scanner_create("inspect_steward/scoring_integrity", ...)`), so the scanner module must be imported in every process that scans — this module, named by the `inspect_ai` entry point in `pyproject.toml`, is how that import happens without anyone asking for it.
"""

from ._scan.integrity import scoring_integrity

__all__ = ["scoring_integrity"]
