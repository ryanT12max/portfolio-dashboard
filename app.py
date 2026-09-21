"""Dashboard for the canonical Houston real-estate investment-analysis view."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from portfolio_dashboard.config import ConfigurationError
from portfolio_dashboard.database import managed_connection


CURRENT_ANALYSIS_SQL = """
    SELECT
        property_key,
        street_address,
        city,
        state_code,
        zip_code,
        property_type,
        bedrooms,
        bathrooms,
        structure_sqft,
        latitude,
        longitude,
        listing_observed_at,
        listing_status,
        asking_price,
        rent_estimate_snapshot_id,
        rent_estimate_observed_at,
        estimated_monthly_rent,
        rent_range_low,
        rent_range_high,
        comparable_count,
        rent_estimate_status,
        investment_analysis_status,
        estimated_gross_yield_pct,
        estimated_gross_yield_low_pct,
        estimated_gross_yield_high_pct
    FROM vw_re_current_investment_analysis
    WHERE listing_status = 'Active' OR listing_status IS NULL
    ORDER BY listing_observed_at DESC, asking_price ASC
"""

LATEST_LISTING_RUN_SQL = """
    SELECT run_id, completed_at, run_status, loaded_count
    FROM re_etl_run
    WHERE source_name = 'RENTCAST'
    ORDER BY run_id DESC
    FETCH FIRST 1 ROW ONLY
"""

LATEST_RENT_RUN_SQL = """
    SELECT run_id, completed_at, run_status, loaded_count
    FROM re_etl_run
    WHERE source_name = 'RENTCAST_RENT_AVM'
    ORDER BY run_id DESC
    FETCH FIRST 1 ROW ONLY
"""


@st.cache_data(ttl=300, show_spinner=False)
def load_dashboard_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read reporting data and separate audit records for the two pipelines."""
    with managed_connection() as connection:
        listings = pd.read_sql_query(CURRENT_ANALYSIS_SQL, con=connection)
        listing_run = pd.read_sql_query(LATEST_LISTING_RUN_SQL, con=connection)
        rent_run = pd.read_sql_query(LATEST_RENT_RUN_SQL, con=connection)

    for frame in (listings, listing_run, rent_run):
        frame.columns = [column.lower() for column in frame.columns]

    for column in ("listing_observed_at", "rent_estimate_observed_at"):
        listings[column] = pd.to_datetime(listings[column], errors="coerce")

    for frame in (listing_run, rent_run):
        if not frame.empty:
            frame["completed_at"] = pd.to_datetime(
                frame["completed_at"], errors="coerce"
            )

    return listings, listing_run, rent_run


def money(value: float | int | None) -> str:
    """Format an optional value as whole dollars."""
    if value is None or pd.isna(value):
        return "—"
    return "$" + f"{value:,.0f}"


