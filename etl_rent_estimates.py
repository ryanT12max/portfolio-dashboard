"""Validate and enrich a tightly capped Townhouse rent-estimate pilot.

Dry-run mode is read-only. Live mode requires explicit confirmation and uses
one HTTP request per candidate, with no automatic retry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

import oracledb
import requests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.database import get_connection, managed_connection


PILOT_PROPERTY_TYPE = "Townhouse"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10

SOURCE_NAME = "RENTCAST_RENT_AVM"
ESTIMATE_SCOPE = "PROPERTY"
INPUT_METHOD = "ADDRESS"
RENTCAST_RENT_AVM_URL = "https://api.rentcast.io/v1/avm/rent/long-term"
RENTCAST_COMPARABLE_COUNT = 5


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
    """A complete, address-based AVM candidate from the canonical view."""

    model_config = ConfigDict(str_strip_whitespace=True)

    property_key: int = Field(gt=0)
    street_address: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state_code: str = Field(min_length=2, max_length=2)
    zip_code: str = Field(min_length=5, max_length=10)
    property_type: str = Field(min_length=1)
    bedrooms: float = Field(ge=0)
    bathrooms: float = Field(ge=0)
    structure_sqft: int = Field(gt=0)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    asking_price: float = Field(gt=0)
    observed_at: datetime


class RentEstimateResponse(BaseModel):
    """Validated subset of RentCast's long-term rent-AVM response."""

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


@dataclass(frozen=True)
class CandidateMetrics:
    selected_count: int
    validated_count: int
    rejected_count: int


@dataclass
class LiveRunMetrics:
    extracted_count: int = 0
    validated_count: int = 0
    rejected_count: int = 0
    loaded_count: int = 0


PILOT_CANDIDATE_QUERY = """
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
        WHERE UPPER(listing.property_type) = :property_type
          AND listing.street_address IS NOT NULL
          AND listing.city IS NOT NULL
          AND listing.state_code IS NOT NULL
          AND listing.zip_code IS NOT NULL
          AND listing.bedrooms IS NOT NULL
          AND listing.bathrooms IS NOT NULL
          AND listing.structure_sqft > 0
          AND listing.latitude IS NOT NULL
          AND listing.longitude IS NOT NULL
          AND listing.asking_price > 0
          AND NOT EXISTS (
              SELECT 1
              FROM re_rent_estimate_snapshot existing_estimate
              WHERE existing_estimate.property_key = listing.property_key
                AND existing_estimate.estimator_source = :estimator_source
                AND existing_estimate.estimate_scope = :estimate_scope
          )
        ORDER BY listing.observed_at DESC, listing.property_key
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
    """Reject accidental requests above the pilot cap."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def validate_candidate_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[list[RentEstimateCandidate], int]:
    """Validate database rows before any API request occurs."""
    candidates: list[RentEstimateCandidate] = []
    rejected_count = 0

    for row in rows:
        payload = dict(zip(CANDIDATE_COLUMNS, row, strict=True))

        try:
            candidates.append(RentEstimateCandidate.model_validate(payload))
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected candidate property_key=%s: %s",
                payload.get("property_key"),
                error.errors(),
            )

    return candidates, rejected_count


