from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from etl_rent_estimates import (
    MAX_PILOT_LIMIT,
    RentEstimateCandidate,
    RentEstimateResponse,
    build_coordinate_rent_estimate_params,
    build_rent_estimate_params,
    can_use_coordinate_fallback,
    estimate_scope_for,
    subject_attribute_mode_for,
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


def multi_family_candidate_row() -> tuple[object, ...]:
    values = list(candidate_row())
    values[0] = 196
    values[5] = "Multi-Family"
    return tuple(values)


def address_only_candidate_row() -> tuple[object, ...]:
    """A valid addressable listing whose internal square-footage field is missing."""
    values = list(candidate_row())
    values[0] = 197
    values[8] = None
    return tuple(values)


def valid_candidate() -> RentEstimateCandidate:
    candidates, rejected_count = validate_candidate_rows([candidate_row()])
    assert rejected_count == 0
    return candidates[0]


def test_validate_candidate_rows_accepts_complete_candidate() -> None:
    candidate = valid_candidate()

    assert isinstance(candidate, RentEstimateCandidate)
    assert candidate.property_key == 195


def test_validate_candidate_rows_rejects_nonpositive_asking_price() -> None:
    candidates, rejected_count = validate_candidate_rows([candidate_row(asking_price=0)])

    assert candidates == []
    assert rejected_count == 1


@pytest.mark.parametrize("limit", [0, MAX_PILOT_LIMIT + 1])
def test_validate_limit_rejects_values_outside_pilot_cap(limit: int) -> None:
    with pytest.raises(ValueError, match="Pilot limit must be"):
        validate_limit(limit)


def test_build_rent_estimate_params_uses_address_and_property_attributes() -> None:
    params = build_rent_estimate_params(valid_candidate())

    assert params == {
        "address": "3754 Tanglewilde St, Unit C3, Houston, TX 77063",
        "propertyType": "Townhouse",
        "bedrooms": 2.0,
        "bathrooms": 1.0,
        "squareFootage": 1031,
        "compCount": 5,
    }


def test_coordinate_params_preserve_property_attributes_without_address() -> None:
    params = build_coordinate_rent_estimate_params(valid_candidate())

    assert "address" not in params
    assert params["latitude"] == 29.724
    assert params["longitude"] == -95.536
    assert params["propertyType"] == "Townhouse"


def test_address_only_candidate_uses_provider_attribute_lookup() -> None:
    candidates, rejected_count = validate_candidate_rows([address_only_candidate_row()])

    assert rejected_count == 0
    candidate = candidates[0]
    assert subject_attribute_mode_for(candidate) == "PROVIDER_LOOKUP"
    assert not can_use_coordinate_fallback(candidate)
    assert build_rent_estimate_params(candidate) == {
        "address": "3754 Tanglewilde St, Unit C3, Houston, TX 77063",
        "lookupSubjectAttributes": "true",
        "compCount": 5,
    }


def test_address_only_candidate_cannot_use_coordinate_fallback() -> None:
    candidates, _ = validate_candidate_rows([address_only_candidate_row()])

    with pytest.raises(ValueError, match="Coordinate fallback requires complete"):
        build_coordinate_rent_estimate_params(candidates[0])


def test_multi_family_candidate_is_explicitly_single_unit_scope() -> None:
    candidates, rejected_count = validate_candidate_rows([multi_family_candidate_row()])

    assert rejected_count == 0
    assert estimate_scope_for(candidates[0]) == "UNIT"


def test_townhouse_candidate_is_whole_property_scope() -> None:
    assert estimate_scope_for(valid_candidate()) == "PROPERTY"


def test_rent_estimate_response_rejects_range_outside_estimate() -> None:
    with pytest.raises(ValidationError, match="rentRangeLow cannot exceed rent"):
        RentEstimateResponse.model_validate(
            {"rent": 1800, "rentRangeLow": 1900, "rentRangeHigh": 2000}
        )
