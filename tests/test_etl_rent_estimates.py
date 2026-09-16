from __future__ import annotations

from datetime import datetime, timezone

import pytest

from etl_rent_estimates import (
    MAX_PILOT_LIMIT,
    RentEstimateCandidate,
    validate_candidate_rows,
    validate_limit,
)


def candidate_row(*, asking_price: float = 250000.0) -> tuple[object, ...]:
    return (
        195,
        "3754 Tanglewilde St, Unit C3, Houston, TX 77063",
        "Houston",
        "TX",
        "77063",
        "Townhouse",
        2,
        1.0,
        1031,
        29.724,
        -95.536,
        asking_price,
        datetime(2026, 9, 15, 20, 27, 13, tzinfo=timezone.utc),
    )


def test_validate_candidate_rows_accepts_complete_candidate() -> None:
    candidates, rejected_count = validate_candidate_rows([candidate_row()])

    assert rejected_count == 0
    assert len(candidates) == 1
    assert isinstance(candidates[0], RentEstimateCandidate)
    assert candidates[0].property_key == 195


def test_validate_candidate_rows_rejects_nonpositive_asking_price() -> None:
    candidates, rejected_count = validate_candidate_rows([candidate_row(asking_price=0)])

    assert candidates == []
    assert rejected_count == 1


@pytest.mark.parametrize("limit", [0, MAX_PILOT_LIMIT + 1])
def test_validate_limit_rejects_values_outside_pilot_cap(limit: int) -> None:
    with pytest.raises(ValueError, match="Pilot limit must be"):
        validate_limit(limit)
