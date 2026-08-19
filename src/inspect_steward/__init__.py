from .hello import hello

try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"


__all__ = [
    "hello",
    "__version__",
]
