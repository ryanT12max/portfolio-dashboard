"""Unit tests for market-research contracts and no-call helper functions."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from etl_market_research import (
    FinnhubCandlePayload,
    MarketSecurity,
    validate_daily_price_records,
    validate_limit,
    validate_lookback_days,
    validate_security_rows,
)


def test_validate_limit_accepts_the_pilot_bounds() -> None:
    assert validate_limit(1) == 1
    assert validate_limit(10) == 10


@pytest.mark.parametrize("value", [0, 11])
def test_validate_limit_rejects_values_outside_the_pilot_cap(value: int) -> None:
    with pytest.raises(ValueError, match="Pilot limit must be between 1 and 10"):
        validate_limit(value)


@pytest.mark.parametrize("value", [0, 32])
def test_validate_lookback_days_rejects_values_outside_the_cap(value: int) -> None:
    with pytest.raises(ValueError, match="Lookback days must be between 1 and 31"):
        validate_lookback_days(value)


def test_validate_security_rows_accepts_a_canonical_ticker() -> None:
    securities, rejected_count = validate_security_rows(
        [(101, "AAPL", "Technology")]
    )

    assert rejected_count == 0
    assert len(securities) == 1
    assert securities[0].ticker == "AAPL"


def test_validate_security_rows_rejects_an_invalid_ticker() -> None:
    securities, rejected_count = validate_security_rows(
        [(101, "aapl", "Technology")]
    )

    assert securities == []
    assert rejected_count == 1


def test_finnhub_candle_payload_rejects_misaligned_arrays() -> None:
    with pytest.raises(ValidationError, match="matching close, timestamp, and volume"):
        FinnhubCandlePayload.model_validate(
            {"s": "ok", "c": [100.0], "t": [1, 2], "v": [1000, 2000]}
        )


def test_validate_daily_price_records_converts_daily_candles() -> None:
    security = MarketSecurity(security_key=101, ticker="AAPL", sector="Technology")
    payload = FinnhubCandlePayload.model_validate(
        {
            "s": "ok",
            "c": [100.25, 101.50],
            "t": [1_789_689_600, 1_789_776_000],
            "v": [1_000, 2_000],
        }
    )

    records, rejected_count = validate_daily_price_records(security, payload)

    assert rejected_count == 0
    assert len(records) == 2
    assert records[0].trade_date <= records[1].trade_date
    assert records[0].close_price == 100.25
    assert records[1].volume == 2_000
    assert all(record.trade_date >= date(2026, 1, 1) for record in records)