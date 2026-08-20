## Unreleased

- Configuration reading: `read_eval_set()` enumerates the tasks of an eval set definition — a Python file culminating in `eval_set()`, an Inspect Flow spec (Python or YAML, via the `[flow]` extra), or a Hawk eval set config (YAML, via the `[hawk]` extra; requires Python >= 3.13) — returning an `Manifest` with per-task identity, display keys, sample counts, and epochs. Requires an `inspect-ai` release with eval-set capture support.
- New `steward tasks` CLI command to enumerate a definition (`--json` for the full manifest).
- Initial package scaffold with the `steward` CLI and its `init` command.
