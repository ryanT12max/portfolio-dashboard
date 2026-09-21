"""Unit tests for the no-API market-research pilot selection."""

from __future__ import annotations

import pytest

from etl_market_research import validate_limit, validate_security_rows


def test_validate_limit_accepts_the_pilot_bounds() -> None:
    assert validate_limit(1) == 1
    assert validate_limit(10) == 10


@pytest.mark.parametrize("value", [0, 11])
def test_validate_limit_rejects_values_outside_the_pilot_cap(value: int) -> None:
    with pytest.raises(ValueError, match="Pilot limit must be between 1 and 10"):
        validate_limit(value)


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