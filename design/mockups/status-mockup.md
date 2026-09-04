<!-- Written by `steward tend`. Regenerated every turn; edits are lost. -->

⚠️ 1 needs an operator · tended 4m ago · agent: 4 open items, collected 12m ago

| task | samples | done | running | queued | limit | score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `cybench@openai/gpt-5` | 37/40 | 92% |  |  |  | 0.62 |
| `cybench@anthropic/claude-opus-5` | 38/40 | 95% |  |  |  | 0.71 |
| `swe_bench_lite@openai/gpt-5` | 117/120 | 98% |  |  |  | 0.33 |
| `swe_bench_lite@anthropic/claude-opus-5 (8/16)` | 61/120 | 51% | 8 | 51 | 1.1M/10Mtk |  |
| `gaia@openai/gpt-5` | 165/165 | 100% |  |  |  | 0.48 |
| `gaia@anthropic/claude-opus-5` | 0/165 | 0% |  |  |  |  |

### operator

- gaia[default]@anthropic/claude-opus-5 has stopped making progress after 2 attempts and will not be respawned — `stalled:gaia:02719224:2`

### anomalies

| task                            | zero | nan | error | early | term |
|---------------------------------|-----:|----:|------:|------:|-----:|
| cybench@openai/gpt-5            |    · |   3 |     · |     · |    · |
| cybench@anthropic/claude-opus-5 |    · |   2 |     · |     · |    · |
| swe_bench_lite@openai/gpt-5     |    · |   · |     2 |     1 |    · |

### resources

```
task                                    refusals  retries  memory  cpu
swe_bench_lite@anthropic/claude-opus-5         0       12  2.1 GB  1.4
```

**Logs** /home/kaia/runs/sweep-0903/logs
