Real-Estate Data Quality Runbook and Lineage

Purpose

This runbook explains how Houston real-estate data travels through the
Portfolio Dashboard, how the quality gate evaluates it, and how to respond to
common outcomes. It applies only to the canonical PORTFOLIO_APP schema.
The legacy ADMIN tables are not dashboard sources.

The quality check is intentionally read-only:

It makes no RentCast API calls.

It makes no Oracle inserts, updates, deletes, or DDL changes.

It reads canonical reporting views and audit records only.

Architecture and data lineage

flowchart TD
    listing_api["RentCast sale-listing API"] --> listing_etl["etl_realestate.py"]
    listing_etl --> validation["Pydantic validation"]
    validation --> core["RE_PROPERTY and RE_LISTING_SNAPSHOT"]
    listing_etl --> audit["RE_ETL_RUN"]
    core --> current_listing["VW_RE_CURRENT_LISTING"]

    current_listing --> rent_candidates["etl_rent_estimates.py candidate selection"]
    rent_candidates --> rent_api["RentCast long-term rent AVM"]
    rent_api --> rent_snapshot["RE_RENT_ESTIMATE_SNAPSHOT"]
    rent_snapshot --> current_rent["VW_RE_CURRENT_RENT_ESTIMATE"]
    audit --> current_analysis["VW_RE_CURRENT_INVESTMENT_ANALYSIS"]
    current_listing --> current_analysis
    current_rent --> current_analysis

    current_analysis --> quality_gate["check_data_quality.py"]
    current_analysis --> dashboard["Streamlit app.py"]

The two API workflows have different purposes:

Workflow

Trigger

Output

Important boundary

Listing ETL

python etl_realestate.py

Current market listings and historical listing snapshots

Calls the RentCast sale-listings endpoint.

Rent-estimate pilot

python etl_rent_estimates.py --live --confirm-live-run --limit N

Historical provider rent estimates

Calls the RentCast rent AVM. Use only a deliberately approved small limit.

Quality gate

python check_data_quality.py

Terminal report and exit status

Read-only; does not run either ETL.

Dashboard

streamlit run app.py

Human-readable investment analysis

Read-only; does not call RentCast.

Canonical database objects

Object

Grain

Role

RE_ETL_RUN

One row per pipeline execution

Audit trail: source, timestamps, status, row counts, and error message.

RE_PROPERTY

One row per source property

Stable property attributes and provider identity.

RE_LISTING_SNAPSHOT

One row per property per listing ETL run

Historical asking-price and listing-status observations.

RE_RENT_ESTIMATE_SNAPSHOT

One row per property per rent-estimate run

Historical RentCast AVM estimates, ranges, comparables, source, and scope.

VW_RE_CURRENT_LISTING

One current listing row per property

Latest listing snapshot joined to its property attributes.

VW_RE_CURRENT_RENT_ESTIMATE

One current rent estimate per property

Latest rent-estimate snapshot for a property.

VW_RE_CURRENT_INVESTMENT_ANALYSIS

One current listing row per property

Dashboard-ready join of current listings and current rent estimates.

The composite unique constraints on the snapshot tables prevent duplicate
property rows within a single ETL run. The reporting views use ROW_NUMBER()
to select the most recent snapshot for each property.

Financial-field lineage

Dashboard field

Origin

Meaning

Asking price

RE_LISTING_SNAPSHOT.asking_price

Current seller list price; not an appraisal or purchase price.

Estimated monthly rent

RE_RENT_ESTIMATE_SNAPSHOT.estimated_monthly_rent

Provider AVM estimate for the whole property in the current Townhouse pilot.

Rent range

rent_range_low, rent_range_high

Provider uncertainty range, not a guarantee of income.

Estimated gross yield

Annual estimated rent divided by asking price

Screening metric only; it is not cap rate or cash flow.

Estimated cap rate

Dashboard scenario calculation

Depends on user-entered expenses and rent assumptions.

Estimated monthly cash flow

Dashboard scenario calculation

Depends on financing and operating-cost assumptions.

A missing rent estimate remains NULL throughout the canonical views. The
dashboard labels it Needs rent estimate instead of treating it as zero
rent or a zero-percent yield.

Quality gate

Run the standard check from the repository root:

python check_data_quality.py

The default thresholds are:

Threshold

Default

Outcome when breached

Reason

Maximum listing age

48 hours

Error and exit code 1

Active-market listings become unreliable when stale.

Maximum rent-estimate age

30 days

Warning

Rent enrichment is a deliberate, partial pilot rather than a daily feed.

Minimum rent-estimate coverage

10%

Warning

A baseline that shows whether enrichment coverage has materially declined.

The thresholds are adjustable without changing code:

python check_data_quality.py --max-listing-age-hours 72
python check_data_quality.py --min-rent-estimate-coverage-pct 15
python check_data_quality.py --fail-on-warning

--fail-on-warning is useful for a future scheduled job. It makes warnings
return exit code 1; use it only after agreeing that each warning should block
the workflow.

