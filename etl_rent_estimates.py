"""Backfill canonical RentCast rent estimates in small, auditable batches.

The pipeline is deliberately conservative:

* dry runs make no provider calls and write nothing;
* live mode requires an explicit confirmation and is capped at ten listings;
* each listing attempt is persisted so known-unavailable listings are not
  requested forever;
* RentCast's Multi-Family response is recorded as a single-unit estimate,
  never as whole-building income.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Sequence

import oracledb
import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.database import get_connection, managed_connection


PILOT_PROPERTY_TYPE = "Townhouse"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10
DEFAULT_REQUEST_DELAY_SECONDS = 0.25
MAX_REQUEST_DELAY_SECONDS = 5.0
SOURCE_NAME = "RENTCAST_RENT_AVM"
RENTCAST_RENT_AVM_URL = "https://api.rentcast.io/v1/avm/rent/long-term"
RENTCAST_COMPARABLE_COUNT = 5
SUPPORTED_PROPERTY_TYPES = frozenset(
    {
        "SINGLE FAMILY",
        "CONDO",
        "TOWNHOUSE",
        "MANUFACTURED",
        "MULTI-FAMILY",
        "APARTMENT",
    }
)
UNIT_SCOPE_PROPERTY_TYPES = frozenset({"MULTI-FAMILY", "APARTMENT"})


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("rent_estimate_pipeline.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("RentEstimateETL")


class RentEstimateCandidate(BaseModel):
    """An addressable current listing eligible for RentCast enrichment.

    Complete listings use the dashboard's own subject-property attributes. A
    separate address-only mode may enrich a listing with missing attributes by
    asking RentCast to look them up from the address. The two modes remain
    explicit so incomplete listing data is never silently treated as complete.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    property_key: int = Field(gt=0)
    street_address: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state_code: str = Field(min_length=2, max_length=2)
    zip_code: str = Field(min_length=5, max_length=10)
    property_type: str = Field(min_length=1)
    bedrooms: float | None = Field(default=None, ge=0)
    bathrooms: float | None = Field(default=None, ge=0)
    structure_sqft: int | None = Field(default=None, gt=0)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    asking_price: float = Field(gt=0)
    observed_at: datetime


class RentEstimateResponse(BaseModel):
    """Validated subset of the RentCast long-term rent-AVM response."""

    rent: float = Field(gt=0)
    rent_range_low: float | None = Field(default=None, gt=0, alias="rentRangeLow")
    rent_range_high: float | None = Field(default=None, gt=0, alias="rentRangeHigh")
    comparables: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_rent_range(self) -> "RentEstimateResponse":
        if self.rent_range_low is not None and self.rent_range_low > self.rent:
            raise ValueError("rentRangeLow cannot exceed rent.")
        if self.rent_range_high is not None and self.rent > self.rent_range_high:
            raise ValueError("rent cannot exceed rentRangeHigh.")
        if (
            self.rent_range_low is not None
            and self.rent_range_high is not None
            and self.rent_range_low > self.rent_range_high
        ):
            raise ValueError("rentRangeLow cannot exceed rentRangeHigh.")
        return self


