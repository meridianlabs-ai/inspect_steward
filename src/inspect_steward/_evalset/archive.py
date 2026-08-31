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
    return _move(location, archive_dir(log_dir), log_dir)


def restore_log(location: str, log_dir: str) -> str:
    """Move one log back out of the archive, into the run's directory.

    The inverse, and what makes the archive a cache rather than only a graveyard: edit a task's args, launch, decide the edit was wrong, revert, launch again — the original identifier is back in the manifest and its log is sitting here, so satisfying it is a move rather than a re-run of a task that already ran (workflow.md §2.2).

    **Only `launch` calls this, and only for an identifier the new manifest asks for.** A tend must not: `reconcile` archives orphans, and a converging loop that also un-archived would have two rules that can disagree about one file, which is a loop rather than a fixed point. Restoring is a decision about *desired state*, and desired state is committed at exactly one place.

    Args:
        location: The archived log, as `observe_logs` reported it from `archive_dir`.
        log_dir: The run's log directory, which the log is coming back into.

    Returns:
        Where the log now is.

    Raises:
        OSError: If the move fails. The same direction as its inverse: a log that could not be restored is still a log, and the task it belongs to simply runs again.
    """
    return _move(location, log_dir, log_dir)


def _move(location: str, destination: str, log_dir: str) -> str:
    """Relocate one log, never over the top of another.

    Args:
        location: The log to move.
        destination: The directory it is going to, created if absent.
        log_dir: The run's log directory, which names the filesystem both ends live on. Both directions move between a directory and its own sibling, so one filesystem covers them.

    Returns:
        Where the log now is.

    Raises:
        OSError: If the move fails, or if the destination already holds `_MAX_COLLISIONS` logs of this name.
    """
    fs = filesystem(log_dir)
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
