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
        listing_observed_at AS observed_at,
        listing_date,
        last_seen_date,
        listing_status,
        asking_price,
        estimated_monthly_rent AS est_monthly_rent,
        estimate_scope,
        rent_subject_attribute_mode,
        rent_estimate_status,
        gross_yield_eligible,
        investment_analysis_status,
        estimated_gross_yield_pct AS gross_yield_pct
    FROM vw_re_current_investment_analysis
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
        watchlist.security_key,
        watchlist.ticker,
        watchlist.sector,
        quote.quote_snapshot_id,
        quote.current_price AS current_quote_price,
        quote.prior_close_price AS quote_prior_close_price,
        quote.change_amount AS quote_change_amount,
        quote.change_pct AS quote_change_pct,
        quote.day_open_price AS quote_day_open_price,
        quote.day_high_price AS quote_day_high_price,
        quote.day_low_price AS quote_day_low_price,
        quote.provider_quote_at,
        quote.observed_at AS quote_observed_at,
        quote.source_name AS quote_source_name,
        watchlist.latest_trade_date,
        watchlist.latest_close_price,
        watchlist.latest_volume,
        watchlist.latest_price_source,
        watchlist.latest_price_observed_at,
        watchlist.fundamental_snapshot_id,
        watchlist.fundamentals_source,
        watchlist.fundamentals_source_as_of_date,
        watchlist.fundamentals_observed_at,
        watchlist.fundamental_market_price,
        watchlist.pe_ratio,
        watchlist.peg_ratio,
        watchlist.pb_ratio,
        watchlist.earnings_per_share,
        watchlist.return_on_equity_pct,
        watchlist.profit_margin_pct,
        watchlist.debt_to_equity_ratio,
        watchlist.free_cash_flow_value,
        watchlist.free_cash_flow_unit,
        watchlist.price_history_status,
        watchlist.fundamentals_status
    FROM vw_mk_watchlist_analysis watchlist
    LEFT JOIN vw_mk_current_quote quote
        ON quote.security_key = watchlist.security_key
    ORDER BY watchlist.ticker
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
        "provider_quote_at",
        "quote_observed_at",
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


def timestamp_with_time_text(value: object) -> str:
    """Format an optional timestamp without asserting a provider timezone."""
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return "—"
    return timestamp.strftime("%Y-%m-%d %H:%M")


def market_price_age_days(latest_trade_date: object) -> int | None:
    """Return calendar-day age for the most recent stored market price."""
    timestamp = pd.to_datetime(latest_trade_date, errors="coerce")
    if pd.isna(timestamp):
        return None
    return (datetime.now(timezone.utc).date() - timestamp.date()).days