class ProviderRentError(RuntimeError):
    """A redacted provider failure with retry and audit classification metadata."""

    def __init__(
        self,
        message: str,
        *,
        input_method: Literal["ADDRESS", "COORDINATES"],
        error_category: str,
        http_status: int | None = None,
        retryable: bool,
        fatal_configuration_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.input_method = input_method
        self.error_category = error_category
        self.http_status = http_status
        self.retryable = retryable
        self.fatal_configuration_error = fatal_configuration_error


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts from one canonical candidate selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


@dataclass
class LiveRunMetrics:
    """Counts stored in RE_ETL_RUN for one live rent-enrichment batch."""

    extracted_count: int = 0
    validated_count: int = 0
    rejected_count: int = 0
    loaded_count: int = 0


RENT_CANDIDATE_QUERY = """
    SELECT
        property_key,
        street_address,
        city,
        state_code,
        zip_code,
        property_type,
        bedrooms,
        bathrooms,
        structure_sqft,
        latitude,
        longitude,
        asking_price,
        observed_at
    FROM (
        SELECT
            listing.property_key,
            listing.street_address,
            listing.city,
            listing.state_code,
            listing.zip_code,
            listing.property_type,
            listing.bedrooms,
            listing.bathrooms,
            listing.structure_sqft,
            listing.latitude,
            listing.longitude,
            listing.asking_price,
            listing.observed_at
        FROM vw_re_current_listing listing
        WHERE UPPER(listing.property_type) IN (
                'SINGLE FAMILY', 'CONDO', 'TOWNHOUSE', 'MANUFACTURED',
                'MULTI-FAMILY', 'APARTMENT'
            )
          AND (:coverage_backfill = 'Y'
               OR :address_only_backfill = 'Y'
               OR UPPER(listing.property_type) = :property_type)
          AND listing.street_address IS NOT NULL
          AND listing.city IS NOT NULL
          AND listing.state_code IS NOT NULL
          AND listing.zip_code IS NOT NULL
          AND listing.asking_price > 0
          AND (
              (:address_only_backfill = 'N'
               AND listing.bedrooms IS NOT NULL
               AND listing.bathrooms IS NOT NULL
               AND listing.structure_sqft > 0
               AND listing.latitude IS NOT NULL
               AND listing.longitude IS NOT NULL)
              OR
              (:address_only_backfill = 'Y'
               AND (
                   listing.bedrooms IS NULL
                   OR listing.bathrooms IS NULL
                   OR listing.structure_sqft IS NULL
                   OR listing.structure_sqft <= 0
               ))
          )
          AND NOT EXISTS (
              SELECT 1
              FROM re_rent_estimate_snapshot existing_estimate
              WHERE existing_estimate.property_key = listing.property_key
                AND existing_estimate.estimator_source = :estimator_source
          )
          AND NOT EXISTS (
              SELECT 1
              FROM re_rent_estimate_attempt terminal_attempt
              WHERE terminal_attempt.property_key = listing.property_key
                AND terminal_attempt.estimator_source = :estimator_source
                AND terminal_attempt.attempt_status = 'UNAVAILABLE'
          )
        ORDER BY
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM re_rent_estimate_attempt previous_attempt
                    WHERE previous_attempt.property_key = listing.property_key
                      AND previous_attempt.estimator_source = :estimator_source
                      AND previous_attempt.attempt_status = 'TRANSIENT_FAILURE'
                ) THEN 1
                ELSE 0
            END,
            listing.observed_at DESC,
            listing.property_key
    )
    WHERE ROWNUM <= :limit
"""

CANDIDATE_COLUMNS = (
    "property_key",
    "street_address",
    "city",
    "state_code",
    "zip_code",
    "property_type",
    "bedrooms",
    "bathrooms",
    "structure_sqft",
    "latitude",
    "longitude",
    "asking_price",
    "observed_at",
)


def validate_limit(value: int) -> int:
    """Reject accidental batches above the documented ten-listing safety cap."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def validate_request_delay_seconds(value: float) -> float:
    """Keep voluntary request pacing within a small, explicit range."""
    if not 0 <= value <= MAX_REQUEST_DELAY_SECONDS:
        raise ValueError(
            "Request delay must be between 0 and "
            f"{MAX_REQUEST_DELAY_SECONDS:g} seconds; received {value}."
        )
    return value


def estimate_scope_for(candidate: RentEstimateCandidate) -> Literal["PROPERTY", "UNIT"]:
    """Classify RentCast output without ever guessing whole-building income."""
    property_type = candidate.property_type.upper()
    if property_type not in SUPPORTED_PROPERTY_TYPES:
        raise ValueError(f"Unsupported RentCast property type: {candidate.property_type}.")
    if property_type in UNIT_SCOPE_PROPERTY_TYPES:
        return "UNIT"
    return "PROPERTY"


def subject_attribute_mode_for(
    candidate: RentEstimateCandidate,
) -> Literal["PROVIDED_ATTRIBUTES", "PROVIDER_LOOKUP"]:
    """State whether the request uses listing attributes or provider lookup."""
    has_complete_attributes = all(
        value is not None
        for value in (
            candidate.bedrooms,
            candidate.bathrooms,
            candidate.structure_sqft,
        )
    )
    return "PROVIDED_ATTRIBUTES" if has_complete_attributes else "PROVIDER_LOOKUP"


def can_use_coordinate_fallback(candidate: RentEstimateCandidate) -> bool:
    """Coordinate requests require explicit subject attributes; address lookup does not."""
    return (
        subject_attribute_mode_for(candidate) == "PROVIDED_ATTRIBUTES"
        and candidate.latitude is not None
        and candidate.longitude is not None
    )


def validate_candidate_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[list[RentEstimateCandidate], int]:
    """Apply the candidate contract and return valid records plus rejection count."""
    valid_candidates: list[RentEstimateCandidate] = []
    rejected_count = 0

    for row in rows:
        payload = dict(zip(CANDIDATE_COLUMNS, row, strict=True))
        try:
            candidate = RentEstimateCandidate.model_validate(payload)
            estimate_scope_for(candidate)
        except (ValidationError, ValueError) as error:
            rejected_count += 1
            logger.warning(
                "Rejected rent candidate property_key=%s: %s",
                payload.get("property_key"),
                error,
            )
            continue
        valid_candidates.append(candidate)

    return valid_candidates, rejected_count


def load_rent_candidates(
    cursor: oracledb.Cursor,
    limit: int,
    *,
    coverage_backfill: bool,
    address_only_backfill: bool = False,
    property_type: str,
) -> tuple[list[RentEstimateCandidate], CandidateMetrics]:
    """Select full-profile or address-only listings missing a usable estimate."""
    limit = validate_limit(limit)
    normalized_property_type = property_type.upper()
    if normalized_property_type not in SUPPORTED_PROPERTY_TYPES:
        raise ValueError(
            f"--property-type must be one of: {', '.join(sorted(SUPPORTED_PROPERTY_TYPES))}."
        )

    cursor.execute(
        RENT_CANDIDATE_QUERY,
        {
            "coverage_backfill": "Y" if coverage_backfill else "N",
            "address_only_backfill": "Y" if address_only_backfill else "N",
            "property_type": normalized_property_type,
            "estimator_source": SOURCE_NAME,
            "limit": limit,
        },
    )
    rows = cursor.fetchall()
    candidates, rejected_count = validate_candidate_rows(rows)
    return candidates, CandidateMetrics(
        selected_count=len(rows),
        validated_count=len(candidates),
        rejected_count=rejected_count,
    )


def build_rent_estimate_params(candidate: RentEstimateCandidate) -> dict[str, object]:
    """Build a documented address request without mixing partial input attributes."""
    if subject_attribute_mode_for(candidate) == "PROVIDER_LOOKUP":
        return {
            "address": candidate.street_address,
            "lookupSubjectAttributes": "true",
            "compCount": RENTCAST_COMPARABLE_COUNT,
        }

    return {
        "address": candidate.street_address,
        "propertyType": candidate.property_type,
        "bedrooms": candidate.bedrooms,
        "bathrooms": candidate.bathrooms,
        "squareFootage": candidate.structure_sqft,
        "compCount": RENTCAST_COMPARABLE_COUNT,
    }


def build_coordinate_rent_estimate_params(candidate: RentEstimateCandidate) -> dict[str, object]:
    """Build the coordinate fallback only for an address-format provider rejection."""
    if not can_use_coordinate_fallback(candidate):
        raise ValueError(
            "Coordinate fallback requires complete listing attributes and coordinates."
        )
    return {
        "latitude": candidate.latitude,
        "longitude": candidate.longitude,
        "propertyType": candidate.property_type,
        "bedrooms": candidate.bedrooms,
        "bathrooms": candidate.bathrooms,
        "squareFootage": candidate.structure_sqft,
        "compCount": RENTCAST_COMPARABLE_COUNT,
    }


def _fetch_rent_estimate(
    session: requests.Session,
    api_key: str,
    candidate: RentEstimateCandidate,
    *,
    input_method: Literal["ADDRESS", "COORDINATES"],
) -> tuple[RentEstimateResponse, dict[str, Any]]:
    """Fetch and validate one provider response without leaking the API key."""
    params = (
        build_rent_estimate_params(candidate)
        if input_method == "ADDRESS"
        else build_coordinate_rent_estimate_params(candidate)
    )
    try:
        response = session.get(
            RENTCAST_RENT_AVM_URL,
            headers={"accept": "application/json", "X-Api-Key": api_key},
            params=params,
            timeout=(5, 30),
        )
    except requests.RequestException:
        raise ProviderRentError(
            f"RentCast request could not be completed for property_key {candidate.property_key}.",
            input_method=input_method,
            error_category="NETWORK_ERROR",
            retryable=True,
        ) from None

    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise ProviderRentError(
                "RentCast rejected the configured API credentials.",
                input_method=input_method,
                error_category="AUTHORIZATION",
                http_status=response.status_code,
                retryable=False,
                fatal_configuration_error=True,
            )
        if response.status_code in (400, 404, 422):
            raise ProviderRentError(
                f"RentCast could not produce an estimate for property_key {candidate.property_key} "
                f"(HTTP {response.status_code}).",
                input_method=input_method,
                error_category="NO_PROVIDER_ESTIMATE",
                http_status=response.status_code,
                retryable=False,
            )
        raise ProviderRentError(
            f"RentCast request failed for property_key {candidate.property_key} "
            f"with HTTP status {response.status_code}.",
            input_method=input_method,
            error_category="PROVIDER_TRANSIENT_ERROR",
            http_status=response.status_code,
            retryable=True,
        )

    try:
        payload = response.json()
    except ValueError:
        raise ProviderRentError(
            f"RentCast returned non-JSON data for property_key {candidate.property_key}.",
            input_method=input_method,
            error_category="INVALID_PROVIDER_PAYLOAD",
            retryable=False,
        ) from None
    if not isinstance(payload, dict):
        raise ProviderRentError(
            f"RentCast returned an unexpected response for property_key {candidate.property_key}.",
            input_method=input_method,
            error_category="INVALID_PROVIDER_PAYLOAD",
            retryable=False,
        )
    try:
        return RentEstimateResponse.model_validate(payload), payload
    except ValidationError:
        raise ProviderRentError(
            f"RentCast returned no usable rent estimate for property_key {candidate.property_key}.",
            input_method=input_method,
            error_category="INVALID_PROVIDER_PAYLOAD",
            retryable=False,
        ) from None


def get_rent_estimate(
    session: requests.Session,
    api_key: str,
    candidate: RentEstimateCandidate,
) -> tuple[RentEstimateResponse, dict[str, Any], Literal["ADDRESS", "COORDINATES"]]:
    """Use address first; spend one coordinate fallback only for address-format errors."""
    try:
        estimate, payload = _fetch_rent_estimate(
            session,
            api_key,
            candidate,
            input_method="ADDRESS",
        )
        return estimate, payload, "ADDRESS"
    except ProviderRentError as address_error:
        if (
            address_error.http_status not in (400, 422)
            or not can_use_coordinate_fallback(candidate)
        ):
            raise

    estimate, payload = _fetch_rent_estimate(
        session,
        api_key,
        candidate,
        input_method="COORDINATES",
    )
    return estimate, payload, "COORDINATES"


def payload_hash(payload: dict[str, Any]) -> str:
    """Create reproducible provenance without storing an unbounded provider response."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def scalar_out_value(value: object) -> int:
    """Normalize python-oracledb RETURNING values across driver versions."""
    if isinstance(value, list):
        value = value[0]
    return int(value)


def start_etl_run(cursor: oracledb.Cursor) -> int:
    """Create and return an auditable live rent-AVM run."""
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
    metrics: LiveRunMetrics,
    error_message: str | None = None,
) -> None:
    """Finalize the audit row with measured counts and a redacted fatal error."""
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
            "loaded_count": metrics.loaded_count,
            "error_message": error_message,
        },
    )


