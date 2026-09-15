"""Streamlit dashboard for the canonical Houston real-estate dataset.

The dashboard is deliberately real-estate-first. It reads only the
PORTFOLIO_APP canonical model through VW_RE_CURRENT_LISTING and never stores
credentials in source code.
"""

from __future__ import annotations

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

LATEST_RUN_SQL = """
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


@st.cache_data(ttl=300, show_spinner=False)
def load_dashboard_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load current listings and the latest ETL audit record."""
    with managed_connection() as connection:
        listings = pd.read_sql_query(CURRENT_LISTINGS_SQL, con=connection)
        latest_run = pd.read_sql_query(LATEST_RUN_SQL, con=connection)

    listings.columns = [column.lower() for column in listings.columns]
    latest_run.columns = [column.lower() for column in latest_run.columns]

    for column in ("observed_at", "listing_date", "last_seen_date"):
        if column in listings:
            listings[column] = pd.to_datetime(listings[column], errors="coerce")

    return listings, latest_run


def money(value: float | int | None) -> str:
    """Format an optional numeric value for a dashboard metric."""
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.0f}"


def percent(value: float | int | None) -> str:
    """Format an optional percentage for a dashboard metric."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.2f}%"


def mortgage_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """Calculate principal-and-interest payment for a fixed-rate loan."""
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


def main() -> None:
    st.set_page_config(layout="wide", page_title="Houston Real Estate Dashboard")
    st.title("Houston Real Estate Investment Dashboard")
    st.caption(
        "Current active listings from RentCast, stored and audited in Oracle Autonomous Database."
    )

    try:
        listings, latest_run = load_dashboard_data()
    except (ConfigurationError, Exception):
        st.error(
            "Unable to load real-estate data. Verify the database settings in .env "
            "and confirm that the canonical real-estate migration has run."
        )
        st.stop()

    if listings.empty:
        st.info("No current listings are available yet. Run `python etl_realestate.py` and refresh.")
        st.stop()

    st.sidebar.header("Listing filters")
    property_types = sorted(listings["property_type"].dropna().unique().tolist())
    selected_types = st.sidebar.multiselect(
        "Property types",
        options=property_types,
        default=property_types,
    )

    min_price = int(listings["asking_price"].min())
    max_price = int(listings["asking_price"].max())
    selected_price_range = st.sidebar.slider(
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
        st.warning("No listings match the current filters. Adjust the sidebar filters.")
        st.stop()

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
        completed_text = completed.strftime("%Y-%m-%d %H:%M UTC") if pd.notna(completed) else "not completed"
        st.caption(
            f"Latest ETL run: #{int(run['run_id'])} • {run['run_status']} • "
            f"{int(run['loaded_count'])} listings loaded • completed {completed_text}"
        )
    elif pd.notna(newest_observation):
        st.caption(f"Latest observed listing timestamp: {newest_observation:%Y-%m-%d %H:%M UTC}")

    with st.expander("How to interpret these metrics"):
        st.markdown(
            """
            - **Asking price** is the seller's current list price, not an appraisal or final sale price.
            - **Gross yield** is `(estimated monthly rent × 12) ÷ asking price`. It does not include taxes, insurance, maintenance, vacancies, financing, or closing costs.
            - **Listings with rent estimate** tells you how many rows have enough source data for a gross-yield calculation. A blank rent is unknown, not zero.
            - Use the scenario calculator for a more realistic first-pass cash-flow estimate, then verify each assumption independently.
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
                "Observed at": lambda value: value.strftime("%Y-%m-%d %H:%M") if pd.notna(value) else "—",
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


if __name__ == "__main__":
    main()
