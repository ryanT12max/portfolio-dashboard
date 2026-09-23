# Rent Estimates and Market Fundamentals Enrichment Runbook

This runbook safely enriches the canonical PORTFOLIO_APP data model. It uses
small, explicit live batches and records provider lineage. Do not paste API
keys, passwords, or DSNs into source files or terminal output.

## 1. Install the one added dependency

Append the single line in requirements.additions.txt to the repository's
existing requirements.txt, then run:

~~~bash
cd /workspaces/portfolio-dashboard-clean
python -m pip install -r requirements.txt
~~~

## 2. Apply the two Oracle migrations

While connected as PORTFOLIO_APP in Oracle Database Actions, run these files
in this exact order:

1. migrations/010_create_rent_estimate_attempt_audit.sql
2. migrations/011_update_re_investment_analysis_scope.sql

Run one file at a time with Run Script. The second migration depends on the
new attempt-audit table created by the first migration.

Verify the expected objects:

~~~sql
SELECT object_name, object_type, status
FROM user_objects
WHERE object_name IN (
    'RE_RENT_ESTIMATE_ATTEMPT',
    'IX_RE_RENT_ATTEMPT_PROPERTY_OBSERVED',
    'IX_RE_RENT_ATTEMPT_STATUS',
    'VW_RE_CURRENT_INVESTMENT_ANALYSIS'
)
ORDER BY object_type, object_name;
~~~

Every returned object should be VALID.

## 3. Verify source code before a live call

Run these commands from the repository root:

~~~bash
python -m py_compile \
  app.py \
  etl_rent_estimates.py \
  etl_market_fundamentals.py \
  check_data_quality.py \
  check_market_data_quality.py \
  portfolio_dashboard/data_quality.py \
  portfolio_dashboard/market_data_quality.py

python -m pytest -q \
  tests/test_etl_rent_estimates.py \
  tests/test_etl_market_fundamentals.py \
  tests/test_data_quality.py \
  tests/test_market_data_quality.py \
  tests/test_app.py

python etl_rent_estimates.py --help
python etl_market_fundamentals.py --help
~~~

## 4. Refresh listings before spending rent-AVM requests

The backfill acts on the current canonical listing view. Refresh listings first
when the real-estate quality report says the listing ETL is stale:

~~~bash
python etl_realestate.py
python check_data_quality.py
~~~

Do not backfill rents for an old listing population and call the result live.

## 5. RentCast backfill: one listing, then ten listings

This first command reads candidates only:

~~~bash
python etl_rent_estimates.py \
  --dry-run \
  --coverage-backfill \
  --limit 1
~~~

The log must show the property type and scope. PROPERTY is eligible for
whole-property gross yield. UNIT is a Multi-Family or Apartment single-unit
estimate and is never used as whole-building yield.

Run the one-listing live pilot:

~~~bash
python etl_rent_estimates.py \
  --live \
  --confirm-live-run \
  --coverage-backfill \
  --limit 1
~~~

If the audit row is acceptable, process the next ten:

~~~bash
python etl_rent_estimates.py \
  --live \
  --confirm-live-run \
  --coverage-backfill \
  --limit 10
~~~

Run at most one ten-listing batch at a time. Review the output and
check_data_quality.py before manually starting the next batch. Continue until
the dry run selects zero candidates or only documented RETRY_PENDING records
remain.

Known-unavailable RentCast responses are stored as UNAVAILABLE and excluded
from future batches. Transient provider failures remain eligible for a later
retry. This prevents an endless paid retry loop while preserving a truthful
coverage denominator.

## 6. Finnhub capability probe: one API call, no database write

The probe calls only Finnhub's Basic Financials endpoint for one ticker. It
does not insert a database row and does not use yfinance:

~~~bash
python etl_market_fundamentals.py --dry-run --limit 1
python etl_market_fundamentals.py --probe-finnhub --ticker AAPL
~~~

If the probe succeeds, Finnhub Basic Financials is available. If it reports an
HTTP error or no usable metrics, do not change the key in code. The later live
pipeline will use its documented yfinance fallback.

## 7. Fundamentals ETL: one, ten, then all eligible securities

The live pipeline tries Finnhub first. It uses yfinance only if Finnhub fails,
returns no usable values, or omits a required canonical metric. It stores one
provider-attributed snapshot per successful candidate.

Start with one security:

~~~bash
python etl_market_fundamentals.py \
  --live \
  --confirm-live-run \
  --limit 1
~~~

Then process ten new eligible securities:

~~~bash
python etl_market_fundamentals.py \
  --live \
  --confirm-live-run \
  --limit 10
~~~

Finally process every security not successfully enriched today:

~~~bash
python etl_market_fundamentals.py \
  --live \
  --confirm-live-run \
  --all-watchlist \
  --verify-fundamentals-quality
~~~

The final command can produce either:

- PASS — all watchlist securities have a current canonical fundamentals
  snapshot; or
- PASS_WITH_WARNINGS — coverage is complete but one or more providers did
  not disclose an as-of date; or
- FAIL — one or more securities still lacks a usable provider snapshot.

## 8. Inspect coverage honestly

Run the scoped fundamentals report:

~~~bash
python check_market_data_quality.py \
  --scope fundamentals-refresh \
  --min-fundamentals-coverage-pct 100
~~~

Then run the full report:

~~~bash
python check_market_data_quality.py
~~~

The full report may still fail because the historical daily-price series is
intentionally stale. That does not invalidate the live quote or fundamentals
coverage result; it is a separate data-quality issue.

## 9. Dashboard interpretation

Start Streamlit only after the migrations and smoke tests complete:

~~~bash
streamlit run app.py
~~~

The real-estate table displays Rent scope and Rent status. A UNIT scope is
shown as research context only. Its gross-yield cell remains blank, and the
scenario calculator requires a separately verified whole-property rent
assumption.
# Address-only rent-estimate exception path

Listings with complete bedrooms, bathrooms, and square footage use those values
as explicit RentCast AVM inputs. If a current listing has a complete address and
asking price but is missing one or more of those subject-property attributes, do
not invent the missing value. After migration `012`, run the separate
address-only dry run:

```bash
python etl_rent_estimates.py --dry-run --address-only-backfill --limit 1
```

This sends no provider request and writes no Oracle row. A validated candidate
will be labeled `PROVIDER_LOOKUP`, meaning the live command will send only its
address and let RentCast perform its documented subject-attribute lookup. It
does not combine known values with partial missing values, and it never uses the
coordinate fallback because that fallback cannot perform automatic lookup.

The capped live pilot is:

```bash
python etl_rent_estimates.py \
  --live \
  --confirm-live-run \
  --address-only-backfill \
  --limit 1
```

The new `subject_attribute_mode` field is stored on both the estimate snapshot
and its audit attempt. `PROVIDED_ATTRIBUTES` identifies the existing strict
path; `PROVIDER_LOOKUP` identifies this address-only exception. Dashboard users
should verify the provider's looked-up subject facts before relying on a rent
estimate, and the data-quality report will continue to flag the underlying
listing's missing core attributes.