def insert_rent_estimate_snapshot(
    cursor: oracledb.Cursor,
    candidate: RentEstimateCandidate,
    run_id: int,
    estimate: RentEstimateResponse,
    raw_payload: dict[str, Any],
    input_method: Literal["ADDRESS", "COORDINATES"],
) -> None:
    """Insert one validated provider estimate with scope and provenance."""
    cursor.setinputsizes(
        property_key=int,
        run_id=int,
        estimator_source=str,
        estimate_scope=str,
        input_method=str,
        subject_attribute_mode=str,
        estimated_monthly_rent=float,
        rent_range_low=float,
        rent_range_high=float,
        comparable_count=int,
        source_payload_hash=str,
    )
    cursor.execute(
        """
        INSERT INTO re_rent_estimate_snapshot (
            property_key, run_id, estimator_source, estimate_scope, input_method,
            subject_attribute_mode,
            estimated_monthly_rent, rent_range_low, rent_range_high,
            comparable_count, source_payload_hash
        ) VALUES (
            :property_key, :run_id, :estimator_source, :estimate_scope, :input_method,
            :subject_attribute_mode,
            :estimated_monthly_rent, :rent_range_low, :rent_range_high,
            :comparable_count, :source_payload_hash
        )
        """,
        {
            "property_key": candidate.property_key,
            "run_id": run_id,
            "estimator_source": SOURCE_NAME,
            "estimate_scope": estimate_scope_for(candidate),
            "input_method": input_method,
            "subject_attribute_mode": subject_attribute_mode_for(candidate),
            "estimated_monthly_rent": estimate.rent,
            "rent_range_low": estimate.rent_range_low,
            "rent_range_high": estimate.rent_range_high,
            "comparable_count": len(estimate.comparables or []),
            "source_payload_hash": payload_hash(raw_payload),
        },
    )


