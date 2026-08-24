"""Moving a log out of the run's directory, which is the only way one ever leaves.

**Steward never deletes an eval log.** A log leaving `logs/` is always a move — reversible, and journaled with its reason — which is a simple, checkable property for a tool asked to be trusted with unattended expensive work (workflow.md, *Steward never destroys a result, but it does curate the directory*). Where `eval_set()`'s own `cleanup_older_eval_logs` removes a superseded attempt, Steward relocates it.

What that buys is a precise meaning for the log directory, which it did not otherwise have: **`logs/` is the current definition's results and nothing else**, so `samples_df(log_dir)` is trustworthy without anyone remembering a filter.

**The archive is a sibling, not a child**, because `list_eval_logs` recurses by default — anything nested inside `log_dir` is still found by the viewer, by `samples_df`, and by every listing, so an archive underneath it would hide nothing.

**It is also a cache.** Edit a task's args, launch, decide the edit was wrong, revert: the original identifier comes back and a matching log is sitting here, so restoring it is a move rather than a re-run.
"""

from inspect_ai._util.file import basename, filesystem

_MAX_COLLISIONS = 1000


def archive_dir(log_dir: str) -> str:
    """Where a log directory's archive lives.

    Derived from `log_dir` rather than from the workspace, because the log directory is frequently somewhere else entirely — an S3 prefix the workspace only points at — and a result's archive belongs beside the result.

    Args:
        log_dir: The run's log directory (`logs/`, or `s3://…/logs`).

    Returns:
        The sibling archive (`logs-archive/`, or `s3://…/logs-archive`).
    """
    return f"{log_dir.rstrip('/')}-archive"


def archive_log(location: str, log_dir: str) -> str:
    """Move one log out of the run's directory, into its archive.

    Args:
        location: The log to move, as `observe_logs` reported it.
        log_dir: The run's log directory, which the archive is derived from.

    Returns:
        Where the log now is.

    Raises:
        OSError: If the move fails. Reported by the caller and left for the next turn to retry — a log that could not be archived is still a log, which is the direction this design fails in everywhere.
    """
    fs = filesystem(log_dir)
    destination = archive_dir(log_dir)
    fs.mkdir(destination, exist_ok=True)

    name = basename(location)
    target = f"{destination}{fs.sep}{name}"
    # a name already taken means the same log was archived under a previous
    # manifest and a later attempt reused the timestamp -- keep both, because
    # the whole point of an archive is that nothing here is thrown away
    if fs.exists(target):
        stem, dot, extension = name.rpartition(".")
        stem, extension = (stem, f"{dot}{extension}") if stem else (name, "")
        for suffix in range(1, _MAX_COLLISIONS):
            candidate = f"{destination}{fs.sep}{stem}-{suffix}{extension}"
            if not fs.exists(candidate):
                target = candidate
                break
        else:
            raise OSError(
                f"{destination} already holds {_MAX_COLLISIONS} copies of {name}"
            )

    fs.mv(location, target)
    return target
