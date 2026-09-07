"""Which model scanners use, and how the fleet comes to agree with it.

**One value, two consumers, the `notification` shape exactly** (`_notify.channel`). The online path resolves a scanner's model as the definition's own `EvalScannerConfig.model`, then `SCOUT_SCAN_MODEL`, then the sample's ambient context — the model under evaluation, or a "none"-model eval's first model role — and only an eval with neither leaves the `NoModel` that raises on use. Steward's `scan_model` setting is the durable spelling of the second rung: exported as `SCOUT_SCAN_MODEL`, which `_worker.spawn`'s environment spread carries into every worker — so the definition's explicit choice still wins, and where nothing is configured at all, scanning is a continuation of the sample's own work on the sample's own model.

**Reflexive, both directions.** Where only scout's variable is set, that is the setting; where Steward's spellings name one, the export overwrites a differing ambient value, because the fleet agreeing with Steward matters more than which was set first — the same rule `establish_channel` applies to inspect's variable. `scan_model: false` *clears* an ambient `SCOUT_SCAN_MODEL` from what workers inherit, which is the one thing only Steward can say: the variable itself has no spelling for "not that".

**A scheduled tend inherits neither variable**, which is why the `_steward.yaml` key earns its place — the one spelling still there at 02:00.
"""

import os
from collections.abc import Callable

SCOUT_SCAN_MODEL = "SCOUT_SCAN_MODEL"
"""Scout's scan-model variable, which Steward both reads and writes."""


def establish_scan_model(declared: str | bool | None = None) -> str | None:
    """Settle which model this process's workers scan with, and export it.

    Called once by anything that spawns, before it does. Mutates `os.environ`, deliberately: the variable is the channel a worker inherits, and scout reads it — a return value nobody could inherit would leave the fleet scanning with whatever the shell happened to hold.

    Args:
        declared: What the flag or the workspace's own spellings said — a model, `False` for none, or `None` for no preference (the caller has already resolved the flag over the file, the way `_tend.turn._settings` resolves `notification`).

    Returns:
        The explicitly configured scan model, or `None` where none is — in which case scanners fall to the ambient default (the sample's model under evaluation, then its first model role), and only a sample with neither records a scan error saying a `scan_model` must be set explicitly.
    """
    if declared is False:
        os.environ.pop(SCOUT_SCAN_MODEL, None)
        return None
    if isinstance(declared, str):
        os.environ[SCOUT_SCAN_MODEL] = declared
        return declared
    return os.environ.get(SCOUT_SCAN_MODEL, "").strip() or None


#: Type of a per-sample scan-model resolver: given the model that produced a
#: transcript (`Transcript.model`, which may be `None`), return the model to
#: scan it with, or `None` to defer to the ambient default.
ScanModelResolver = Callable[[str | None], str | None]

_resolver: "ScanModelResolver | None" = None


def set_scan_model_resolver(resolver: "ScanModelResolver | None") -> None:
    """Register a per-sample scan-model policy, or clear it with `None`.

    A **per-sample rung between the explicit scanner model and the fleet-wide
    `SCOUT_SCAN_MODEL`/ambient default.** `SCOUT_SCAN_MODEL` and a scanner's
    injected `params.model` are one value for the whole fleet — settled before
    any worker spawns — which cannot be right for a launch spanning arms of
    different vendors, where the model that should grade a transcript is a
    function of the vendor that produced it. A resolver is that function,
    consulted once per transcript inside the scan.

    Inversion of control, deliberately: Steward owns the seam and knows nothing
    of who registers it. The one built-in consumer is `scoring_integrity`, which
    calls it only when constructed with no explicit `model`; a resolver that
    returns `None` for a transcript leaves that transcript on the ambient default
    exactly as if none were registered, so registering one is safe by
    construction and never worsens the no-model case.

    Kept off the `SCOUT_SCAN_MODEL` string path on purpose: a run with no
    resolver is byte-identical to before, and the scan-config drift hash
    (`_scan.bracket`) — which keys on the settled string — is unaffected.
    """
    global _resolver
    _resolver = resolver


def scan_model_resolver() -> "ScanModelResolver | None":
    """The registered per-sample scan-model resolver, or `None`."""
    return _resolver
