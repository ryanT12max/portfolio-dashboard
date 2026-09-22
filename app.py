"""Streamlit dashboard for canonical real-estate and market-watchlist data.

The dashboard reads only PORTFOLIO_APP reporting views. It surfaces data
freshness and coverage limitations so historical or incomplete data is never
presented as current investment advice.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from portfolio_dashboard.config import ConfigurationError
from portfolio_dashboard.database import managed_connection


CURRENT_LISTINGS_SQL = """
    SELECT
        property_key,
        source_property_id,
        street_address,
        city,
        state_code,
        zip_code,
        property_type,
        bedrooms,
        bathrooms,
        structure_sqft,
        lot_size_sqft,
        year_built,
        latitude,
        longitude,
        observed_at,
        listing_date,
        last_seen_date,
        listing_status,
        asking_price,
        est_monthly_rent,
        gross_yield_pct
    FROM vw_re_current_listing
    WHERE listing_status = 'Active' OR listing_status IS NULL
    ORDER BY observed_at DESC, asking_price ASC
"""

LATEST_REAL_ESTATE_RUN_SQL = """
    SELECT
        run_id,
        started_at,
        completed_at,
        run_status,
        extracted_count,
        validated_count,
        rejected_count,
        loaded_count
    FROM re_etl_run
    ORDER BY run_id DESC
    FETCH FIRST 1 ROW ONLY
"""

MARKET_WATCHLIST_SQL = """
    SELECT
        security_key,
        ticker,
        sector,
        latest_trade_date,
        latest_close_price,
        latest_volume,
        latest_price_source,
        latest_price_observed_at,
        fundamental_snapshot_id,
        fundamentals_source,
        fundamentals_source_as_of_date,
        fundamentals_observed_at,
        fundamental_market_price,
        pe_ratio,
        peg_ratio,
        pb_ratio,
        earnings_per_share,
        return_on_equity_pct,
        profit_margin_pct,
        debt_to_equity_ratio,
        free_cash_flow_value,
        free_cash_flow_unit,
        price_history_status,
        fundamentals_status
    FROM vw_mk_watchlist_analysis
    ORDER BY ticker
"""

MARKET_PRICE_HISTORY_SQL = """
    SELECT
        ticker,
        trade_date,
        close_price,
        volume,
        source_name,
        observed_at
    FROM vw_mk_price_history
    WHERE ticker = :ticker
    ORDER BY trade_date
"""


@st.cache_data(ttl=300, show_spinner=False)
def load_real_estate_dashboard_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load current listings and the latest real-estate ETL audit record."""
    with managed_connection() as connection:
        listings = pd.read_sql_query(CURRENT_LISTINGS_SQL, con=connection)
        latest_run = pd.read_sql_query(LATEST_REAL_ESTATE_RUN_SQL, con=connection)

    listings.columns = [column.lower() for column in listings.columns]
    latest_run.columns = [column.lower() for column in latest_run.columns]

    for column in ("observed_at", "listing_date", "last_seen_date"):
        if column in listings:
            listings[column] = pd.to_datetime(listings[column], errors="coerce")

    return listings, latest_run


@st.cache_data(ttl=300, show_spinner=False)
def load_market_watchlist_data() -> pd.DataFrame:
    """Load the canonical market-watchlist analysis view."""
    with managed_connection() as connection:
        watchlist = pd.read_sql_query(MARKET_WATCHLIST_SQL, con=connection)

    watchlist.columns = [column.lower() for column in watchlist.columns]

    for column in (
        "latest_trade_date",
        "latest_price_observed_at",
        "fundamentals_source_as_of_date",
        "fundamentals_observed_at",
    ):
        if column in watchlist:
            watchlist[column] = pd.to_datetime(watchlist[column], errors="coerce")

    return watchlist


@st.cache_data(ttl=300, show_spinner=False)
def load_market_price_history(ticker: str) -> pd.DataFrame:
    """Load one security's canonical historical close-price series."""
    with managed_connection() as connection:
        history = pd.read_sql_query(
            MARKET_PRICE_HISTORY_SQL,
            con=connection,
            params={"ticker": ticker},
        )

    history.columns = [column.lower() for column in history.columns]
    if "trade_date" in history:
        history["trade_date"] = pd.to_datetime(history["trade_date"], errors="coerce")
    if "observed_at" in history:
        history["observed_at"] = pd.to_datetime(history["observed_at"], errors="coerce")

    return history


