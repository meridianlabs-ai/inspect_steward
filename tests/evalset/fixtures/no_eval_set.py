from inspect_ai import eval_set


# references eval_set (so type detection succeeds) but never calls it
def never_called() -> None:
    eval_set(tasks=[], log_dir="logs")
