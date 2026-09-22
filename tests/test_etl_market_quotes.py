"""Unit tests for the no-call market-quote pilot selection."""

from __future__ import annotations

import pytest

from etl_market_quotes import validate_candidate_rows, validate_limit


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