def percent(value: float | int | None) -> str:
    """Format an optional percentage."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.2f}%"


def money_range(low: float | int | None, high: float | int | None) -> str:
    """Format an optional dollar range."""
    if low is None or high is None or pd.isna(low) or pd.isna(high):
        return "—"
    return f"{money(low)}–{money(high)}"


def percent_range(low: float | int | None, high: float | int | None) -> str:
    """Format an optional percentage range."""
    if low is None or high is None or pd.isna(low) or pd.isna(high):
        return "—"
    return f"{percent(low)}–{percent(high)}"


def mortgage_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """Return fixed-rate monthly principal and interest."""
    if principal <= 0:
        return 0.0

    monthly_rate = annual_rate_pct / 100 / 12
    payment_count = term_years * 12

    if monthly_rate == 0:
        return principal / payment_count

    return principal * (
        monthly_rate * (1 + monthly_rate) ** payment_count
    ) / ((1 + monthly_rate) ** payment_count - 1)


def run_caption(label: str, frame: pd.DataFrame, noun: str) -> str:
    """Describe one ETL run without mislabeling it as the other pipeline."""
    if frame.empty:
        return f"{label}: no completed run is available yet."

    run = frame.iloc[0]
    completed = run["completed_at"]
    completed_text = (
        completed.strftime("%Y-%m-%d %H:%M UTC")
        if pd.notna(completed)
        else "not completed"
    )
    loaded = int(run["loaded_count"]) if pd.notna(run["loaded_count"]) else 0

    return (
        f"{label}: run #{int(run['run_id'])} • {run['run_status']} • "
        f"{loaded:,} {noun} loaded • completed {completed_text}"
    )


def render_scenario_calculator(listings: pd.DataFrame) -> None:
    """Show an editable, first-pass cash-flow scenario."""
    st.subheader("Investment Scenario Calculator")
    st.caption(
        "Planning only—not an appraisal, lending quote, or investment recommendation."
    )

    lookup = listings.set_index("property_key")
    selected_key = st.selectbox(
        "Choose a listing",
        listings["property_key"].tolist(),
        format_func=lambda key: (
            f"{lookup.loc[key, 'street_address']} "
            f"({money(lookup.loc[key, 'asking_price'])})"
        ),
    )
    selected = lookup.loc[selected_key]

    has_estimate = selected["rent_estimate_status"] == "ESTIMATED"
    suggested_rent = selected["estimated_monthly_rent"]
    rent_default = float(suggested_rent) if pd.notna(suggested_rent) else 0.0

    if has_estimate:
        comparable_count = (
            int(selected["comparable_count"])
            if pd.notna(selected["comparable_count"])
            else 0
        )
        st.info(
            "Provider rent estimate: "
            f"{money(suggested_rent)} per month "
            f"(range {money_range(selected['rent_range_low'], selected['rent_range_high'])}; "
            f"{comparable_count} comparables). Review it before using it."
        )
    else:
        st.warning(
            "No provider rent estimate is available. Enter a researched manual "
            "rent assumption; missing rent is never treated as zero."
        )

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
            help="Starts with the provider estimate when one is available.",
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
        monthly_hoa = st.number_input(
            "Monthly HOA assumption",
            min_value=0.0,
            value=0.0,
            step=25.0,
        )
        maintenance_pct = st.slider("Maintenance reserve (% of rent)", 0, 30, 8, 1)
        vacancy_pct = st.slider("Vacancy reserve (% of rent)", 0, 30, 5, 1)

        down_payment = purchase_price * down_payment_pct / 100
        principal = purchase_price - down_payment
        monthly_pi = mortgage_payment(principal, interest_rate_pct, loan_term_years)
        monthly_tax = purchase_price * property_tax_pct / 100 / 12
        monthly_insurance = annual_insurance / 12
        monthly_maintenance = monthly_rent * maintenance_pct / 100
        monthly_vacancy = monthly_rent * vacancy_pct / 100

        operating_costs = (
            monthly_tax
            + monthly_insurance
            + monthly_hoa
            + monthly_maintenance
            + monthly_vacancy
        )
        monthly_cash_flow = monthly_rent - monthly_pi - operating_costs
        annual_noi = (monthly_rent - operating_costs) * 12
        cap_rate = annual_noi / purchase_price * 100 if purchase_price else None

        left, right = st.columns(2)
        left.metric("Down payment", money(down_payment))
        right.metric("Principal and interest", money(monthly_pi))
        left.metric("Monthly operating costs", money(operating_costs))
        right.metric("Estimated monthly cash flow", money(monthly_cash_flow))
        st.metric("Estimated cap rate", percent(cap_rate))

    if monthly_rent == 0:
        st.info("Enter a monthly rent assumption to calculate cash flow and cap rate.")

    st.caption(
        "Operating costs include assumed tax, insurance, HOA, maintenance, and vacancy. "
        "They exclude closing costs, utilities, management fees, capital expenditures, "
        "income taxes, and major repairs."
    )


def main() -> None:
    st.set_page_config(layout="wide", page_title="Houston Real Estate Dashboard")
    st.title("Houston Real Estate Investment Dashboard")
    st.caption(
        "Current active listings and historical RentCast rent estimates, stored and "
        "audited in Oracle Autonomous Database."
    )

    if st.sidebar.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

    try:
        listings, listing_run, rent_run = load_dashboard_data()
    except ConfigurationError as error:
        st.error(f"Configuration error: {error}")
        st.stop()
    except Exception:
        st.error(
            "Unable to load investment-analysis data. Confirm the canonical migrations "
            "have run and that the database settings are valid."
        )
        st.stop()

    if listings.empty:
        st.info("No current listings are available. Run the real-estate ETL and refresh.")
        st.stop()

    st.sidebar.header("Listing filters")

    property_types = sorted(listings["property_type"].dropna().unique().tolist())
    selected_types = st.sidebar.multiselect(
        "Property types",
        options=property_types,
        default=property_types,
    )

    labels = {
        "ESTIMATED": "Rent estimate available",
        "MISSING": "Needs rent estimate",
    }
    selected_statuses = st.sidebar.multiselect(
        "Rent-estimate availability",
        options=list(labels),
        default=list(labels),
        format_func=lambda status: labels[status],
    )

    min_price = int(listings["asking_price"].min())
    max_price = int(listings["asking_price"].max())
    price_range = st.sidebar.slider(
        "Asking-price range",
        min_value=min_price,
        max_value=max_price,
        value=(min_price, max_price),
        step=5_000,
    )

    filtered = listings[
        listings["property_type"].isin(selected_types)
        & listings["rent_estimate_status"].isin(selected_statuses)
        & listings["asking_price"].between(*price_range)
    ].copy()
    filtered = filtered.sort_values(
        by=["estimated_gross_yield_pct", "asking_price"],
        ascending=[False, True],
        na_position="last",
    )

    if filtered.empty:
        st.warning("No listings match the current filters. Adjust the sidebar filters.")
        st.stop()

    estimate_count = int((filtered["rent_estimate_status"] == "ESTIMATED").sum())
    coverage_pct = estimate_count / len(filtered) * 100
    median_yield = filtered.loc[
        filtered["rent_estimate_status"] == "ESTIMATED",
        "estimated_gross_yield_pct",
    ].median()

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)
    metric_1.metric("Listings shown", f"{len(filtered):,}")
    metric_2.metric("Median asking price", money(filtered["asking_price"].median()))
    metric_3.metric("Rent-estimate coverage", percent(coverage_pct))
    metric_4.metric("Median estimated gross yield", percent(median_yield))

    st.caption(run_caption("Latest listing ETL", listing_run, "listings"))
    st.caption(run_caption("Latest rent-estimate ETL", rent_run, "estimates"))

    missing_count = len(filtered) - estimate_count
    if missing_count:
        st.info(
            f"{missing_count:,} shown listing(s) still need a rent estimate. They remain "
            "available for manual scenario analysis."
        )

    with st.expander("How to interpret this dashboard"):
        st.markdown(
            """
            - **Rent estimate available** means a validated RentCast long-term rent
              estimate is stored for the property. It is not guaranteed income.
            - **Needs rent estimate** means no estimate has been collected. Blank rent
              and yield values mean *unknown*, not zero.
            - **Estimated gross yield** is annual estimated rent divided by asking
              price. It excludes taxes, insurance, HOA, maintenance, vacancy,
              financing, and closing costs.
            - **Rent and yield ranges** show the provider's lower and upper estimate
              bounds. A wide range warrants more property-level research.
            """
        )

    st.subheader("Current listings")
    table = filtered.assign(
        rent_estimate_availability=filtered["rent_estimate_status"].map(labels),
        rent_estimate_range=filtered.apply(
            lambda row: money_range(row["rent_range_low"], row["rent_range_high"]),
            axis=1,
        ),
        estimated_yield_range=filtered.apply(
            lambda row: percent_range(
                row["estimated_gross_yield_low_pct"],
                row["estimated_gross_yield_high_pct"],
            ),
            axis=1,
        ),
    )[
        [
            "street_address",
            "city",
            "property_type",
            "bedrooms",
            "bathrooms",
            "structure_sqft",
            "asking_price",
            "rent_estimate_availability",
            "estimated_monthly_rent",
            "rent_estimate_range",
            "comparable_count",
            "estimated_gross_yield_pct",
            "estimated_yield_range",
            "listing_observed_at",
        ]
    ].rename(
        columns={
            "street_address": "Address",
            "city": "City",
            "property_type": "Type",
            "bedrooms": "Beds",
            "bathrooms": "Baths",
            "structure_sqft": "Square feet",
            "asking_price": "Asking price",
            "rent_estimate_availability": "Rent estimate",
            "estimated_monthly_rent": "Estimated monthly rent",
            "rent_estimate_range": "Rent range",
            "comparable_count": "Comparables",
            "estimated_gross_yield_pct": "Estimated gross yield",
            "estimated_yield_range": "Yield range",
            "listing_observed_at": "Listing observed at",
        }
    )

    st.dataframe(
        table.style.format(
            {
                "Beds": "{:.1f}",
                "Baths": "{:.1f}",
                "Square feet": "{:,.0f}",
                "Asking price": money,
                "Estimated monthly rent": money,
                "Comparables": "{:,.0f}",
                "Estimated gross yield": percent,
                "Listing observed at": lambda value: (
                    value.strftime("%Y-%m-%d %H:%M") if pd.notna(value) else "—"
                ),
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