Check classifications

Check

Severity

Why it matters

No current listings

Error

The dashboard has no market population to analyze.

Duplicate current listing rows

Error

The one-property/one-current-row reporting assumption is broken.

Missing or non-positive asking price

Error

Yield and scenario calculations would be invalid.

Duplicate rent-estimate property/run group

Error

The historical estimate grain is broken.

Missing latitude or longitude

Warning

The row cannot be shown on the map, but can still be analyzed.

Missing bedrooms, bathrooms, or valid square footage

Warning

Limits property comparison and rent-AVM input quality.

Rent coverage below baseline

Warning

The portfolio has fewer enriched listings than expected.

Latest listing ETL failed, is missing, or is stale

Error

Current-listing analysis is no longer trustworthy.

Latest rent-estimate ETL failed, is missing, or is stale

Warning

The pilot is partial; existing estimates can still be displayed with caution.

An Error makes the script return exit code 1. A Warning returns exit
code 0 by default, while clearly recording the condition in the report.

Expected baseline from the initial pilot

The initial verified pilot contained:

Metric

Verified value

Current active listings

135

Rent estimates stored for distinct properties

19

Rent-estimate coverage

14.07%

Duplicate rent-estimate property/run groups

0

Asking-price coverage

100%

Latitude and longitude coverage

100%

These numbers are an example baseline, not a permanent target. Listing
availability naturally changes when new source data is loaded.

Standard operating procedure

1. Before reviewing the dashboard

python -m pytest -q
python check_data_quality.py

Review the terminal report before relying on dashboard rankings or scenario
results. A quality-gate error means the data should be refreshed or repaired
before analysis.

2. Refresh listing data when it is stale

Only do this when you intend to make a RentCast listing API request:

python etl_realestate.py
python check_data_quality.py

Then start or refresh the dashboard:

streamlit run app.py

Do not change an ETL timestamp manually to make the freshness check pass.
The audit row must reflect a real completed pipeline run.

3. Enrich rent estimates deliberately

The rent AVM is a cost-controlled pilot. First inspect candidates without API
calls:

python etl_rent_estimates.py --dry-run --limit 10

Only after reviewing the candidates and approving API usage, run a capped live
request:

python etl_rent_estimates.py --live --confirm-live-run --limit 1

Then run the quality gate again. Do not schedule or bulk-expand rent requests
until the desired coverage, budget, and property-scope rules are documented.

Failure playbooks

Listing freshness error

Example: The report says listing freshness is above its configured
threshold.

Confirm the error came from check_data_quality.py, not the dashboard.

Review the latest listing run in the report and inspect
etl_pipeline.log.

Decide whether it is appropriate to incur another listing API request.

If approved, run python etl_realestate.py.

Re-run python check_data_quality.py.

If the ETL fails, preserve the log and investigate the failure. Do not
suppress the error by increasing the threshold without a documented reason.

Latest listing ETL is failed

Inspect the terminal traceback and etl_pipeline.log.

Query RE_ETL_RUN in Oracle Database Actions to confirm the run status
and error message.

Fix the cause in code or configuration.

Run the unit tests and compile check before retrying:

python -m py_compile etl_realestate.py
python -m pytest -q

Retry the ETL only when the configuration and API usage are approved.

Low rent-estimate coverage warning

Low coverage is expected while the pilot is intentionally limited. It is not
a data corruption event.

Confirm the dashboard still labels missing rent as unknown.

Review the candidate dry run.

Decide whether a small additional pilot batch is justified by the API
budget and learning objective.

Record the coverage change after any approved enrichment.

Duplicate or invalid data error

Do not use the dashboard ranking for a decision until the error is fixed.

Identify the affected property keys with a read-only Oracle query.

Determine whether the problem came from source data, validation, upsert
logic, or a reporting-view change.

Add or update a unit test that reproduces the defect.

Apply a versioned migration only when a schema change is required; do not
edit production table structure manually.

Database connection failure

Run python test_connection.py.

Confirm the local .env contains valid DB_USER, DB_PASSWORD, and
DB_DSN values. Do not print, commit, or paste those values.

Confirm the application user is PORTFOLIO_APP.

Verify the Autonomous Database network access and wallet-less TLS
configuration.

Security and change-management rules

Keep .env, Streamlit secrets, wallet files, certificates, and API keys
out of Git.

Use PORTFOLIO_APP for application objects and canonical schema changes.

Add schema changes as numbered SQL migrations. Run them once in Oracle
Database Actions while connected as PORTFOLIO_APP.

Do not make the dashboard or quality checker write to the database.

Commit code, tests, migrations, and documentation together when they
describe one logical change.

Before pushing, run:

git diff --check
python -m pytest -q

Future operational maturity

When the project is ready for automation, add a scheduled workflow that runs
unit tests and the read-only quality gate. Keep API-calling ETLs separate from
that workflow until API budget, secrets management, and retry behavior are
explicitly approved.

## Operations

- [Data quality runbook and lineage](docs/data-quality-runbook.md)