def money(value: float | int | None) -> str:
    """Format an optional numeric value as whole-dollar currency."""
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.0f}"


def percent(value: float | int | None) -> str:
    """Format an optional numeric value as a percentage."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.2f}%"


def decimal(value: float | int | None) -> str:
    """Format an optional numeric value without implying a unit or currency."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.2f}"


def timestamp_text(value: object) -> str:
    """Format an optional pandas or Oracle date/timestamp for the dashboard."""
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return "—"
    return timestamp.strftime("%Y-%m-%d")


def market_price_age_days(latest_trade_date: object) -> int | None:
    """Return calendar-day age for the most recent stored market price."""
    timestamp = pd.to_datetime(latest_trade_date, errors="coerce")
    if pd.isna(timestamp):
        return None
    return (datetime.now(timezone.utc).date() - timestamp.date()).days


def mortgage_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """Calculate the principal-and-interest payment for a fixed-rate loan."""
    if principal <= 0:
        return 0.0

    monthly_rate = annual_rate_pct / 100 / 12
    payment_count = term_years * 12

    if monthly_rate == 0:
        return principal / payment_count

    return principal * (
        monthly_rate * (1 + monthly_rate) ** payment_count
    ) / ((1 + monthly_rate) ** payment_count - 1)


def render_scenario_calculator(listings: pd.DataFrame) -> None:
    """Render an assumption-driven underwriting scenario for one listing."""
    st.subheader("Investment Scenario Calculator")
    st.caption(
        "This is a planning tool, not an appraisal, lending quote, or investment recommendation."
    )

    options = listings["property_key"].tolist()
    lookup = listings.set_index("property_key")

    selected_key = st.selectbox(
        "Choose a listing",
        options,
        format_func=lambda key: (
            f"{lookup.loc[key, 'street_address']} "
            f"({money(lookup.loc[key, 'asking_price'])})"
        ),
    )
    selected = lookup.loc[selected_key]

    suggested_rent = selected["est_monthly_rent"]
    rent_default = float(suggested_rent) if pd.notna(suggested_rent) else 0.0

    assumptions, results = st.columns(2)

    with assumptions:
        st.markdown("#### Assumptions")
        purchase_price = st.number_input(
            "Purchase price",
            min_value=0.0,
            value=float(selected["asking_price"]),
            step=5_000.0,
        )
        monthly_rent = st.number_input(
            "Monthly rent assumption",
            min_value=0.0,
            value=rent_default,
            step=100.0,
            help="RentCast sale listings may not include an estimated rent. Enter your own assumption when needed.",
        )
        down_payment_pct = st.slider("Down payment (%)", 0, 100, 20, 5)
        interest_rate_pct = st.number_input(
            "Annual interest rate (%)",
            min_value=0.0,
            max_value=20.0,
            value=6.5,
            step=0.1,
        )
        loan_term_years = st.selectbox("Loan term (years)", (15, 20, 30), index=2)

    with results:
        st.markdown("#### Scenario results")
        down_payment = purchase_price * down_payment_pct / 100
        principal = purchase_price - down_payment
        monthly_pi = mortgage_payment(principal, interest_rate_pct, loan_term_years)

        property_tax_pct = st.number_input(
            "Property-tax assumption (%)",
            min_value=0.0,
            max_value=10.0,
            value=2.1,
            step=0.1,
        )
        annual_insurance = st.number_input(
            "Annual insurance assumption",
            min_value=0.0,
            value=2_400.0,
            step=100.0,
        )
        maintenance_pct = st.slider("Maintenance reserve (% of rent)", 0, 30, 8, 1)
        vacancy_pct = st.slider("Vacancy reserve (% of rent)", 0, 30, 5, 1)

        monthly_tax = purchase_price * property_tax_pct / 100 / 12
        monthly_insurance = annual_insurance / 12
        monthly_maintenance = monthly_rent * maintenance_pct / 100
        monthly_vacancy = monthly_rent * vacancy_pct / 100
        operating_costs = (
            monthly_tax + monthly_insurance + monthly_maintenance + monthly_vacancy
        )
        estimated_cash_flow = monthly_rent - monthly_pi - operating_costs

        annual_noi = (monthly_rent - operating_costs) * 12
        cap_rate = annual_noi / purchase_price * 100 if purchase_price else None

        metric_left, metric_right = st.columns(2)
        metric_left.metric("Down payment", money(down_payment))
        metric_right.metric("Principal and interest", money(monthly_pi))
        metric_left.metric("Monthly operating costs", money(operating_costs))
        metric_right.metric("Estimated monthly cash flow", money(estimated_cash_flow))
        st.metric("Estimated cap rate", percent(cap_rate))

    if monthly_rent == 0:
        st.info("Enter a monthly rent assumption to evaluate cash flow and cap rate.")

    st.caption(
        "Operating costs shown include assumed tax, insurance, maintenance, and vacancy only. "
        "They exclude closing costs, HOA fees, utilities, management fees, capital expenditures, and income taxes."
    )


