"""Moving a log between a store and a run, so that no reader ever meets half of one.

**A log arrives under its own name, and the name is the whole of what anybody checks.** Inspect writes a timestamp, the task and a hash, so a name already present is *this log* — which is what makes publication idempotent and a reuse copy skippable. That reasoning is sound and it silently assumed something it never verified: that a file sitting under that name is a **finished** one.

An interrupted copy breaks the assumption in the worst available direction. The partial file is at the final path under the final name, so the next publication sees the name, calls it already published, and reports a success — while `observe_logs` cannot read the file and the store answers no query about it. A publication that landed nothing reported `count=1, failed=()`. The reuse path fails the same way one step further on, recording a task as satisfied from a store by a file nothing can read.

Two changes, and they are the two halves of one rule — *a file under the final name is a finished file*:

- **Nothing is written at the final path.** The copy lands beside it under a unique `.part` name and is renamed into place, which is atomic on every filesystem this runs on. A reader sees the log or nothing, never a prefix of it. The suffix keeps it out of `observe_logs`' reach, which reads `.eval` and `.json` only, so an abandoned staging file is inert rather than a log that will not parse.
- **An existing destination is verified rather than trusted.** Sizes are compared against the source, which is exactly the check the failure mode calls for — a truncation is a size difference and nothing else is expected to be — and costs one stat where reading a header costs a parse. A destination that does not match is the wreckage of an earlier attempt and is replaced; one that matches is the same log and is left exactly alone, because overwriting it would be Steward rewriting a result to no purpose.

**What is not handled is a crash between the copy and the rename**, which leaves a `.part` file nobody collects. That is the failure this trades *down* to: an inert stray file in a directory, rather than a store confidently serving a name it cannot read.
"""

from uuid import uuid4

from inspect_ai._util.file import FileSystem, copy_file, filesystem

PART = "part"
"""Suffix a staged copy carries until it is renamed into place.

Outside the `.eval` and `.json` that `observe_logs` reads, so a staging file that outlives its copy is invisible to every reader rather than being a log that will not parse.
"""


def copy_log(source: str, target: str, fs: FileSystem) -> bool:
    """Put `source` at `target`, atomically, unless it is already there whole.

    Args:
        source: The log to copy, wherever it lives.
        target: Where it should end up.
        fs: The filesystem `target` is on. The source's own is resolved here, since the two are frequently different — publishing goes from a local run to a remote store and reuse comes back the other way.

    Returns:
        Whether this call actually wrote the log. `False` means a complete copy was already there, which is a distinction the caller needs rather than a detail: for a directory store, *the file is the row*, so a log that was already present is somebody else's publication and not one this project may later withdraw.

    Raises:
        Exception: Whatever the filesystems raise. Not narrowed — either side may be an fsspec backend whose failures are its own — and both callers degrade it to a warning.
    """
    if (
        fs.exists(target)
        and fs.info(target).size == filesystem(source).info(source).size
    ):
        return False
    staged = f"{target}.{uuid4().hex}.{PART}"
    try:
        copy_file(source, staged)
        # only reachable where the target is wreckage: an intact one returned
        # above, so what is removed here is a file no reader could read
        if fs.exists(target):
            fs.rm(target)
        fs.mv(staged, target)
    except Exception:
        _discard(staged, fs)
        raise
    return True


def _discard(staged: str, fs: FileSystem) -> None:
    """Clear a staging file whose copy did not finish.

    Silent on failure, which is the one place that is right: this runs on the way out of an exception that is about to be raised, and a cleanup that fails would replace a caller's real diagnosis with a complaint about a temporary file.
    """
    try:
        fs.rm(staged)
    except Exception:
        pass


__all__ = ["copy_log"]