def insert_attempt(
    cursor: oracledb.Cursor,
    candidate: RentEstimateCandidate,
    run_id: int,
    *,
    status: Literal["ESTIMATED", "UNAVAILABLE", "TRANSIENT_FAILURE"],
    input_method: Literal["ADDRESS", "COORDINATES"],
    provider_http_status: int | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    source_payload_hash: str | None = None,
) -> None:
    """Persist a success or failure outcome for deterministic backfill progress."""
    cursor.execute(
        """
        INSERT INTO re_rent_estimate_attempt (
            property_key, run_id, estimator_source, requested_scope, input_method,
            subject_attribute_mode, attempt_status, provider_http_status, error_category, error_message,
            source_payload_hash
        ) VALUES (
            :property_key, :run_id, :estimator_source, :requested_scope, :input_method,
            :subject_attribute_mode, :attempt_status, :provider_http_status, :error_category, :error_message,
            :source_payload_hash
        )
        """,
        {
            "property_key": candidate.property_key,
            "run_id": run_id,
            "estimator_source": SOURCE_NAME,
            "requested_scope": estimate_scope_for(candidate),
            "input_method": input_method,
            "subject_attribute_mode": subject_attribute_mode_for(candidate),
            "attempt_status": status,
            "provider_http_status": provider_http_status,
            "error_category": error_category,
            "error_message": error_message[:1000] if error_message else None,
            "source_payload_hash": source_payload_hash,
        },
    )


