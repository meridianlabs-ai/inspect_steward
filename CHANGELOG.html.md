# changelog – Inspect Steward

## 0.2.5 (09 September 2026)

- Stop a scheduled agent collect from tearing down its own schedule.
- Improve runbook to more decisively prompt for signoff gates.

## 0.2.4 (08 September 2026)

- Bound adaptive model connections: spawn workers with a fixed range (min 20, max the samples ramp’s top) and raise the ceiling only on genuine sustained saturation.
- More accurate model context window detection, using the window the definition resolved at capture and checking it during smoke runs.
- Ensure that tool call approval is not applied to llm_scanner [answer()](https://inspect.aisi.org.uk/reference/inspect_ai.scorer.html#answer) tool.
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
