"""Which model scanners use, and how the fleet comes to agree with it.

**One value, two consumers, the `notification` shape exactly** (`_notify.channel`). The online path resolves a scanner's model as the definition's own `EvalScannerConfig.model`, then `SCOUT_SCAN_MODEL`, then the sample's ambient context — the model under evaluation, or a "none"-model eval's first model role — and only an eval with neither leaves the `NoModel` that raises on use. Steward's `scan_model` setting is the durable spelling of the second rung: exported as `SCOUT_SCAN_MODEL`, which `_worker.spawn`'s environment spread carries into every worker — so the definition's explicit choice still wins, and where nothing is configured at all, scanning is a continuation of the sample's own work on the sample's own model.

**Reflexive, both directions.** Where only scout's variable is set, that is the setting; where Steward's spellings name one, the export overwrites a differing ambient value, because the fleet agreeing with Steward matters more than which was set first — the same rule `establish_channel` applies to inspect's variable. `scan_model: false` *clears* an ambient `SCOUT_SCAN_MODEL` from what workers inherit, which is the one thing only Steward can say: the variable itself has no spelling for "not that".

**A scheduled tend inherits neither variable**, which is why the `_steward.yaml` key earns its place — the one spelling still there at 02:00.
"""

import os

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
