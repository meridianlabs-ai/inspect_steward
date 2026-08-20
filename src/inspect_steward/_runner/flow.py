"""Conforming program for Inspect Flow definitions.

Loads a flow spec and makes its `eval_set()` call (via `inspect_flow.api.eval_set`, which has none of `flow run`'s store/flow.yaml/log-scanning behavior). Run by Steward with the eval-set capture (and, in the future, selection) environment applied.
"""

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> None:
    from inspect_flow.api import eval_set, load_spec

    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Path to the flow spec (.py or .yaml).")
    parser.add_argument(
        "--args", default="{}", help="JSON dict of args for the spec function."
    )
    parsed = parser.parse_args()

    args: dict[str, Any] = json.loads(parsed.args)
    spec = load_spec(parsed.file, args=args or None)
    # match the flow CLI: relative paths resolve against the spec file's parent
    eval_set(spec, base_dir=str(Path(parsed.file).resolve().parent))


if __name__ == "__main__":
    main()