def load_pilot_candidates(
    cursor: oracledb.Cursor,
    limit: int,
) -> tuple[list[RentEstimateCandidate], CandidateMetrics]:
    """Read complete, not-yet-enriched Townhouse candidates."""
    limit = validate_limit(limit)

    cursor.execute(
        PILOT_CANDIDATE_QUERY,
        {
            "property_type": PILOT_PROPERTY_TYPE.upper(),
            "estimator_source": SOURCE_NAME,
            "estimate_scope": ESTIMATE_SCOPE,
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
    """Build the documented address-based RentCast AVM request."""
    return {
        "address": candidate.street_address,
        "propertyType": candidate.property_type,
        "bedrooms": candidate.bedrooms,
        "bathrooms": candidate.bathrooms,
        "squareFootage": candidate.structure_sqft,
        "compCount": RENTCAST_COMPARABLE_COUNT,
    }


def get_rent_estimate(
    session: requests.Session,
    api_key: str,
    candidate: RentEstimateCandidate,
) -> tuple[RentEstimateResponse, dict[str, Any]]:
    """Make exactly one API request and validate its response."""
    response = session.get(
        RENTCAST_RENT_AVM_URL,
        headers={"accept": "application/json", "X-Api-Key": api_key},
        params=build_rent_estimate_params(candidate),
        timeout=(5, 30),
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("RentCast returned an unexpected rent-AVM response format.")

    return RentEstimateResponse.model_validate(payload), payload


def payload_hash(payload: dict[str, Any]) -> str:
    """Store reproducible provenance without storing the full provider payload."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def scalar_out_value(value: object) -> int:
    """Normalize RETURNING values from python-oracledb."""
    if isinstance(value, list):
        value = value[0]
    return int(value)


def start_etl_run(cursor: oracledb.Cursor) -> int:
    """Create a durable audit row before making the external API call."""
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
    """Complete the audit record with final metrics."""
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
    property_key: int,
    run_id: int,
    estimate: RentEstimateResponse,
    raw_payload: dict[str, Any],
) -> None:
    """Insert one validated estimate in the canonical history table."""
    cursor.setinputsizes(
        property_key=int,
        run_id=int,
        estimator_source=str,
        estimate_scope=str,
        input_method=str,
        estimated_monthly_rent=float,
        rent_range_low=float,
        rent_range_high=float,
        comparable_count=int,
        source_payload_hash=str,
    )

    cursor.execute(
        """
        INSERT INTO re_rent_estimate_snapshot (
            property_key,
            run_id,
            estimator_source,
            estimate_scope,
            input_method,
            estimated_monthly_rent,
            rent_range_low,
            rent_range_high,
            comparable_count,
            source_payload_hash
        )
        VALUES (
            :property_key,
            :run_id,
            :estimator_source,
            :estimate_scope,
            :input_method,
            :estimated_monthly_rent,
            :rent_range_low,
            :rent_range_high,
            :comparable_count,
            :source_payload_hash
        )
        """,
        {
            "property_key": property_key,
            "run_id": run_id,
            "estimator_source": SOURCE_NAME,
            "estimate_scope": ESTIMATE_SCOPE,
            "input_method": INPUT_METHOD,
            "estimated_monthly_rent": estimate.rent,
            "rent_range_low": estimate.rent_range_low,
            "rent_range_high": estimate.rent_range_high,
            "comparable_count": len(estimate.comparables or []),
            "source_payload_hash": payload_hash(raw_payload),
        },
    )


def run_dry_run(limit: int) -> CandidateMetrics:
    """Validate candidates without calling RentCast or writing Oracle rows."""
    logger.info(
        "Starting dry run: property_type=%s, limit=%s. "
        "No RentCast calls and no Oracle writes will occur.",
        PILOT_PROPERTY_TYPE,
        limit,
    )

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            candidates, metrics = load_pilot_candidates(cursor, limit)

    for candidate in candidates:
        logger.info(
            "Candidate property_key=%s | address=%s | beds=%s | baths=%s | "
            "sqft=%s | asking_price=%.2f | scope=%s",
            candidate.property_key,
            candidate.street_address,
            candidate.bedrooms,
            candidate.bathrooms,
            candidate.structure_sqft,
            candidate.asking_price,
            ESTIMATE_SCOPE,
        )

    logger.info(
        "Dry run complete: selected=%s, validated=%s, rejected=%s. "
        "No RentCast calls and no Oracle writes occurred.",
        metrics.selected_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return metrics


def run_live_pilot(limit: int) -> int:
    """Run a deliberately capped and auditable live AVM enrichment."""
    api_key = get_settings().rentcast_api_key
    if not api_key:
        raise ConfigurationError(
            "Missing required environment variable: RENTCAST_API_KEY. "
            "Add it to your local .env file or Codespaces secrets."
        )

    connection = get_connection()
    run_id: int | None = None
    metrics = LiveRunMetrics()

    try:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = start_etl_run(cursor)
            connection.commit()

            candidates, candidate_metrics = load_pilot_candidates(cursor, limit)
            metrics.extracted_count = candidate_metrics.selected_count
            metrics.rejected_count = candidate_metrics.rejected_count

            # No retry adapter: one candidate produces one outgoing request.
            with requests.Session() as session:
                for candidate in candidates:
                    try:
                        estimate, raw_payload = get_rent_estimate(
                            session,
                            api_key,
                            candidate,
                        )
                    except (
                        requests.RequestException,
                        ValidationError,
                        ValueError,
                        RuntimeError,
                    ) as error:
                        metrics.rejected_count += 1
                        logger.warning(
                            "Rejected rent estimate for property_key=%s: %s",
                            candidate.property_key,
                            error,
                        )
                        continue

                    metrics.validated_count += 1
                    insert_rent_estimate_snapshot(
                        cursor,
                        candidate.property_key,
                        run_id,
                        estimate,
                        raw_payload,
                    )
                    metrics.loaded_count += 1

            run_status = "SUCCEEDED" if metrics.rejected_count == 0 else "PARTIAL"
            finish_etl_run(cursor, run_id, run_status, metrics)
            connection.commit()

            logger.info(
                "Live rent-estimate run %s completed: loaded=%s, status=%s.",
                run_id,
                metrics.loaded_count,
                run_status,
            )
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
                    str(error)[:1000],
                )
            connection.commit()

        logger.exception("Live rent-estimate pilot failed.")
        raise
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    """Create a CLI that requires explicit live-run confirmation."""
    parser = argparse.ArgumentParser(
        description="Validate or enrich a capped Townhouse rent-estimate pilot."
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
        help="Call RentCast and write validated estimates.",
    )
    parser.add_argument(
        "--confirm-live-run",
        action="store_true",
        help="Required with --live to acknowledge API usage and database writes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PILOT_LIMIT,
        help=f"Candidates to process (1-{MAX_PILOT_LIMIT}; default: %(default)s).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        validate_limit(args.limit)

        if args.live:
            if not args.confirm_live_run:
                parser.error("--live requires --confirm-live-run.")
            run_live_pilot(args.limit)
        else:
            run_dry_run(args.limit)
    except Exception:
        logger.exception("Rent-estimate pipeline failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()