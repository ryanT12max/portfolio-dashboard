"""Extract active Houston sale listings from RentCast and load them into Oracle.

This pipeline writes only to the PORTFOLIO_APP canonical real-estate model:
RE_ETL_RUN, RE_PROPERTY, and RE_LISTING_SNAPSHOT.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import oracledb
import requests
from pydantic import BaseModel, Field, ValidationError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.database import get_connection

SOURCE_NAME = "RENTCAST"
RENTCAST_URL = "https://api.rentcast.io/v1/listings/sale"
DEFAULT_CITY = "Houston"
DEFAULT_STATE = "TX"
# RentCast documents these property-type values; use a pipe for multiple values.
PROPERTY_TYPES = "Multi-Family|Townhouse"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("etl_pipeline.log"), logging.StreamHandler()],
)
logger = logging.getLogger("RealEstateETL")


class PropertyListing(BaseModel):
    """Validated subset of a RentCast sale-listing response."""

    source_property_id: str = Field(..., min_length=1, alias="id")
    street_address: str = Field(..., min_length=1, alias="formattedAddress")
    city: Optional[str] = Field(default=None, alias="city")
    state_code: Optional[str] = Field(default=None, min_length=2, max_length=2, alias="state")
    zip_code: Optional[str] = Field(default=None, alias="zipCode")
    property_type: Optional[str] = Field(default=None, alias="propertyType")
    bedrooms: Optional[float] = Field(default=None, ge=0, alias="bedrooms")
    bathrooms: Optional[float] = Field(default=None, ge=0, alias="bathrooms")
    structure_sqft: Optional[int] = Field(default=None, ge=0, alias="squareFootage")
    lot_size_sqft: Optional[int] = Field(default=None, ge=0, alias="lotSize")
    year_built: Optional[int] = Field(default=None, ge=1600, le=2200, alias="yearBuilt")
    latitude: Optional[float] = Field(default=None, ge=-90, le=90, alias="latitude")
    longitude: Optional[float] = Field(default=None, ge=-180, le=180, alias="longitude")
    asking_price: float = Field(..., gt=0, alias="price")
    # Sale-listing responses do not guarantee a rent estimate. Preserve missing as NULL.
    est_monthly_rent: Optional[float] = Field(default=None, ge=0, alias="rent")
    listing_status: Optional[str] = Field(default=None, alias="status")
    listing_date: Optional[datetime] = Field(default=None, alias="listedDate")
    last_seen_date: Optional[datetime] = Field(default=None, alias="lastSeenDate")


@dataclass(frozen=True)
class FetchMetrics:
    extracted_count: int
    validated_count: int
    rejected_count: int


def build_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def fetch_and_validate_listings() -> tuple[list[PropertyListing], FetchMetrics]:
    """Fetch pages up to the configured cap and validate every returned row."""
    settings = get_settings()
    api_key = settings.rentcast_api_key

    if not api_key:
        raise ConfigurationError(
            "Missing required environment variable: RENTCAST_API_KEY. "
            "Add it to your local .env file or Codespaces secrets."
        )

    record_cap = settings.rentcast_max_listings
    headers = {"accept": "application/json", "X-Api-Key": api_key}
    session = build_http_session()

    extracted_count = 0
    rejected_count = 0
    offset = 0
    records_by_id: dict[str, PropertyListing] = {}

    logger.info(
        "Starting RentCast fetch: city=%s, state=%s, types=%s, max_records=%s",
        DEFAULT_CITY,
        DEFAULT_STATE,
        PROPERTY_TYPES,
        record_cap,
    )

    while extracted_count < record_cap:
        page_size = min(500, record_cap - extracted_count)
        params = {
            "city": DEFAULT_CITY,
            "state": DEFAULT_STATE,
            "propertyType": PROPERTY_TYPES,
            "status": "Active",
            "limit": page_size,
            "offset": offset,
        }

        response = session.get(RENTCAST_URL, headers=headers, params=params, timeout=(5, 30))
        response.raise_for_status()
        page = response.json()

        if not isinstance(page, list):
            raise RuntimeError("RentCast returned an unexpected response format; expected a JSON list.")
        if not page:
            break

        extracted_count += len(page)
        logger.info("Received %s records at offset %s.", len(page), offset)

        for item in page:
            try:
                listing = PropertyListing.model_validate(item)
            except ValidationError as error:
                rejected_count += 1
                logger.warning("Rejected a RentCast record during validation: %s", error.errors())
                continue

            # The endpoint is ordered by lastSeenDate descending. Keep the first record if
            # a provider ID appears more than once in the same fetch.
            if listing.source_property_id in records_by_id:
                rejected_count += 1
                logger.warning("Rejected duplicate provider ID: %s", listing.source_property_id)
                continue

            records_by_id[listing.source_property_id] = listing

        if len(page) < page_size:
            break
        offset += len(page)

    records = list(records_by_id.values())
    metrics = FetchMetrics(
        extracted_count=extracted_count,
        validated_count=len(records),
        rejected_count=rejected_count,
    )
    logger.info(
        "Validation complete: extracted=%s, validated=%s, rejected=%s.",
        metrics.extracted_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return records, metrics


def scalar_out_value(value: object) -> int:
    """Normalize python-oracledb RETURNING values across driver versions."""
    if isinstance(value, list):
        value = value[0]
    return int(value)


def start_etl_run(cursor: oracledb.Cursor) -> int:
    run_id = cursor.var(oracledb.DB_TYPE_NUMBER)
    cursor.execute(
        """
        INSERT INTO re_etl_run (source_name, run_status)
        VALUES (:source_name, 'RUNNING')
        RETURNING run_id INTO :run_id
        """,
        {"source_name": SOURCE_NAME, "run_id": run_id},
    )
    return scalar_out_value(run_id.getvalue())


def finish_etl_run(
    cursor: oracledb.Cursor,
    run_id: int,
    run_status: str,
    metrics: FetchMetrics,
    loaded_count: int,
    error_message: Optional[str] = None,
) -> None:
    cursor.execute(
        """
        UPDATE re_etl_run
        SET completed_at = SYSTIMESTAMP,
            run_status = :run_status,
            extracted_count = :extracted_count,
            validated_count = :validated_count,
            rejected_count = :rejected_count,
            loaded_count = :loaded_count,
            error_message = :error_message
        WHERE run_id = :run_id
        """,
        {
            "run_id": run_id,
            "run_status": run_status,
            "extracted_count": metrics.extracted_count,
            "validated_count": metrics.validated_count,
            "rejected_count": metrics.rejected_count,
            "loaded_count": loaded_count,
            "error_message": error_message,
        },
    )


def payload_hash(listing: PropertyListing) -> str:
    payload = json.dumps(listing.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def as_date(value: Optional[datetime]) -> Optional[date]:
    return value.date() if value else None


def upsert_property(cursor: oracledb.Cursor, listing: PropertyListing) -> int:
    """Upsert stable attributes and return the database-generated property key."""
    # Several property fields are optional. Explicit bind types prevent Oracle from
    # inferring an inconsistent datatype when the first value for a field is NULL.
    cursor.setinputsizes(
        source_name=str,
        source_property_id=str,
        street_address=str,
        city=str,
        state_code=str,
        zip_code=str,
        property_type=str,
        bedrooms=float,
        bathrooms=float,
        structure_sqft=int,
        lot_size_sqft=int,
        year_built=int,
        latitude=float,
        longitude=float,
    )
    bindings = {
        "source_name": SOURCE_NAME,
        "source_property_id": listing.source_property_id,
        "street_address": listing.street_address,
        "city": listing.city,
        "state_code": listing.state_code,
        "zip_code": listing.zip_code,
        "property_type": listing.property_type,
        "bedrooms": listing.bedrooms,
        "bathrooms": listing.bathrooms,
        "structure_sqft": listing.structure_sqft,
        "lot_size_sqft": listing.lot_size_sqft,
        "year_built": listing.year_built,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
    }
    cursor.execute(
        """
        MERGE INTO re_property target
        USING (
            SELECT :source_name AS source_name,
                   :source_property_id AS source_property_id
            FROM dual
        ) source
        ON (
            target.source_name = source.source_name
            AND target.source_property_id = source.source_property_id
        )
        WHEN MATCHED THEN
            UPDATE SET
                target.street_address = :street_address,
                target.city = COALESCE(:city, target.city),
                target.state_code = COALESCE(:state_code, target.state_code),
                target.zip_code = COALESCE(:zip_code, target.zip_code),
                target.property_type = COALESCE(:property_type, target.property_type),
                target.bedrooms = COALESCE(:bedrooms, target.bedrooms),
                target.bathrooms = COALESCE(:bathrooms, target.bathrooms),
                target.structure_sqft = COALESCE(:structure_sqft, target.structure_sqft),
                target.lot_size_sqft = COALESCE(:lot_size_sqft, target.lot_size_sqft),
                target.year_built = COALESCE(:year_built, target.year_built),
                target.latitude = COALESCE(:latitude, target.latitude),
                target.longitude = COALESCE(:longitude, target.longitude),
                target.updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN
            INSERT (
                source_name, source_property_id, street_address, city, state_code,
                zip_code, property_type, bedrooms, bathrooms, structure_sqft,
                lot_size_sqft, year_built, latitude, longitude
            )
            VALUES (
                :source_name, :source_property_id, :street_address, :city, :state_code,
                :zip_code, :property_type, :bedrooms, :bathrooms, :structure_sqft,
                :lot_size_sqft, :year_built, :latitude, :longitude
            )
        """,
        bindings,
    )
    cursor.execute(
        """
        SELECT property_key
        FROM re_property
        WHERE source_name = :source_name
          AND source_property_id = :source_property_id
        """,
        {"source_name": SOURCE_NAME, "source_property_id": listing.source_property_id},
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"No property key found for source ID {listing.source_property_id}.")
    return int(row[0])


def insert_listing_snapshot(
    cursor: oracledb.Cursor,
    property_key: int,
    run_id: int,
    listing: PropertyListing,
) -> None:
    cursor.execute(
        """
        INSERT INTO re_listing_snapshot (
            property_key, run_id, listing_date, last_seen_date, listing_status,
            asking_price, est_monthly_rent, source_payload_hash
        )
        VALUES (
            :property_key, :run_id, :listing_date, :last_seen_date, :listing_status,
            :asking_price, :est_monthly_rent, :source_payload_hash
        )
        """,
        {
            "property_key": property_key,
            "run_id": run_id,
            "listing_date": as_date(listing.listing_date),
            "last_seen_date": as_date(listing.last_seen_date),
            "listing_status": listing.listing_status,
            "asking_price": listing.asking_price,
            "est_monthly_rent": listing.est_monthly_rent,
            "source_payload_hash": payload_hash(listing),
        },
    )


def run_pipeline() -> int:
    """Audit, fetch, and load one complete run with atomic property/snapshot writes."""
    connection = get_connection()
    run_id: Optional[int] = None
    metrics = FetchMetrics(extracted_count=0, validated_count=0, rejected_count=0)

    try:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = start_etl_run(cursor)
            connection.commit()

            # The audit row is committed before the external API call, so failures are visible.
            records, metrics = fetch_and_validate_listings()

            for listing in records:
                property_key = upsert_property(cursor, listing)
                insert_listing_snapshot(cursor, property_key, run_id, listing)

            run_status = "PARTIAL" if metrics.rejected_count else "SUCCEEDED"
            finish_etl_run(cursor, run_id, run_status, metrics, loaded_count=len(records))
            connection.commit()
            logger.info("Run %s completed: loaded=%s, status=%s.", run_id, len(records), run_status)
            return run_id

    except Exception as error:
        connection.rollback()
        if run_id is not None:
            with connection.cursor() as cursor:
                finish_etl_run(
                    cursor,
                    run_id,
                    "FAILED",
                    metrics,
                    loaded_count=0,
                    error_message=str(error)[:1000],
                )
            connection.commit()
        logger.exception("Real-estate ETL failed.")
        raise
    finally:
        connection.close()


def main() -> None:
    try:
        run_pipeline()
    except Exception:
        logger.exception("Pipeline terminated with an error.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
