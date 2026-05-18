# Cost math

Reference doc for the `three-cent-contacts` skill. Built by Chris Berkley · [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts).

## Per-stage cost

| Stage | Cost |
|-------|------|
| 1 — Sonar (OpenRouter) | $0.005 per call |
| 2 — Site scrape (httpx) | $0 (your bandwidth) |
| 3 — Pattern read | $0 (local) |
| 4 — Hunter pattern | $0.01 per domain lookup |
| 5 — Apply pattern | $0 (local) |
| 6 — Bouncer verify | $0.01 per email checked |
| 7 — Bright Data SERP + Gemini Flash parse | ~$0.015-0.025 per row |

## Per-row cost (typical)

Cost is **cumulative across every stage a row fires**. `stage_hit` in the output CSV reports the last stage that resolved the row, but the cost field captures everything paid to get there.

| Path | Stages that fired | Cost | % of typical batch |
|------|-------------------|------|---|
| Sonar hit, verified | 1 + 6 | $0.015 | 60-70% |
| Site scrape hit, pattern from email, verified | 1 + 2 + 3 + 6 | $0.015 | 10-15% |
| Hunter pattern needed, verified | 1 + 2 + 4 + 5 + 6 | $0.025 | 10-15% |
| SERP fallback for hard target, verified | 1 + 2 + 4 + 5 + 7 + 6 | $0.045 | 3-8% |
| Unresolved (all stages tried, nothing verified) | 1 + 2 + 4 + 7 + 6 | $0.045 | 3-5% |

**Blended typical: ~$0.018-0.028 per verified contact.**

## Pre-run estimate

When the user provides a batch, estimate the **ceiling** (worst case) by assuming every row hits stage 7:

```
ceiling = rows × $0.04
```

Show that to the user before running batches ≥ 50 rows. Actual will almost always come in 30-50% lower than the ceiling.

## Mid-batch cost guard

If `--max-cost-per-row` is set and an individual row's stages exceed it, abort that row and mark it `cost_exceeded` in the output. Don't bail the whole batch — just that row.

## What the costs don't include

- Your time configuring API keys (one-time, ~10 min)
- API minimum spend / monthly fees (mostly $0; OpenRouter is pay-as-you-go)
- Output destination costs (writing to Google Sheets / HubSpot / Smartlead is free per row but consumes their API quotas)