def market_quote_age_hours(
    oldest_quote_observed_at: object,
    now: datetime | None = None,
) -> float | None:
    """Return elapsed UTC hours since the oldest current quote observation."""
    timestamp = pd.to_datetime(oldest_quote_observed_at, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return None

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    return (now - timestamp.to_pydatetime()).total_seconds() / 3600


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


MAP_DEFAULT_COLOR = [33, 113, 181, 150]
MAP_SELECTED_COLOR = [230, 126, 34, 255]
MAP_DEFAULT_RADIUS_METERS = 180
MAP_SELECTED_RADIUS_METERS = 360

FUNDAMENTAL_METRIC_GROUPS = {
    "Valuation": (
        ("P/E ratio", "pe_ratio", "number"),
        ("PEG ratio", "peg_ratio", "number"),
        ("P/B ratio", "pb_ratio", "number"),
    ),
    "Profitability & efficiency": (
        ("EPS", "earnings_per_share", "number"),
        ("Return on equity", "return_on_equity_pct", "percent"),
        ("Profit margin", "profit_margin_pct", "percent"),
    ),
    "Financial health & solvency": (
        ("Debt-to-equity", "debt_to_equity_ratio", "number"),
        ("Free cash flow", "free_cash_flow_value", "number"),
    ),
}


def selected_property_key_from_rows(
    table: pd.DataFrame,
    selected_rows: list[int],
) -> int | None:
    """Return one selected property key while safely handling a stale table event."""
    if not selected_rows:
        return None

    selected_index = selected_rows[0]
    if selected_index < 0 or selected_index >= len(table):
        return None

    property_key = table.iloc[selected_index].get("Property key")
    if property_key is None or pd.isna(property_key):
        return None
    return int(property_key)


def build_listing_map_frame(
    listings: pd.DataFrame,
    selected_property_key: int | None,
) -> pd.DataFrame:
    """Build a clean map frame with one distinct marker for a selected listing."""
    map_frame = listings.dropna(subset=["latitude", "longitude"]).copy()
    if map_frame.empty:
        return map_frame

    map_frame["marker_color"] = map_frame["property_key"].apply(
        lambda property_key: (
            MAP_SELECTED_COLOR
            if selected_property_key == int(property_key)
            else MAP_DEFAULT_COLOR
        )
    )
    map_frame["marker_radius"] = map_frame["property_key"].apply(
        lambda property_key: (
            MAP_SELECTED_RADIUS_METERS
            if selected_property_key == int(property_key)
            else MAP_DEFAULT_RADIUS_METERS
        )
    )
    return map_frame


def render_listing_map(
    listings: pd.DataFrame,
    selected_property_key: int | None,
) -> None:
    """Render location markers and focus the selected table row when available."""
    map_frame = build_listing_map_frame(listings, selected_property_key)
    if map_frame.empty:
        return

    try:
        import pydeck as pdk
    except ImportError:
        st.warning("Interactive map highlighting is unavailable in this environment.")
        return

    selected_row = map_frame[map_frame["property_key"] == selected_property_key]
    if selected_row.empty:
        center_latitude = float(map_frame["latitude"].mean())
        center_longitude = float(map_frame["longitude"].mean())
        zoom = 10.5
    else:
        center_latitude = float(selected_row.iloc[0]["latitude"])
        center_longitude = float(selected_row.iloc[0]["longitude"])
        zoom = 14

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_frame,
        get_position="[longitude, latitude]",
        get_fill_color="marker_color",
        get_radius="marker_radius",
        pickable=True,
        stroked=True,
        get_line_color=[255, 255, 255, 220],
        line_width_min_pixels=1,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(
            latitude=center_latitude,
            longitude=center_longitude,
            zoom=zoom,
            pitch=0,
        ),
        tooltip={
            "html": (
                "<b>{street_address}</b><br/>"
                "Asking price: ${asking_price}<br/>"
                "Type: {property_type}"
            ),
            "style": {"backgroundColor": "#1f2937", "color": "white"},
        },
    )
    st.pydeck_chart(deck, use_container_width=True)


