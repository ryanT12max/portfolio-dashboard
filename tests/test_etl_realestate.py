from datetime import datetime

import pytest
from pydantic import ValidationError

from etl_realestate import PropertyListing, as_date, payload_hash


def valid_listing_payload():
    return {
        "id": "rentcast-property-123",
        "formattedAddress": "123 Example Street, Houston, TX 77002",
        "city": "Houston",
        "state": "TX",
        "zipCode": "77002",
        "propertyType": "Multi-Family",
        "bedrooms": 4,
        "bathrooms": 3.5,
        "squareFootage": 2400,
        "lotSize": 5000,
        "yearBuilt": 2005,
        "latitude": 29.7604,
        "longitude": -95.3698,
        "price": 450000,
        "status": "Active",
    }


def test_listing_contract_preserves_missing_rent_as_none():
    listing = PropertyListing.model_validate(valid_listing_payload())

    assert listing.est_monthly_rent is None
    assert listing.source_property_id == "rentcast-property-123"


def test_listing_contract_rejects_non_positive_price():
    payload = valid_listing_payload()
    payload["price"] = 0

    with pytest.raises(ValidationError):
        PropertyListing.model_validate(payload)


def test_payload_hash_is_deterministic_and_dates_are_normalized():
    listing = PropertyListing.model_validate(valid_listing_payload())

    assert payload_hash(listing) == payload_hash(listing)
    assert len(payload_hash(listing)) == 64
    assert as_date(datetime(2026, 9, 15, 12, 30)) == datetime(2026, 9, 15).date()