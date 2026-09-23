# Canonical daily-price refresh

## Purpose

`etl_market_prices.py` refreshes the canonical `MK_DAILY_PRICE` table from
yfinance after the Finnhub daily-candle endpoint proved unavailable to this
project's key. It does not alter the legacy `ADMIN.STOCK_HISTORY` data or any
existing canonical price row.

The loader is append-only. A source row is inserted only when the
`(security_key, trade_date)` pair does not already exist. This preserves clear
lineage:

| Data period | Source name | Meaning |
| --- | --- | --- |
| Through 2026-09-08 | `LEGACY_ADMIN_STOCK_HISTORY` | Historical research migrated from the former ADMIN schema |
| New completed sessions | `YFINANCE_DAILY_PRICE` | Daily rows retrieved by the current yfinance refresh |

## Price definition

The canonical `close_price` receives yfinance's daily `Close` column with
`auto_adjust=False`. The pipeline does **not** substitute adjusted close,
invent missing values, or replace null/invalid provider values with zero.

The yfinance `end` date is exclusive. To avoid an intraday partial daily bar,
the pipeline requests through today before 4:30 p.m. New York time and through
tomorrow only after that buffer. Weekend and market-holiday no-data responses
are logged as no data; they do not create fabricated rows.

## Safe operating sequence

Run every command from the repository root:

```bash
cd /workspaces/portfolio-dashboard-clean

python -m py_compile \
  etl_market_prices.py \
  check_market_data_quality.py \
  portfolio_dashboard/market_data_quality.py \
  tests/test_etl_market_prices.py \
  tests/test_market_data_quality.py

python -m pytest -q \
  tests/test_etl_market_prices.py \
  tests/test_market_data_quality.py
```

First make a one-request provider probe. It does not connect to Oracle:

```bash
python etl_market_prices.py --probe-yfinance AAPL
```

Then make a read-only one-security selection. It makes no yfinance request and
writes no Oracle rows:

```bash
python etl_market_prices.py --dry-run --limit 1
```

Run the one-security live pilot only after those checks pass:

```bash
python etl_market_prices.py \
  --live \
  --confirm-live-run \
  --limit 1
```

Inspect the audit row in Database Actions before requesting the whole
watchlist:

```sql
SELECT
    run_id,
    source_name,
    run_status,
    extracted_count,
    validated_count,
    rejected_count,
    loaded_count,
    started_at,
    completed_at,
    error_message
FROM mk_etl_run
WHERE source_name = 'YFINANCE_DAILY_PRICE'
ORDER BY run_id DESC;
```

Then perform the capped all-watchlist refresh:

```bash
python etl_market_prices.py --dry-run --all-watchlist

python etl_market_prices.py \
  --live \
  --confirm-live-run \
  --all-watchlist \
  --verify-price-quality
```

`--all-watchlist` has a fixed cap of 100 securities and cannot be combined
with `--limit`. The current watchlist has 44 securities, so it is within that
cap. The loader pauses 0.5 seconds between live provider requests by default;
you can use `--request-delay-seconds` from 0 through 5 if required.

If the provider returns a missing or non-finite `Close` or `Volume`, the row is
rejected and is never replaced with zero. The audit run becomes `PARTIAL` when
one or more rows or provider requests are rejected. Inspect and resolve that
condition before treating the refresh as fully clean; a coverage-only report
can still pass when previously loaded price rows are within its freshness window.

## Verification and honest interpretation

Run the daily-price scope after a refresh:

```bash
python check_market_data_quality.py \
  --scope price-refresh \
  --min-price-history-coverage-pct 100 \
  --max-price-age-days 7
```

Then run the complete report:

```bash
python check_market_data_quality.py
```

The `price-refresh` scope evaluates daily-price coverage, duplicates, and
freshness only. The full report also evaluates quote freshness and fundamentals
lineage. It can therefore remain `PASS_WITH_WARNINGS` when a fundamentals
snapshot has no provider reporting date, even after the daily prices are
fresh. That is the desired behavior: retrieval time and source remain visible,
and unavailable provider reporting dates are not guessed.

## Regular use

On a business day after the market-close buffer, repeat:

```bash
python etl_market_prices.py \
  --live \
  --confirm-live-run \
  --all-watchlist \
  --verify-price-quality
```

The candidate query skips a security once a successful or partial canonical run
has loaded the latest expected completed weekday. Re-running on the same day is
therefore an idempotent no-op rather than a rewrite.
