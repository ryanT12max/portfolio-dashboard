Houston Real Estate Investment Dashboard
An end-to-end Python data-engineering project that collects active Houston sale listings from RentCast, validates them with Pydantic, stores them in Oracle Autonomous Database, and presents beginner-friendly investment analysis in Streamlit.

The current product is deliberately real-estate-first. Stock functionality will be added only after there is a defined holdings model and data source.

What the project does
Extracts active Multi-Family and Townhouse listings in Houston, Texas from RentCast.

Validates provider records before loading them into the database.

Upserts stable property attributes and records a time-stamped listing snapshot for every ETL run.

Audits each pipeline execution, including extracted, validated, rejected, and loaded record counts.

Shows current listings, data freshness, simple yield metrics, locations, and an assumption-driven cash-flow scenario in Streamlit.

Architecture
flowchart LR
    A[RentCast API] --> B[Pydantic validation]
    B --> C[Oracle canonical schema]
    C --> D[Current-listing view]
    D --> E[Streamlit dashboard]
Technology
Area	Implementation
Extraction	Python, Requests, RentCast API
Validation	Pydantic data contracts
Database	Oracle Cloud Autonomous Database with oracledb Thin mode
Transformation/loading	Oracle MERGE, transactional snapshots, SQL view
Dashboard	Streamlit and Pandas
Testing	Pytest unit tests
Configuration	Environment variables and .env for local development
Canonical database model
All current real-estate application objects are owned by PORTFOLIO_APP.

Object	Purpose
RE_ETL_RUN	Audit record for every ETL execution and data-quality counts.
RE_PROPERTY	Stable property identity and attributes, uniquely identified by source and provider property ID.
RE_LISTING_SNAPSHOT	Historical time-series listing observations, one per property per ETL run.
VW_RE_CURRENT_LISTING	Latest snapshot per property, including the derived gross-yield calculation.
This design separates relatively stable property details from values that change over time, such as asking price, listing status, and rent estimate.

Repository layout
portfolio-dashboard/
├── app.py                         # Streamlit real-estate dashboard
├── etl_realestate.py              # RentCast-to-Oracle ETL pipeline
├── test_connection.py             # Safe Oracle connectivity check
├── migrations/
│   └── 001_create_real_estate_core.sql
├── portfolio_dashboard/
│   ├── config.py                  # Central environment configuration
│   └── database.py                # Shared Oracle connection helpers
├── tests/
│   ├── test_config.py
│   ├── test_database.py
│   └── test_etl_realestate.py
├── .env.example
├── requirements.txt
└── requirements-dev.txt
Local setup
1. Install dependencies
python -m pip install --requirement requirements.txt
python -m pip install --requirement requirements-dev.txt
2. Create local configuration
Copy .env.example to .env, then replace each placeholder locally.

cp .env.example .env
chmod 600 .env
Required variables:

DB_USER=PORTFOLIO_APP
DB_PASSWORD=replace_with_your_database_password
DB_DSN=replace_with_your_walletless_tls_dsn
RENTCAST_API_KEY=replace_with_your_rentcast_api_key
RENTCAST_MAX_LISTINGS=50
DB_DSN is the wallet-less TLS connection value already used by your local Oracle connection. Keep real values only in .env or Codespaces secrets; never commit them.

3. Create the canonical schema
Run migrations/001_create_real_estate_core.sql once while signed in to Oracle Database Actions as PORTFOLIO_APP.

The migration creates new canonical objects; it does not modify the historical ADMIN-owned tables.

4. Verify the database connection
python test_connection.py
Expected output:

Connected successfully as: PORTFOLIO_APP
5. Run the ETL pipeline
python etl_realestate.py
The ETL writes an audit record first, validates provider records, upserts properties, inserts listing snapshots, and commits only a complete property/snapshot load.

6. Run the dashboard
streamlit run app.py
In Codespaces, open the forwarded Streamlit port from the Ports tab.

Testing
Run the full unit-test suite:

python -m pytest -q
The tests use fake configuration values and a mocked Oracle connection. They do not call RentCast or modify the database.

Data and investment-analysis notes
Gross yield is (estimated monthly rent × 12) ÷ asking price.

A missing rent estimate means unknown, not zero. RentCast sale-listing responses may not include rental estimates.

The dashboard's scenario calculator is a planning tool. Its cash-flow estimate is based on user-entered assumptions for financing, tax, insurance, maintenance, and vacancy.

The calculator does not replace property-level due diligence, an appraisal, lending terms, legal advice, or investment advice.

Operational and security practices
Credentials are loaded from environment variables through portfolio_dashboard.config.

.env, Oracle wallet files, private keys, and Streamlit secrets are ignored by Git.

The project uses wallet-less TLS with database network access restricted through an Oracle IP access-control list.

The ETL runs ALTER SESSION DISABLE PARALLEL DML to avoid the Oracle ORA-12838 batch-write issue encountered during development.

The project pins direct production dependencies and keeps Pytest in requirements-dev.txt.

Next planned improvements
Add a dedicated rental-estimate enrichment pipeline before treating yield as broadly available.

Add data-quality monitoring for rejected records, late ETL runs, and null-rate changes.

Add a separate stock holdings model and stock pipeline once personal holdings exist.

# Market Watchlist News Feature

This package adds a non-persistent Yahoo Finance news panel to the Market
Watchlist. It preserves the supplied dashboard layout and uses the existing
**Choose a ticker** selector.

## What changes

- Fetches at most three recent, linkable stories for the selected ticker using
  `yfinance.Ticker(ticker).get_news()`.
- Uses a 15-minute Streamlit cache so each viewer interaction does not create
  an unnecessary provider request.
- Shows title, publisher, publication time, provider snippet, and an external
  **Read article** link.
- Does not scrape article bodies, create an Oracle table, write database rows,
  add a secret, or invent a summary when Yahoo does not supply one.
- Filters out a story when Yahoo explicitly identifies it as related to other
  tickers, rejects unsafe/non-web links, and handles multiple known yfinance
  response shapes.
- Keeps news available even when the selected ticker's historical-price query
  is unavailable.

## Browser-only installation

1. Open `ryanT12max/portfolio-dashboard` on GitHub and press `.` to open
   github.dev.
2. Open the repository's existing `app.py`.
3. Replace its contents with this package's `app.py`.
4. Create `tests/test_market_news.py` from this package's test file.
5. Review the source-control diff and commit with:

   `feat: add selected-ticker market news panel`

6. Streamlit Community Cloud will automatically deploy the GitHub commit.

## Verification

After the deployment finishes, open the **Market Watchlist** tab, choose a
ticker, and scroll below the historical price chart. The panel should show up
to three provider stories or a clear availability message. No Oracle migration
or Streamlit Secret change is required.

## Important data-labeling behavior

The displayed text is provider-supplied metadata. A missing summary remains
missing; the dashboard must not generate or imply a summary. News is external
research context, not investment advice.