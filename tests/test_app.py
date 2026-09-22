"""Focused tests for dashboard helpers and quote-query separation."""

from __future__ import annotations

from datetime import datetime, timezone

from app import MARKET_WATCHLIST_SQL, market_quote_age_hours, timestamp_with_time_text


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
