## Unreleased

- `integrity_scanner: false` (or `STEWARD_INTEGRITY_SCANNER=false`) turns off the built-in `scoring_integrity` scanner. The definition's own scanners and the `scanners` key are unaffected; with nothing left to scan, the launch lays down no scan directory and the smoke reports `scan_coverage` as unexercised. Turning it off over rows the built-in already recorded is refused at launch, like any removed scanner.
- Definition arguments (`steward launch -A KEY=VALUE`, and `steward tasks -A`) now reach a plain `eval_set()` script as well as a Flow spec. A script receives each as its own `--key=value` option (`-A shard=0/3` becomes `--shard=0/3`), read with ordinary `argparse`, at capture and in every worker; the arguments are recorded in the manifest and reused on re-launch, as Flow's are.

## 0.2.9 (07 September 2026)

- **Default change: stuck samples are now handled by the agent.** The agent may cancel a stuck tool call without asking (`stuck_cancel`, now default `true`) and, if the sample is still stuck, cancel and requeue it for one fresh attempt (`stuck_action`, new, default `retry`). A sample that wedges again after its retry goes to the operator. Set `stuck_action` to `score`, `error`, or `cancel` for a different standing outcome, or opt out with `stuck_cancel: false` and `stuck_action: none` (or the `STEWARD_*` variables). Requeuing beyond the single guarded retry remains operator-only, and Steward itself still never cancels anything: the grants decide the item's owner and pre-fill the command, and the agent executes and journals.

- Built-in posture for construction-rooted scoring-integrity findings: a `reward_hacking` or `scoring_artifact` finding from the built-in scanner is the agent's to confirm and rule `score --by agent` — the fault ships with the corpus and recurs on every run, so excluding or zeroing invents a number no other run has. The window's item and collect line now carry the doctrine and the self-recordable `rule` command in place of a proposal; `steward propose --action exclude|zero` on such a class cautions (and still records, since the carve-outs — a successful escape, misconduct the corpus does not explain — travel that path); the signoff readiness line and the signature echo tally findings scored as recorded separately, pointing at `analysis.md`.

- systemd timer: set `KillMode=process` on the tend service. Under the default `control-group`, systemd killed every process left in the unit's cgroup when the oneshot tend exited, so a worker a scheduled tend started was killed seconds later (`start_new_session` does not leave the cgroup). Re-arm the timer (`steward timer arm`) to pick up the new unit; a hand-written `killmode.conf` drop-in is no longer needed.

- Reasoning-on-the-wire smoke: count Google's `thoughtSignature` marker, the camelCase key the genai SDK serializes on the wire, alongside the snake_case `thought_signature`. Without it a Google request that plainly carried a replayed reasoning block counted zero markers, and the check read a real replay as a dropped block — a false failure rather than a signature defect upstream.

## 0.2.8 (23 September 2026)

- Interim scoring: keep each interim metric's originating scorer, so a running task's headline resolves correctly when two dict-valued scorers emit the same score name (e.g. both a deterministic and an adjudicated scorer reporting `hijack`). Reads the `scorer`/`name` pair from Inspect's interim response (0.3.266+), falling back to the pre-0.3.266 single field for an older worker; the `.steward/interim.json` cache version is bumped, so a stale cache is discarded and re-harvested rather than migrated.
- Record host memory and swap each tend, show the figures and a two-hour trend under the resources table, hold the ramp while headroom is low, and raise a `memory` item for the agent when the host is short or on course to run out (the runbook's remedy is swap on Linux, or lower concurrency). A worker that died without a traceback now carries the host's headroom at the previous tend as evidence.
- Raised the `inspect-ai` floor to 0.3.266 (for the interim-scoring `scorer`/`name` split above) and the `inspect_flow` extra to 0.13.1 (the first release that handles Inspect's 3-tuple `eval_resolve_tasks` return, added with review policies in 0.3.266). Task-qualified `--sample-id` selectors are now resolved against every task name in the run when a log is compared to the manifest, matching `eval_run`, so a namespaced id whose prefix is not a task in the run (e.g. `user:cybergym/arvo_6008`) is no longer mistaken for a selector and dropped.

## 0.2.7 (13 September 2026)

- Agent no longer makes proactive tuning proposals when the samples ramp reaches its maximum.
- Lower the default `samples_ramp` ceiling from 200 to 150.

## 0.2.6 (10 September 2026)

- Land `zero` rulings on corpora whose sample ids contain a colon (work around inspect_ai reading the id's prefix as a task name); a side run that lands no usable log now fails rather than deferring forever, and the sign-off failure points at a run log with content and at the side worker logs.

## 0.2.5 (09 September 2026)

- Stop a scheduled agent collect from tearing down its own schedule.
- Improve runbook to more decisively prompt for signoff gates.

## 0.2.4 (08 September 2026)

- Bound adaptive model connections: spawn workers with a fixed range (min 20, max the samples ramp's top) and raise the ceiling only on genuine sustained saturation.
- More accurate model context window detection, using the window the definition resolved at capture and checking it during smoke runs.
- Ensure that tool call approval is not applied to llm_scanner `answer()` tool.
- Prevent overlapping scheduled agent collects: a collect that outruns its interval holds a lock so the next scheduler fire is skipped rather than stacking a second agent on top.

## 0.2.3 (07 September 2026)

- Improved handling of live scan results (fold periodically, cleanup local buffer).

## 0.2.2 (07 September 2026)

- Codex CLI background scheduling command (`steward schedule --agent codex`).
- Cleanup status.md formatting (bold not headers, exclude log).

## 0.2.1 (06 September 2026)

- Unify agent and slack notification rendering.
- Accept `INSPECT_LOG_DIR` as definition of log root dir.

## 0.2.0 (06 September 2026)

- Initial release.
