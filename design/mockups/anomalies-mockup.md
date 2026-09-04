# anomalies

<!-- Written by `steward tend`. Regenerated every turn; edits are lost. -->

Every caveat that reached the final data, and per task the samples that did not take the normal course. Derived from `journal.jsonl` and the logs; nothing here is a second record, so it cannot disagree with them.

Scores are over 645 of 650 samples (5 excluded).

## By task

| task                            | samples | zeroed | excluded | errored | scored early | terminated |
|---------------------------------|--------:|-------:|---------:|--------:|-------------:|-----------:|
| cybench@openai/gpt-5            |      40 |      · |        3 |       · |            · |          · |
| cybench@anthropic/claude-opus-5 |      40 |      · |        2 |       · |            · |          · |
| swe_bench_lite@openai/gpt-5     |     120 |      · |        · |       2 |            1 |          · |

3 other tasks: every sample took the normal course.

## `error:openai.APITimeoutError@openai/_client.py:post`

- **What happened** — 5 samples errored the same way
- **Scope** — 5 samples in `cybench[default]@anthropic/claude-opus-5`, `cybench[default]@openai/gpt-5`
- **Why accepted** — "provider outage 02:10-02:40 UTC; every retry landed inside it and the samples are not coming back"
- **Accepted by** — kaia, at 2026-09-04T13:12:06.752Z
- **Effect on the data** — 5 samples excluded from scoring
- **Samples** — `cybench[default]@anthropic/claude-opus-5/cb-08:1`, `cybench[default]@anthropic/claude-opus-5/cb-31:1`, `cybench[default]@openai/gpt-5/cb-03:1`, `cybench[default]@openai/gpt-5/cb-11:1`, `cybench[default]@openai/gpt-5/cb-27:1`
