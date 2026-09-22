"""Unit tests for market-quote contracts and no-call helper functions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests
from pydantic import ValidationError

from etl_market_quotes import (
    FinnhubQuotePayload,
    QUOTE_CANDIDATE_QUERY,
    QuoteCandidate,
    build_quote_snapshot,
    fetch_quote_payload,
    validate_candidate_rows,
    validate_limit,
)


def test_validate_limit_accepts_the_pilot_bounds() -> None:
    assert validate_limit(1) == 1
    assert validate_limit(10) == 10


@pytest.mark.parametrize("value", [0, 11])
def test_validate_limit_rejects_values_outside_the_pilot_cap(value: int) -> None:
    with pytest.raises(ValueError, match="Pilot limit must be between 1 and 10"):
        validate_limit(value)


def test_validate_candidate_rows_accepts_a_canonical_ticker() -> None:
    candidates, rejected_count = validate_candidate_rows(
        [(101, "AAPL", "Technology")]
    )

    assert rejected_count == 0
    assert len(candidates) == 1
    assert candidates[0].security_key == 101
    assert candidates[0].ticker == "AAPL"


def test_validate_candidate_rows_rejects_an_invalid_ticker() -> None:
    candidates, rejected_count = validate_candidate_rows(
        [(101, "aapl", "Technology")]
    )

    assert candidates == []
    assert rejected_count == 1


def test_candidate_query_skips_successful_quotes_observed_today() -> None:
    normalized_query = " ".join(QUOTE_CANDIDATE_QUERY.split())

    assert "NOT EXISTS" in normalized_query
    assert "mk_quote_snapshot snapshot" in normalized_query
    assert "run_audit.run_status IN ('SUCCEEDED', 'PARTIAL')" in normalized_query
    assert "snapshot.observed_at AT TIME ZONE 'UTC'" in normalized_query


def test_finnhub_quote_payload_rejects_an_invalid_day_range() -> None:
    with pytest.raises(ValidationError, match="day_low_price cannot exceed"):
        FinnhubQuotePayload.model_validate(
            {"c": 100.0, "h": 99.0, "l": 101.0, "t": 1_789_689_600}
        )


def test_build_quote_snapshot_normalizes_provider_data() -> None:
    candidate = QuoteCandidate(
        security_key=101,
        ticker="AAPL",
        sector="Technology",
    )
    payload = FinnhubQuotePayload.model_validate(
        {
            "c": 101.5,
            "d": 1.25,
            "dp": 1.25,
            "h": 102.0,
            "l": 99.5,
            "o": 100.0,
            "pc": 100.25,
            "t": 1_789_689_600,
        }
    )

    snapshot = build_quote_snapshot(candidate, payload)

    assert snapshot.security_key == 101
    assert snapshot.current_price == 101.5
    assert snapshot.provider_quote_at is not None
    assert snapshot.provider_quote_at.tzinfo == timezone.utc
    assert len(snapshot.source_payload_hash) == 64


class _StaticResponse:
    """Minimal response double for a provider HTTP-status failure."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpFailureSession:
    """Return a fixed HTTP failure without making a network request."""

    def get(self, *args: object, **kwargs: object) -> _StaticResponse:
        return _StaticResponse(403)


class _RequestFailureSession:
    """Raise a Requests error containing a synthetic token-like value."""

    def get(self, *args: object, **kwargs: object) -> _StaticResponse:
        raise requests.RequestException(
            "request failed for https://example.test/?token=test-token-must-not-appear"
        )


@pytest.mark.parametrize(
    "session",
    [_HttpFailureSession(), _RequestFailureSession()],
)
def test_provider_errors_do_not_expose_the_api_key(session: object) -> None:
    with pytest.raises(RuntimeError) as captured_error:
        fetch_quote_payload(
            session,  # type: ignore[arg-type]
            ticker="AAPL",
            api_key="test-token-must-not-appear",
        )

    error_message = str(captured_error.value)
    assert "test-token-must-not-appear" not in error_message
    assert "AAPL" in error_message
