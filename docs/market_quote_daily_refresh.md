# Daily Market Quote Refresh

## Purpose

Refresh one current Finnhub quote snapshot for every eligible security in the
canonical market watchlist, then verify quote coverage and observation
freshness without treating legacy historical daily prices as current data.

The workflow is intentionally idempotent. A security that already has a quote
from a successful run on the current UTC day is skipped. Re-running the command
on the same day therefore creates a successful zero-row audit run instead of
duplicating quote snapshots.

## Normal daily command

Run this from the repository root after confirming that `FINNHUB_API_KEY` and
database settings are available through `.env` or Codespaces secrets:

```bash
python etl_market_quotes.py \
  --live \
  --confirm-live-run \
  --daily-refresh \
  --verify-quote-quality
```

`--daily-refresh` processes every security that has not received a successful
quote today, up to the fixed 100-security safety cap. The current 44-security
watchlist is therefore fully covered by one normal run.

The command paces provider calls by 0.25 seconds. To slow a run further, pass
for example `--request-delay-seconds 0.5`. Do not set it below zero or above
five seconds.

## Expected success result

The ETL logs a successful `FINNHUB_QUOTE` run. It then prints:

```text
MARKET QUOTE REFRESH QUALITY REPORT
Scope: current quote coverage and observation freshness only.
...
PASS: quote-refresh coverage and freshness checks passed.
```

The post-refresh gate requires:

- 100% current-quote coverage;
- an oldest quote observation no more than 30 hours old.

## Important distinction: two quality scopes

Use the quote-refresh scope after the daily quote job:

```bash
python check_market_data_quality.py --scope quote-refresh \
  --max-current-quote-age-hours 30
```

Use the default full scope for periodic data-governance review:

```bash
python check_market_data_quality.py
```

The full scope can correctly fail while the quote-refresh scope passes because
the imported daily close-price history and fundamentals are legacy historical
research data. Do not weaken the full-quality thresholds merely to make the
daily quote job report green.

## Safe preflight

Before an unfamiliar or changed run, preview the eligible cohort without API
calls or database writes:

```bash
python etl_market_quotes.py --dry-run --daily-refresh
```

## Failure handling

If the live refresh fails, inspect the newest `MK_ETL_RUN` audit row and the
sanitized `market_quote_pipeline.log`. Do not paste API keys, `.env` contents,
or raw provider URLs into an issue or commit. Correct the cause, then re-run
the normal daily command; already successful same-day securities will be
skipped automatically.