def build_fundamentals_explorer_frame(
    watchlist: pd.DataFrame,
    metric_group: str,
    sort_column: str,
    ascending: bool,
    hide_missing_metrics: bool,
    show_lineage: bool,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Create one clean, category-sized fundamentals table for every security."""
    metric_definitions = FUNDAMENTAL_METRIC_GROUPS[metric_group]
    metric_columns = [column for _, column, _ in metric_definitions]
    metric_labels = {column: label for label, column, _ in metric_definitions}

    frame = watchlist.copy()
    frame["sector_display"] = frame["sector"].fillna("Unclassified")
    if hide_missing_metrics:
        frame = frame[frame[metric_columns].notna().any(axis=1)].copy()

    display_columns = ["ticker", "sector_display", *metric_columns]
    rename_columns = {
        "ticker": "Ticker",
        "sector_display": "Sector",
        **metric_labels,
    }

    if metric_group == "Financial health & solvency":
        display_columns.append("free_cash_flow_unit")
        rename_columns["free_cash_flow_unit"] = "FCF unit"

    if show_lineage:
        display_columns.extend(
            [
                "fundamentals_source",
                "fundamentals_source_as_of_date",
                "fundamentals_observed_at",
            ]
        )
        rename_columns.update(
            {
                "fundamentals_source": "Source",
                "fundamentals_source_as_of_date": "Provider as-of date",
                "fundamentals_observed_at": "Retrieved by pipeline",
            }
        )

    display = frame[display_columns].sort_values(
        by=sort_column,
        ascending=ascending,
        na_position="last",
    )
    return display.rename(columns=rename_columns), metric_labels


def render_metric_dictionary() -> None:
    """Render novice-friendly metric definitions without generating buy/sell signals."""
    with st.expander("📖 Fundamental Metric Dictionary"):
        st.info(
            "🟢 means a pattern investors often view favorably after comparing similar "
            "companies. 🔴 means a potential caution or research question. Neither icon "
            "means buy, sell, safe, or unsafe by itself."
        )

        valuation_tab, profitability_tab, health_tab = st.tabs(
            [
                "💲 Valuation",
                "📈 Profitability & efficiency",
                "🛡️ Financial health & solvency",
            ]
        )

        with valuation_tab:
            st.markdown(
                """
                **Price-to-Earnings (P/E) ratio**

                - **What it means:** the share price divided by earnings per share. In plain English, it approximates how much investors are paying for each $1 of a company's earnings.
                - 🟢 **Lower than similar companies or its own history:** can suggest investors are paying less for current earnings.
                - 🔴 **Higher than similar companies or its own history:** can suggest investors are paying more for current earnings and expecting more growth.
                - **How investors use it:** as a valuation comparison within the same industry. A low P/E is not automatically a bargain; weak or falling earnings can also make it low.

                **PEG ratio (Price/Earnings-to-Growth)**

                - **What it means:** a P/E ratio viewed alongside expected earnings growth. It asks whether the price paid for earnings is high or low relative to expected growth.
                - 🟢 **Lower relative to comparable companies:** can suggest the price is lower relative to the growth assumption.
                - 🔴 **Higher relative to comparable companies:** can suggest investors are paying more for each unit of expected growth.
                - **How investors use it:** to add a growth lens to P/E. It depends on growth forecasts, which can be wrong, so it is not a stand-alone buy signal.

                **Price-to-Book (P/B) ratio**

                - **What it means:** the share price divided by book value per share, where book value is accounting equity after liabilities.
                - 🟢 **Lower relative to peers:** can suggest the market price is low compared with recorded net assets.
                - 🔴 **Higher relative to peers:** can suggest investors value the business well above recorded net assets.
                - **How investors use it:** often for asset-heavy businesses. It can be much less useful for companies whose value comes mainly from brands, software, or other intangible assets.
                """
            )

        with profitability_tab:
            st.markdown(
                """
                **Earnings Per Share (EPS)**

                - **What it means:** company profit allocated per common share.
                - 🟢 **Higher or consistently growing EPS:** can indicate improving profits on a per-share basis.
                - 🔴 **Low, negative, or falling EPS:** can indicate losses, slowing profits, or more shares reducing the profit attributable to each share.
                - **How investors use it:** to study earnings trends and calculate valuation measures such as P/E; the direction and quality of earnings matter as much as one number.

                **Return on Equity (ROE)**

                - **What it means:** net income divided by shareholder equity. It shows how much profit a company generates from owners' invested capital.
                - 🟢 **Higher and sustainable compared with peers:** can indicate efficient use of shareholder capital.
                - 🔴 **Low or falling compared with peers:** can indicate weak profitability or inefficient use of equity.
                - **Important caution:** an unusually high ROE can be caused by very low equity or high debt, so investors check debt levels too.

                **Profit Margin**

                - **What it means:** the percentage of revenue that remains as profit after expenses.
                - 🟢 **Higher or stable versus peers:** can suggest stronger pricing power, cost control, or a more profitable business model.
                - 🔴 **Lower or declining versus peers:** can suggest rising costs, competition, or weaker profitability.
                - **How investors use it:** to compare operating performance among companies with similar business models; normal margins differ greatly by industry.
                """
            )

        with health_tab:
            st.markdown(
                """
                **Debt-to-Equity (D/E) ratio**

                - **What it means:** debt compared with shareholder equity. It is a quick view of how much a company relies on borrowing.
                - 🟢 **Lower relative to similar companies:** often means less leverage and potentially less debt-related financial pressure.
                - 🔴 **Higher relative to similar companies:** can mean more leverage and greater sensitivity to interest costs or weaker earnings.
                - **How investors use it:** to assess balance-sheet risk alongside cash flow and interest coverage. Banks, utilities, and REITs can normally carry more debt than other industries.

                **Free Cash Flow (FCF)**

                - **What it means:** cash remaining after a company funds normal operations and the long-term investments needed to run the business.
                - 🟢 **Positive and consistently growing FCF:** can suggest the business is generating cash that may support debt repayment, reinvestment, dividends, or share repurchases.
                - 🔴 **Negative or shrinking FCF:** can be a caution, but it can also reflect a company deliberately investing for future growth.
                - **How investors use it:** to assess cash-generation quality and whether reported profit is turning into cash. Compare trends and size-adjusted measures; raw FCF dollars are not directly comparable across companies of different sizes.
                """
            )

        st.caption(
            "Use these metrics as a research checklist: compare the same industry, review "
            "multi-period trends and company filings, and consider your goals and risk "
            "tolerance. The dashboard does not produce buy or sell recommendations."
        )


def render_fundamentals_explorer(watchlist: pd.DataFrame) -> None:
    """Render every watchlist security through a compact category-level metric lens."""
    st.subheader("📊 Fundamentals Explorer")
    st.caption(
        "Choose one metric group to compare every watchlist security without a wide, "
        "hard-to-read table. Missing values are unknown, not zero."
    )

    metric_group = st.radio(
        "Metric group",
        options=list(FUNDAMENTAL_METRIC_GROUPS),
        horizontal=True,
        key="fundamentals_metric_group",
    )
    metric_definitions = FUNDAMENTAL_METRIC_GROUPS[metric_group]
    metric_labels = [label for label, _, _ in metric_definitions]
    label_to_column = {label: column for label, column, _ in metric_definitions}

    control_left, control_middle, control_right = st.columns(3)
    with control_left:
        sort_label = st.selectbox(
            "Sort by",
            options=metric_labels,
            key="fundamentals_sort_metric",
        )
    with control_middle:
        sort_direction = st.radio(
            "Sort direction",
            options=("Low to high", "High to low"),
            horizontal=True,
            key="fundamentals_sort_direction",
        )
    with control_right:
        hide_missing_metrics = st.checkbox(
            "Hide stocks with no values in this group",
            value=False,
            key="fundamentals_hide_missing",
        )

    show_lineage = st.checkbox(
        "Show data source and dates",
        value=False,
        key="fundamentals_show_lineage",
        help="Shows provider lineage without crowding the default comparison view.",
    )
    explorer, _ = build_fundamentals_explorer_frame(
        watchlist,
        metric_group=metric_group,
        sort_column=label_to_column[sort_label],
        ascending=sort_direction == "Low to high",
        hide_missing_metrics=hide_missing_metrics,
        show_lineage=show_lineage,
    )

    if explorer.empty:
        st.info("No securities have a value for the selected fundamentals filter.")
        return

    format_rules = {
        "P/E ratio": "{:.2f}",
        "PEG ratio": "{:.2f}",
        "P/B ratio": "{:.2f}",
        "EPS": "{:.2f}",
        "Return on equity": "{:.2f}%",
        "Profit margin": "{:.2f}%",
        "Debt-to-equity": "{:.2f}",
        "Free cash flow": "{:.2f}",
        "Provider as-of date": timestamp_text,
        "Retrieved by pipeline": timestamp_with_time_text,
    }
    st.dataframe(
        explorer.style.format(format_rules, na_rep="—"),
        hide_index=True,
        use_container_width=True,
    )
    if metric_group == "Financial health & solvency":
        st.caption(
            "Free cash flow is shown only with the provider's recorded unit. Do not treat "
            "it as dollars or compare it across providers when the unit is unknown."
        )


def render_scenario_calculator(listings: pd.DataFrame) -> None:
    """Render an assumption-driven underwriting scenario for one listing."""
    st.subheader("🧮 Investment Scenario Calculator")
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
    unit_scope_estimate = selected.get("estimate_scope") == "UNIT"
    provider_attribute_lookup = (
        selected.get("rent_subject_attribute_mode") == "PROVIDER_LOOKUP"
    )
    rent_default = (
        float(suggested_rent)
        if pd.notna(suggested_rent) and not unit_scope_estimate
        else 0.0
    )

    assumptions, results = st.columns(2)

    with assumptions:
        st.markdown("#### 🧾 Assumptions")
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
            help=(
                "Property-level RentCast estimates are used as a starting point. "
                "Enter your own whole-property assumption when needed."
            ),
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
        st.markdown("#### 📊 Scenario results")
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

    if unit_scope_estimate:
        st.warning(
            "RentCast returned a single-unit estimate for this Multi-Family listing. "
            "It was not prefilled because it is not whole-building income. Enter a "
            "separately verified whole-property rent assumption before using this calculator."
        )
    if provider_attribute_lookup:
        st.info(
            "RentCast looked up the subject-property attributes from the address because "
            "this listing is missing one or more internal attributes. Verify the provider "
            "attributes before relying on this estimate."
        )
    elif not unit_scope_estimate and monthly_rent == 0:
        st.info("Enter a monthly rent assumption to evaluate cash flow and cap rate.")

    st.caption(
        "Operating costs shown include assumed tax, insurance, maintenance, and vacancy only. "
        "They exclude closing costs, HOA fees, utilities, management fees, capital expenditures, and income taxes."
    )


def render_real_estate_dashboard() -> None:
    """Render the existing real-estate opportunity analysis section."""
    st.header("🏠 Houston Real Estate Opportunities")
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

    property_scope_count = int((filtered["estimate_scope"] == "PROPERTY").sum())
    unit_scope_count = int((filtered["estimate_scope"] == "UNIT").sum())
    gross_yield_eligible_count = int(
        (filtered["gross_yield_eligible"] == "Y").sum()
    )
    median_yield = filtered["gross_yield_pct"].median()
    newest_observation = filtered["observed_at"].max()

    metric_1, metric_2, metric_3 = st.columns(3)
    metric_1.metric("📋 Listings shown", f"{len(filtered):,}")
    metric_2.metric("💰 Median asking price", money(filtered["asking_price"].median()))
    metric_3.metric(
        "🏡 Gross-yield eligible",
        f"{gross_yield_eligible_count:,}/{len(filtered):,}",
        help="Only PROPERTY-scope rent estimates can support whole-property gross yield.",
    )

    st.caption(
        f"Rent estimate scope: {property_scope_count:,} whole-property estimate(s), "
        f"{unit_scope_count:,} single-unit estimate(s). Median gross yield "
        f"({percent(median_yield)}) uses whole-property estimates only."
    )

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
            - **Single-unit rent estimate** is RentCast's value for one unit in a Multi-Family listing. It is displayed for research, but it is never used as whole-building income or gross yield.

            #### 💵 Cost and financing assumptions

            - **Property-tax assumption** is a yearly percentage used for a quick estimate: `purchase price × tax percentage`. The dashboard divides that annual estimate by 12 for the monthly budget. For a real property, start with the county tax record or a tax professional; the assessed value and your exemptions may differ from the purchase price.
            - **Annual insurance assumption** is your estimated total property-insurance cost for one year. Enter a lender or insurer quote when possible; the dashboard divides it by 12 to budget a monthly amount.
            - **Annual interest rate** is the loan interest rate quoted by a lender. The dashboard converts it to a monthly rate when calculating the loan payment. It is an input, not a rate calculated from the listing, and it can change with credit, down payment, loan type, and market conditions.
            - **Maintenance reserve** is money set aside each month for expected repairs and replacements, such as plumbing fixes, appliances, or paint. The dashboard estimates it as a percentage of monthly rent; it is a planned budget, not a bill that has already happened.
            - **Vacancy reserve** is money set aside for periods when the property earns no rent, such as tenant turnover or marketing time. The dashboard estimates it as a percentage of monthly rent.

            #### 📈 Results shown by the scenario calculator

            - **Principal and interest** is the monthly loan payment that pays down the amount borrowed (**principal**) and the lender's charge for borrowing it (**interest**). Property tax and insurance are calculated separately in this dashboard.
            - **Estimated cap rate** is the property's estimated annual operating income before mortgage payments, divided by its purchase price: `(annual rent − annual operating costs) ÷ purchase price`. A cap rate helps compare properties, but it does not include financing or every possible expense.
            - **Estimated monthly cash flow** is the rent left after the dashboard subtracts principal and interest, property tax, insurance, maintenance reserve, and vacancy reserve. A positive result is not guaranteed profit because expenses such as HOA fees, utilities, management, repairs beyond the reserve, closing costs, and income taxes are not included.
            """
        )

    st.subheader("📋 Current listings")
    st.caption("🖱️ Select one row to center the map and highlight that property marker.")
    table_columns = [
        "property_key",
        "street_address",
        "city",
        "property_type",
        "bedrooms",
        "bathrooms",
        "structure_sqft",
        "asking_price",
        "est_monthly_rent",
        "estimate_scope",
        "gross_yield_pct",
        "observed_at",
    ]
    table = filtered[table_columns].rename(
        columns={
            "property_key": "Property key",
            "street_address": "Address",
            "city": "City",
            "property_type": "Type",
            "bedrooms": "Beds",
            "bathrooms": "Baths",
            "structure_sqft": "Square feet",
            "asking_price": "Asking price",
            "est_monthly_rent": "Monthly rent",
            "estimate_scope": "Rent scope",
            "gross_yield_pct": "Gross yield",
            "observed_at": "Observed at",
        }
    )
    table_event = st.dataframe(
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
        column_config={"Property key": None},
        on_select="rerun",
        selection_mode="single-row",
        key="current_listings_table",
    )
    selected_property_key = selected_property_key_from_rows(
        table,
        list(table_event.selection.rows),
    )

    if not filtered.dropna(subset=["latitude", "longitude"]).empty:
        st.subheader("Listing locations")
        render_listing_map(filtered, selected_property_key)

    st.divider()
    render_scenario_calculator(filtered)