def run_dry_run(
    limit: int,
    *,
    coverage_backfill: bool = False,
    address_only_backfill: bool = False,
    property_type: str = PILOT_PROPERTY_TYPE,
) -> CandidateMetrics:
    """Log a read-only, capped cohort without API calls or Oracle writes."""
    selection = (
        "address-only listings with provider attribute lookup"
        if address_only_backfill
        else "all supported property types"
        if coverage_backfill
        else property_type
    )
    logger.info(
        "Starting rent-estimate dry run: selection=%s, limit=%s. "
        "No RentCast calls and no Oracle writes will occur.",
        selection,
        limit,
    )
    with managed_connection() as connection:
        with connection.cursor() as cursor:
            candidates, metrics = load_rent_candidates(
                cursor,
                limit,
                coverage_backfill=coverage_backfill,
                address_only_backfill=address_only_backfill,
                property_type=property_type,
            )

    for candidate in candidates:
        logger.info(
            "Candidate property_key=%s | type=%s | scope=%s | attributes=%s | address=%s",
            candidate.property_key,
            candidate.property_type,
            estimate_scope_for(candidate),
            subject_attribute_mode_for(candidate),
            candidate.street_address,
        )
    logger.info(
        "Rent-estimate dry run complete: selected=%s, validated=%s, rejected=%s. "
        "No RentCast calls and no Oracle writes occurred.",
        metrics.selected_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return metrics


def run_live_batch(
    limit: int,
    *,
    coverage_backfill: bool,
    address_only_backfill: bool,
    property_type: str,
    request_delay_seconds: float,
) -> int:
    """Enrich one small batch and persist a success/failure outcome per listing."""
    settings = get_settings()
    api_key = settings.rentcast_api_key
    if not api_key:
        raise ConfigurationError(
            "Missing required environment variable: RENTCAST_API_KEY. "
            "Add it to your local .env file or Codespaces secrets."
        )
    request_delay_seconds = validate_request_delay_seconds(request_delay_seconds)
    connection = get_connection()
    run_id: int | None = None
    metrics = LiveRunMetrics()

    try:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = start_etl_run(cursor)
            connection.commit()
            candidates, candidate_metrics = load_rent_candidates(
                cursor,
                limit,
                coverage_backfill=coverage_backfill,
                address_only_backfill=address_only_backfill,
                property_type=property_type,
            )
            metrics.extracted_count = candidate_metrics.selected_count
            metrics.rejected_count = candidate_metrics.rejected_count

            with requests.Session() as session:
                for index, candidate in enumerate(candidates):
                    try:
                        estimate, raw_payload, input_method = get_rent_estimate(
                            session,
                            api_key,
                            candidate,
                        )
                    except ProviderRentError as error:
                        if error.fatal_configuration_error:
                            raise ConfigurationError(str(error)) from None
                        metrics.rejected_count += 1
                        insert_attempt(
                            cursor,
                            candidate,
                            run_id,
                            status=(
                                "TRANSIENT_FAILURE" if error.retryable else "UNAVAILABLE"
                            ),
                            input_method=error.input_method,
                            provider_http_status=error.http_status,
                            error_category=error.error_category,
                            error_message=str(error),
                        )
                        logger.warning(
                            "No usable RentCast estimate for property_key=%s: %s",
                            candidate.property_key,
                            error,
                        )
                    else:
                        metrics.validated_count += 1
                        insert_rent_estimate_snapshot(
                            cursor,
                            candidate,
                            run_id,
                            estimate,
                            raw_payload,
                            input_method,
                        )
                        insert_attempt(
                            cursor,
                            candidate,
                            run_id,
                            status="ESTIMATED",
                            input_method=input_method,
                            source_payload_hash=payload_hash(raw_payload),
                        )
                        metrics.loaded_count += 1

                    if index < len(candidates) - 1 and request_delay_seconds > 0:
                        time.sleep(request_delay_seconds)

            run_status = "SUCCEEDED" if metrics.rejected_count == 0 else "PARTIAL"
            finish_etl_run(cursor, run_id, run_status, metrics)
            connection.commit()
            logger.info(
                "Live rent-estimate run %s completed: extracted=%s, validated=%s, "
                "rejected=%s, loaded=%s, status=%s.",
                run_id,
                metrics.extracted_count,
                metrics.validated_count,
                metrics.rejected_count,
                metrics.loaded_count,
                run_status,
            )
            return run_id
    except Exception as error:
        connection.rollback()
        if run_id is not None:
            with connection.cursor() as cursor:
                finish_etl_run(cursor, run_id, "FAILED", metrics, str(error))
            connection.commit()
        logger.exception("Live rent-estimate batch failed.")
        raise
    finally:
        connection.close()


def positive_int(value: str) -> int:
    """Argparse validator for a positive whole-number batch size."""
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a whole number.") from error
    if converted <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return converted


def nonnegative_float(value: str) -> float:
    """Argparse validator for request pacing."""
    try:
        return validate_request_delay_seconds(float(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    """Build dry-run and explicit-live controls for capped rent backfills."""
    parser = argparse.ArgumentParser(
        description="Validate or enrich small, tracked RentCast rent-estimate batches."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate candidates only; no API calls or Oracle writes.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Call RentCast and write validated estimates plus attempt outcomes.",
    )
    parser.add_argument(
        "--confirm-live-run",
        action="store_true",
        help="Required with --live to acknowledge API usage and database writes.",
    )
    parser.add_argument(
        "--coverage-backfill",
        action="store_true",
        help=(
            "Select missing estimates across every supported current property type. "
            "Without this flag, only --property-type is selected."
        ),
    )
    parser.add_argument(
        "--address-only-backfill",
        action="store_true",
        help=(
            "Select only listings missing one or more subject-property attributes. "
            "RentCast receives the address and performs its own subject-attribute lookup."
        ),
    )
    parser.add_argument(
        "--property-type",
        default=PILOT_PROPERTY_TYPE,
        help=(
            "Single property type used without --coverage-backfill "
            f"(default: {PILOT_PROPERTY_TYPE})."
        ),
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_PILOT_LIMIT,
        help=(
            f"Listings per batch (1-{MAX_PILOT_LIMIT}; default: %(default)s). "
            "Use repeated small batches for coverage backfill."
        ),
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=nonnegative_float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=(
            "Delay between live provider requests "
            f"(0-{MAX_REQUEST_DELAY_SECONDS:g}; default: %(default)s)."
        ),
    )
    return parser


def main() -> None:
    """Run a no-call check or a confirmed, capped RentCast enrichment batch."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_limit(args.limit)
        if args.coverage_backfill and args.address_only_backfill:
            parser.error("--coverage-backfill and --address-only-backfill cannot be combined.")
        if args.live:
            if not args.confirm_live_run:
                parser.error("--live requires --confirm-live-run.")
            run_live_batch(
                args.limit,
                coverage_backfill=args.coverage_backfill,
                address_only_backfill=args.address_only_backfill,
                property_type=args.property_type,
                request_delay_seconds=args.request_delay_seconds,
            )
        else:
            run_dry_run(
                args.limit,
                coverage_backfill=args.coverage_backfill,
                address_only_backfill=args.address_only_backfill,
                property_type=args.property_type,
            )
    except Exception:
        logger.exception("Rent-estimate pipeline failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()