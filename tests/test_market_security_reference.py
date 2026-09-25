"""Focused tests for canonical company-name and sector display helpers."""

from __future__ import annotations

import pandas as pd

from app import MARKET_WATCHLIST_SQL, prepare_market_display_frame


def test_market_watchlist_query_reads_canonical_security_reference_fields() -> None:
    normalized_sql = " ".join(MARKET_WATCHLIST_SQL.upper().split())

    assert "WATCHLIST.SECURITY_NAME" in normalized_sql
    assert "WATCHLIST.SECURITY_REFERENCE_SOURCE" in normalized_sql
    assert "WATCHLIST.SECURITY_REFERENCE_OBSERVED_AT" in normalized_sql


def test_prepare_market_display_frame_keeps_known_name_and_sector() -> None:
    watchlist = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "security_name": "  Apple   Inc.  ",
                "sector": " Technology ",
            }
        ]
    )

    display = prepare_market_display_frame(watchlist)

    assert display.loc[0, "company_display"] == "Apple Inc."
    assert display.loc[0, "sector_display"] == "Technology"


def test_prepare_market_display_frame_labels_missing_reference_data_honestly() -> None:
    watchlist = pd.DataFrame(
        [
            {
                "ticker": "MISSING",
                "security_name": None,
                "sector": None,
            }
        ]
    )

    display = prepare_market_display_frame(watchlist)

    assert display.loc[0, "company_display"] == "Name unavailable"
    assert display.loc[0, "sector_display"] == "Unclassified"