def render_market_watchlist_dashboard() -> None:
    """Render current quote snapshots alongside historical market research."""
    st.header("📈 Market Watchlist")
    st.caption(
        "Current quote snapshots and historical market research stored in the canonical "
        "Oracle market model. This is an educational watchlist, not a personal holdings "
        "ledger or investment recommendation."
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

    securities_with_current_quotes = int(watchlist["quote_snapshot_id"].notna().sum())
    oldest_current_quote_observed_at = watchlist["quote_observed_at"].min()
    newest_current_quote_observed_at = watchlist["quote_observed_at"].max()
    quote_age_hours = market_quote_age_hours(oldest_current_quote_observed_at)

    oldest_latest_price_date = watchlist["latest_trade_date"].min()
    newest_latest_price_date = watchlist["latest_trade_date"].max()
    # Freshness is governed by the stalest security, not the most recently
    # refreshed ticker in the watchlist.
    price_age_days = market_price_age_days(oldest_latest_price_date)
    securities_with_fundamentals = int(
        (watchlist["fundamentals_status"] != "MISSING_FUNDAMENTALS").sum()
    )
    fundamentals_coverage = securities_with_fundamentals / len(watchlist) * 100

    metric_1, metric_2, metric_3, metric_4, metric_5 = st.columns(5)
    metric_1.metric("🔎 Watchlist securities", f"{len(watchlist):,}")
    metric_2.metric(
        "💬 Current quotes",
        f"{securities_with_current_quotes:,}/{len(watchlist):,}",
    )
    metric_3.metric(
        "⏱️ Oldest quote age",
        "—" if quote_age_hours is None else f"{quote_age_hours:.1f} hours",
    )
    metric_4.metric(
        "🗓️ Historical price ending",
        timestamp_text(oldest_latest_price_date),
    )
    metric_5.metric("📚 Fundamentals coverage", f"{fundamentals_coverage:.2f}%")

    if securities_with_current_quotes == 0:
        st.error(
            "No current quote snapshots are available. Do not interpret this watchlist as "
            "showing current market prices."
        )
    elif securities_with_current_quotes < len(watchlist):
        st.warning(
            f"Current quote coverage is {securities_with_current_quotes} of {len(watchlist)} "
            "securities. Blank quote values are unavailable, not zero."
        )
    elif quote_age_hours is None:
        st.warning(
            "Current quote snapshots exist, but their observation timestamps are unavailable."
        )
    elif quote_age_hours > 72:
        st.warning(
            f"Current quote coverage is complete, but the oldest quote snapshot is "
            f"{quote_age_hours:.1f} hours old. Refresh quotes before using them as current context."
        )
    else:
        st.success(
            f"Current quote coverage is complete: {securities_with_current_quotes} of "
            f"{len(watchlist)} securities. The oldest quote snapshot is "
            f"{quote_age_hours:.1f} hours old."
        )

    st.caption(
        "Current quote observation timestamps range from "
        f"{timestamp_with_time_text(oldest_current_quote_observed_at)} through "
        f"{timestamp_with_time_text(newest_current_quote_observed_at)}."
    )

    st.caption(
        "Historical daily-price dates range from "
        f"{timestamp_text(oldest_latest_price_date)} through "
        f"{timestamp_text(newest_latest_price_date)}."
    )

    if price_age_days is None:
        st.error(
            "No latest market-price date is available. Do not interpret the watchlist as current market data."
        )
    elif price_age_days > 7:
        st.warning(
            f"The historical daily-price series is {price_age_days} calendar days old. "
            "It remains useful only as dated context for the table and trend chart below; "
            "it does not replace the current quote snapshots above."
        )
    else:
        st.info(
            f"The oldest latest stored market price is {price_age_days} calendar day(s) old. "
            "They are historical reference data, not real-time quotes."
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

    render_metric_dictionary()

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

    st.subheader("💬 Current quote snapshots")
    st.caption(
        "These values come from the latest successful Finnhub quote snapshot for each "
        "security. They are displayed separately from historical daily close prices."
    )
    quote_comparison = filtered[
        [
            "ticker",
            "sector_display",
            "current_quote_price",
            "quote_change_amount",
            "quote_change_pct",
            "quote_prior_close_price",
            "quote_day_open_price",
            "quote_day_high_price",
            "quote_day_low_price",
            "provider_quote_at",
            "quote_observed_at",
        ]
    ].rename(
        columns={
            "ticker": "Ticker",
            "sector_display": "Sector",
            "current_quote_price": "Current quote",
            "quote_change_amount": "Change",
            "quote_change_pct": "Change %",
            "quote_prior_close_price": "Prior close",
            "quote_day_open_price": "Day open",
            "quote_day_high_price": "Day high",
            "quote_day_low_price": "Day low",
            "provider_quote_at": "Provider quote time",
            "quote_observed_at": "Observed by pipeline",
        }
    )
    quote_comparison = quote_comparison.sort_values(by=["Sector", "Ticker"])
    st.dataframe(
        quote_comparison.style.format(
            {
                "Current quote": "${:,.2f}",
                "Change": "${:,.2f}",
                "Change %": "{:.2f}%",
                "Prior close": "${:,.2f}",
                "Day open": "${:,.2f}",
                "Day high": "${:,.2f}",
                "Day low": "${:,.2f}",
                "Provider quote time": timestamp_with_time_text,
                "Observed by pipeline": timestamp_with_time_text,
            },
            na_rep="—",
        ),
        hide_index=True,
        use_container_width=True,
    )

    render_fundamentals_explorer(filtered)

    st.subheader("📉 Historical daily close-price trend")
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
        f"{selected_ticker}: {len(chart_frame):,} stored historical trading day(s), from "
        f"{timestamp_text(chart_frame['trade_date'].min())} through "
        f"{timestamp_text(chart_frame['trade_date'].max())}. This chart does not update "
        "when a current quote snapshot is loaded."
    )
    st.metric(
        f"Historical {selected_ticker} daily close",
        money(latest_price["close_price"]),
        help="This is a historical daily close stored in Oracle, not a current quote.",
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
