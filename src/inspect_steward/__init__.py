from ._evalset.manifest import (
    Manifest,
    ManifestSource,
    ManifestTask,
)
from ._evalset.read import ReadEvalSetError, read_eval_set

try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"


__all__ = [
    "Manifest",
    "ManifestSource",
    "ManifestTask",
    "ReadEvalSetError",
    "read_eval_set",
    "__version__",
]