def render_real_estate_dashboard() -> None:
    """Render the existing real-estate opportunity analysis section."""
    st.header("Houston Real Estate Opportunities")
    st.caption(
        "Current active RentCast listings stored and audited in Oracle Autonomous Database."
    )

    try:
        listings, latest_run = load_real_estate_dashboard_data()
    except (ConfigurationError, Exception):
        st.error(
            "Unable to load real-estate data. Verify the database settings in .env "
            "and confirm that the canonical real-estate migration has run."
        )
        return

    if listings.empty:
        st.info("No current listings are available yet. Run `python etl_realestate.py` and refresh.")
        return

    property_types = sorted(listings["property_type"].dropna().unique().tolist())
    filter_left, filter_right = st.columns(2)

    with filter_left:
        selected_types = st.multiselect(
            "Property types",
            options=property_types,
            default=property_types,
        )

    min_price = int(listings["asking_price"].min())
    max_price = int(listings["asking_price"].max())
    with filter_right:
        if min_price == max_price:
            selected_price_range = (min_price, max_price)
            st.caption(f"All current listings have the same asking price: {money(min_price)}.")
        else:
            selected_price_range = st.slider(
                "Asking-price range",
                min_value=min_price,
                max_value=max_price,
                value=(min_price, max_price),
                step=5_000,
            )

    filtered = listings[
        listings["property_type"].isin(selected_types)
        & listings["asking_price"].between(*selected_price_range)
    ].copy()
    filtered = filtered.sort_values(
        by=["gross_yield_pct", "asking_price"],
        ascending=[False, True],
        na_position="last",
    )

    if filtered.empty:
        st.warning("No listings match the current filters. Adjust the filters above.")
        return

    rent_coverage_count = int(filtered["est_monthly_rent"].notna().sum())
    median_yield = filtered["gross_yield_pct"].median()
    newest_observation = filtered["observed_at"].max()

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)
    metric_1.metric("Listings shown", f"{len(filtered):,}")
    metric_2.metric("Median asking price", money(filtered["asking_price"].median()))
    metric_3.metric("Listings with rent estimate", f"{rent_coverage_count:,}")
    metric_4.metric("Median gross yield", percent(median_yield))

    if not latest_run.empty:
        run = latest_run.iloc[0]
        completed = pd.to_datetime(run["completed_at"], errors="coerce")
        completed_text = (
            completed.strftime("%Y-%m-%d %H:%M UTC")
            if pd.notna(completed)
            else "not completed"
        )
        loaded_count = int(run["loaded_count"]) if pd.notna(run["loaded_count"]) else 0
        st.caption(
            f"Latest ETL run: #{int(run['run_id'])} • {run['run_status']} • "
            f"{loaded_count} listings loaded • completed {completed_text}"
        )
    elif pd.notna(newest_observation):
        st.caption(
            f"Latest observed listing timestamp: {newest_observation:%Y-%m-%d %H:%M UTC}"
        )

    with st.expander("How to interpret these real-estate metrics"):
        st.markdown(
            """
            - **Asking price** is the seller's list price, not an appraisal or final sale price.
            - **Gross yield** is `(estimated monthly rent × 12) ÷ asking price`. It excludes operating and financing costs.
            - **Listings with rent estimate** identifies rows with source rent data. A blank rent is unknown, not zero.
            - Use the scenario calculator for a first-pass underwriting exercise, then verify each assumption independently.
            """
        )

    st.subheader("Current listings")
    table_columns = [
        "street_address",
        "city",
        "property_type",
        "bedrooms",
        "bathrooms",
        "structure_sqft",
        "asking_price",
        "est_monthly_rent",
        "gross_yield_pct",
        "observed_at",
    ]
    table = filtered[table_columns].rename(
        columns={
            "street_address": "Address",
            "city": "City",
            "property_type": "Type",
            "bedrooms": "Beds",
            "bathrooms": "Baths",
            "structure_sqft": "Square feet",
            "asking_price": "Asking price",
            "est_monthly_rent": "Monthly rent",
            "gross_yield_pct": "Gross yield",
            "observed_at": "Observed at",
        }
    )
    st.dataframe(
        table.style.format(
            {
                "Beds": "{:.1f}",
                "Baths": "{:.1f}",
                "Square feet": "{:,.0f}",
                "Asking price": "${:,.0f}",
                "Monthly rent": "${:,.0f}",
                "Gross yield": "{:.2f}%",
                "Observed at": timestamp_text,
            },
            na_rep="—",
        ),
        hide_index=True,
        use_container_width=True,
    )

    map_frame = filtered.dropna(subset=["latitude", "longitude"]).copy()
    if not map_frame.empty:
        st.subheader("Listing locations")
        st.map(
            map_frame.rename(columns={"latitude": "lat", "longitude": "lon"}),
            latitude="lat",
            longitude="lon",
        )

    st.divider()
    render_scenario_calculator(filtered)


