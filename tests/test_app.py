"""Focused tests for dashboard helpers and quote-query separation."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app import (
    CURRENT_LISTINGS_SQL,
    FUNDAMENTAL_METRIC_GROUPS,
    MAP_DEFAULT_COLOR,
    MAP_SELECTED_COLOR,
    MARKET_WATCHLIST_SQL,
    build_listing_map_frame,
    build_fundamentals_explorer_frame,
    market_quote_age_hours,
    selected_property_key_from_rows,
    timestamp_with_time_text,
)


def test_market_quote_age_uses_elapsed_utc_hours() -> None:
    observed_at = datetime(2026, 9, 22, 18, tzinfo=timezone.utc)
    now = datetime(2026, 9, 22, 20, 30, tzinfo=timezone.utc)

    assert market_quote_age_hours(observed_at, now) == 2.5


def test_market_watchlist_query_keeps_quotes_separate_from_daily_prices() -> None:
    normalized_sql = " ".join(MARKET_WATCHLIST_SQL.upper().split())

    assert "LEFT JOIN VW_MK_CURRENT_QUOTE QUOTE" in normalized_sql
    assert "QUOTE.CURRENT_PRICE AS CURRENT_QUOTE_PRICE" in normalized_sql
    assert "WATCHLIST.LATEST_CLOSE_PRICE" in normalized_sql


def test_timestamp_with_time_text_omits_unverified_timezone_label() -> None:
    timestamp = datetime(2026, 9, 22, 19, 43, tzinfo=timezone.utc)

    assert timestamp_with_time_text(timestamp) == "2026-09-22 19:43"


def test_real_estate_query_uses_scope_aware_investment_analysis_view() -> None:
    normalized_sql = " ".join(CURRENT_LISTINGS_SQL.upper().split())

    assert "FROM VW_RE_CURRENT_INVESTMENT_ANALYSIS" in normalized_sql
    assert "ESTIMATE_SCOPE" in normalized_sql
    assert "RENT_SUBJECT_ATTRIBUTE_MODE" in normalized_sql
    assert "GROSS_YIELD_ELIGIBLE" in normalized_sql


def test_selected_listing_row_resolves_to_its_canonical_property_key() -> None:
    table = pd.DataFrame(
        {
            "Property key": [101, 202],
            "Address": ["First Street", "Second Street"],
        }
    )

    assert selected_property_key_from_rows(table, [1]) == 202
    assert selected_property_key_from_rows(table, []) is None
    assert selected_property_key_from_rows(table, [99]) is None


def test_selected_listing_uses_a_distinct_map_marker_color() -> None:
    listings = pd.DataFrame(
        {
            "property_key": [101, 202],
            "latitude": [29.76, 29.77],
            "longitude": [-95.36, -95.37],
        }
    )

    map_frame = build_listing_map_frame(listings, selected_property_key=202)

    assert map_frame.iloc[0]["marker_color"] == MAP_DEFAULT_COLOR
    assert map_frame.iloc[1]["marker_color"] == MAP_SELECTED_COLOR


def test_current_listing_table_does_not_show_internal_rent_profile_columns() -> None:
    source = open("app.py", encoding="utf-8").read()

    assert '"rent_subject_attribute_mode",' not in source
    assert '"rent_estimate_status",' not in source


def test_fundamentals_explorer_shows_all_stocks_in_a_compact_metric_group() -> None:
    watchlist = pd.DataFrame(
        {
            "ticker": ["BBB", "AAA", "CCC"],
            "sector": ["Energy", None, "Technology"],
            "pe_ratio": [20.0, 10.0, None],
            "peg_ratio": [2.0, 1.0, None],
            "pb_ratio": [3.0, 2.0, None],
        }
    )

    explorer, labels = build_fundamentals_explorer_frame(
        watchlist,
        metric_group="Valuation",
        sort_column="pe_ratio",
        ascending=True,
        hide_missing_metrics=False,
        show_lineage=False,
    )

    assert list(explorer["Ticker"]) == ["AAA", "BBB", "CCC"]
    assert list(explorer.columns) == ["Ticker", "Sector", "P/E ratio", "PEG ratio", "P/B ratio"]
    assert labels["pe_ratio"] == "P/E ratio"


def test_fundamentals_explorer_can_hide_stocks_without_a_group_value() -> None:
    watchlist = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "sector": ["Technology", "Energy"],
            "debt_to_equity_ratio": [0.5, None],
            "free_cash_flow_value": [None, None],
            "free_cash_flow_unit": [None, None],
        }
    )

    explorer, _ = build_fundamentals_explorer_frame(
        watchlist,
        metric_group="Financial health & solvency",
        sort_column="debt_to_equity_ratio",
        ascending=True,
        hide_missing_metrics=True,
        show_lineage=False,
    )

    assert list(explorer["Ticker"]) == ["AAA"]
    assert "FCF unit" in explorer.columns


def test_market_dashboard_replaces_wide_comparison_with_fundamentals_explorer() -> None:
    source = open("app.py", encoding="utf-8").read()

    assert "Historical daily-price and fundamentals comparison" not in source
    assert "render_fundamentals_explorer(filtered)" in source
    assert set(FUNDAMENTAL_METRIC_GROUPS) == {
        "Valuation",
        "Profitability & efficiency",
        "Financial health & solvency",
    }
