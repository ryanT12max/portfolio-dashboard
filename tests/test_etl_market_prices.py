"""Unit tests for the yfinance daily-price refresh safety rules."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from etl_market_prices import (
    DailyPriceCandidate,
    DailyPriceRecord,
    insert_daily_price,
    provider_end_date,
    required_through_date,
    resolve_refresh_limit,
    validate_candidate_rows,
    validate_daily_price_records,
)


def candidate() -> DailyPriceCandidate:
    """Return a valid canonical security for price-record tests."""
    return DailyPriceCandidate(security_key=1, ticker="AAPL", sector="Technology")


def test_pre_close_window_excludes_current_session() -> None:
    now = datetime(2026, 9, 23, 19, tzinfo=timezone.utc)  # 3:00 p.m. New York

    assert provider_end_date(now) == date(2026, 9, 23)
    assert required_through_date(now) == date(2026, 9, 22)


def test_post_close_window_allows_current_session() -> None:
    now = datetime(2026, 9, 23, 21, tzinfo=timezone.utc)  # 5:00 p.m. New York

    assert provider_end_date(now) == date(2026, 9, 24)
    assert required_through_date(now) == date(2026, 9, 23)


def test_required_through_date_skips_weekends() -> None:
    now = datetime(2026, 9, 21, 15, tzinfo=timezone.utc)  # Monday pre-close

    assert required_through_date(now) == date(2026, 9, 18)


def test_all_watchlist_limit_cannot_be_combined_with_pilot_limit() -> None:
    with pytest.raises(ValueError, match="all-watchlist"):
        resolve_refresh_limit(True, 1)


def test_all_watchlist_uses_fixed_safety_cap() -> None:
    assert resolve_refresh_limit(True, None) == 100


def test_candidate_contract_rejects_invalid_ticker() -> None:
    candidates, rejected_count = validate_candidate_rows(
        [(1, "AAPL", "Technology"), (2, "bad ticker", None)]
    )

    assert [item.ticker for item in candidates] == ["AAPL"]
    assert rejected_count == 1


def test_daily_records_keep_unadjusted_close_and_volume() -> None:
    history = pd.DataFrame(
        {"Close": [210.25], "Volume": [1234567]},
        index=pd.to_datetime(["2026-09-22"]),
    )

    records, rejected_count = validate_daily_price_records(
        candidate(), history, date(2026, 9, 23)
    )

    assert records == [
        DailyPriceRecord(
            security_key=1,
            ticker="AAPL",
            trade_date=date(2026, 9, 22),
            close_price=210.25,
            volume=1234567,
        )
    ]
    assert rejected_count == 0


def test_invalid_close_is_rejected_without_substituting_zero() -> None:
    history = pd.DataFrame(
        {"Close": [0], "Volume": [1234567]},
        index=pd.to_datetime(["2026-09-22"]),
    )

    records, rejected_count = validate_daily_price_records(
        candidate(), history, date(2026, 9, 23)
    )

    assert records == []
    assert rejected_count == 1


def test_missing_volume_is_rejected_without_substituting_zero() -> None:
    history = pd.DataFrame(
        {"Close": [210.25], "Volume": [float("nan")]},
        index=pd.to_datetime(["2026-09-22"]),
    )

    records, rejected_count = validate_daily_price_records(
        candidate(), history, date(2026, 9, 23)
    )

    assert records == []
    assert rejected_count == 1


def test_provider_row_on_or_after_exclusive_end_date_is_rejected() -> None:
    history = pd.DataFrame(
        {"Close": [210.25], "Volume": [1234567]},
        index=pd.to_datetime(["2026-09-23"]),
    )

    records, rejected_count = validate_daily_price_records(
        candidate(), history, date(2026, 9, 23)
    )

    assert records == []
    assert rejected_count == 1


class FakeCursor:
    """Minimal cursor double used to inspect the immutable insert statement."""

    def __init__(self) -> None:
        self.sql = ""
        self.bind_values: dict[str, object] = {}
        self.rowcount = 1

    def execute(self, sql: str, bind_values: dict[str, object]) -> None:
        self.sql = sql
        self.bind_values = bind_values


def test_daily_price_loader_is_insert_only() -> None:
    cursor = FakeCursor()
    record = DailyPriceRecord(
        security_key=1,
        ticker="AAPL",
        trade_date=date(2026, 9, 22),
        close_price=210.25,
        volume=1234567,
    )

    inserted = insert_daily_price(cursor, record, run_id=99)  # type: ignore[arg-type]

    assert inserted == 1
    assert "WHERE NOT EXISTS" in cursor.sql
    assert "UPDATE SET" not in cursor.sql
    assert cursor.bind_values["source_name"] == "YFINANCE_DAILY_PRICE"