def render_market_watchlist_dashboard() -> None:
    """Render canonical market research with explicit data-quality context."""
    st.header("Market Watchlist")
    st.caption(
        "Historical market research data stored in the canonical Oracle market model. "
        "This is an educational watchlist, not a personal holdings ledger or investment recommendation."
    )

    try:
        watchlist = load_market_watchlist_data()
    except (ConfigurationError, Exception):
        st.error(
            "Unable to load market-watchlist data. Verify the database settings in .env "
            "and confirm that the canonical market migrations have run."
        )
        return

    if watchlist.empty:
        st.info("No canonical market-watchlist securities are available yet.")
        return

        oldest_latest_price_date = watchlist["latest_trade_date"].min()
    newest_latest_price_date = watchlist["latest_trade_date"].max()

    # Freshness is governed by the stalest security, not the most recently
    # refreshed ticker in the watchlist.
    price_age_days = market_price_age_days(oldest_latest_price_date)

    securities_with_prices = int(
        (watchlist["price_history_status"] == "PRICE_AVAILABLE").sum()
    )
    securities_with_fundamentals = int(
        (watchlist["fundamentals_status"] != "MISSING_FUNDAMENTALS").sum()
    )
    fundamentals_coverage = securities_with_fundamentals / len(watchlist) * 100

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)
    metric_1.metric("Watchlist securities", f"{len(watchlist):,}")
    metric_2.metric("Securities with price history", f"{securities_with_prices:,}")
    metric_3.metric(
        "Oldest latest price date",
        timestamp_text(oldest_latest_price_date),
    )
    metric_4.metric("Fundamentals coverage", f"{fundamentals_coverage:.2f}%")

    st.caption(
        "Per-security latest-price dates range from "
        f"{timestamp_text(oldest_latest_price_date)} through "
        f"{timestamp_text(newest_latest_price_date)}."
    )

    if price_age_days is None:
        st.error(
            "No latest market-price date is available. Do not interpret the "
            "watchlist as current market data."
        )
    elif price_age_days > 7:
        st.error(
            f"The oldest latest market price is {price_age_days} calendar days old. "
            "The market data-quality gate currently fails its 7-day freshness "
            "threshold, so do not treat displayed prices as live or use them for "
            "time-sensitive decisions."
        )
    else:
        st.info(
            f"The oldest latest stored market price is {price_age_days} calendar "
            "day(s) old. They are historical reference data, not real-time quotes."
        )

    if fundamentals_coverage < 25:
        st.warning(
            f"Only {securities_with_fundamentals} of {len(watchlist)} securities have "
            "fundamentals. Missing values are unknown; they are not zero or unfavorable values."
        )

    if (watchlist["fundamentals_status"] == "AVAILABLE_AS_OF_UNKNOWN").any():
        st.warning(
            "Available fundamentals do not have a verified provider as-of date. Treat them "
            "as legacy historical context rather than current company financials."
        )

    with st.expander("How to interpret this market watchlist"):
        st.markdown(
            """
            - **Latest close price** is the newest historical close stored in your database; it is not a live quote.
            - **P/E, PEG, P/B, return on equity, and profit margin** are research metrics, not a recommendation or valuation model.
            - **Missing fundamentals** means the database has no imported value for that security. It does not imply poor quality.
            - **Free cash flow is intentionally not shown as currency** because the legacy source did not document its unit.
            - This watchlist does not represent stocks you own. Personal holdings and cost basis will be added later through a separate portfolio-ledger model.
            """
        )

    filter_left, filter_right = st.columns(2)
    display_frame = watchlist.copy()
    display_frame["sector_display"] = display_frame["sector"].fillna("Unclassified")
    sectors = sorted(display_frame["sector_display"].unique().tolist())

    with filter_left:
        selected_sectors = st.multiselect(
            "Sectors",
            options=sectors,
            default=sectors,
            key="market_sectors",
        )
    with filter_right:
        fundamentals_only = st.checkbox(
            "Show only securities with fundamentals",
            value=False,
            key="market_fundamentals_only",
        )

    filtered = display_frame[display_frame["sector_display"].isin(selected_sectors)].copy()
    if fundamentals_only:
        filtered = filtered[
            filtered["fundamentals_status"] != "MISSING_FUNDAMENTALS"
        ].copy()

    if filtered.empty:
        st.warning("No securities match the selected watchlist filters.")
        return

    st.subheader("Watchlist comparison")
    comparison = filtered[
        [
            "ticker",
            "sector_display",
            "latest_close_price",
            "latest_trade_date",
            "latest_volume",
            "pe_ratio",
            "peg_ratio",
            "pb_ratio",
            "earnings_per_share",
            "return_on_equity_pct",
            "profit_margin_pct",
            "debt_to_equity_ratio",
            "fundamentals_status",
        ]
    ].rename(
        columns={
            "ticker": "Ticker",
            "sector_display": "Sector",
            "latest_close_price": "Latest close price",
            "latest_trade_date": "Latest price date",
            "latest_volume": "Latest volume",
            "pe_ratio": "P/E ratio",
            "peg_ratio": "PEG ratio",
            "pb_ratio": "P/B ratio",
            "earnings_per_share": "EPS",
            "return_on_equity_pct": "Return on equity",
            "profit_margin_pct": "Profit margin",
            "debt_to_equity_ratio": "Debt-to-equity",
            "fundamentals_status": "Fundamentals status",
        }
    )
    comparison = comparison.sort_values(by=["Sector", "Ticker"])

    st.dataframe(
        comparison.style.format(
            {
                "Latest close price": "${:,.2f}",
                "Latest price date": timestamp_text,
                "Latest volume": "{:,.0f}",
                "P/E ratio": "{:.2f}",
                "PEG ratio": "{:.2f}",
                "P/B ratio": "{:.2f}",
                "EPS": "{:.2f}",
                "Return on equity": "{:.2f}%",
                "Profit margin": "{:.2f}%",
                "Debt-to-equity": "{:.2f}",
            },
            na_rep="—",
        ),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Historical close-price trend")
    ticker_options = filtered["ticker"].tolist()
    selected_ticker = st.selectbox(
        "Choose a ticker",
        options=ticker_options,
        key="market_price_history_ticker",
    )

    try:
        price_history = load_market_price_history(selected_ticker)
    except (ConfigurationError, Exception):
        st.error("Unable to load the selected ticker's historical price series.")
        return

    chart_frame = price_history.dropna(subset=["trade_date", "close_price"]).copy()
    if chart_frame.empty:
        st.info(f"No canonical price history is available for {selected_ticker}.")
        return

    latest_price = chart_frame.iloc[-1]
    st.caption(
        f"{selected_ticker}: {len(chart_frame):,} stored trading day(s), from "
        f"{timestamp_text(chart_frame['trade_date'].min())} through "
        f"{timestamp_text(chart_frame['trade_date'].max())}."
    )
    st.metric(
        f"Latest stored {selected_ticker} close",
        money(latest_price["close_price"]),
        help="This is the latest close stored in Oracle, not a live market quote.",
    )
    st.line_chart(
        chart_frame.set_index("trade_date")[["close_price"]].rename(
            columns={"close_price": "Close price"}
        )
    )


def main() -> None:
    """Configure and render the two canonical investment-research sections."""
    st.set_page_config(layout="wide", page_title="Personal Investment Dashboard")
    st.title("Personal Investment Dashboard")
    st.caption(
        "A personal research dashboard for Houston real estate and market watchlists. "
        "It is not financial, tax, legal, or investment advice."
    )

    real_estate_tab, market_tab = st.tabs(
        ["🏠 Real Estate Opportunities", "📈 Market Watchlist"]
    )

    with real_estate_tab:
        render_real_estate_dashboard()

    with market_tab:
        render_market_watchlist_dashboard()


if __name__ == "__main__":
    main()
