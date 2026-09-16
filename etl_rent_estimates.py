"""Safely select and validate candidates for the RentCast rent-estimate pilot.

This command is intentionally dry-run only. It reads from the canonical
VW_RE_CURRENT_LISTING view, makes no HTTP requests, and writes no Oracle rows.
It is the safety gate before enabling the capped rent-AVM enrichment pipeline.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

import oracledb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from portfolio_dashboard.database import managed_connection


PILOT_PROPERTY_TYPE = "Townhouse"
DEFAULT_PILOT_LIMIT = 10
MAX_PILOT_LIMIT = 10
ESTIMATE_SCOPE = "PROPERTY"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("rent_estimate_dry_run.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("RentEstimateDryRun")


class RentEstimateCandidate(BaseModel):
    """A complete, address-based rent-AVM candidate from the canonical view."""

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


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts for one read-only pilot selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


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
        FROM vw_re_current_listing
        WHERE UPPER(property_type) = :property_type
          AND street_address IS NOT NULL
          AND city IS NOT NULL
          AND state_code IS NOT NULL
          AND zip_code IS NOT NULL
          AND bedrooms IS NOT NULL
          AND bathrooms IS NOT NULL
          AND structure_sqft > 0
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND asking_price > 0
        ORDER BY observed_at DESC, property_key
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
    """Reject accidental requests above the small, approved pilot cap."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


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
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected candidate property_key=%s: %s",
                payload.get("property_key"),
                error.errors(),
            )
            continue
        valid_candidates.append(candidate)

    return valid_candidates, rejected_count


def load_pilot_candidates(
    cursor: oracledb.Cursor,
    limit: int,
) -> tuple[list[RentEstimateCandidate], CandidateMetrics]:
    """Read and validate pilot candidates without calling an API or writing data."""
    limit = validate_limit(limit)
    cursor.execute(
        PILOT_CANDIDATE_QUERY,
        {"property_type": PILOT_PROPERTY_TYPE.upper(), "limit": limit},
    )
    rows = cursor.fetchall()
    candidates, rejected_count = validate_candidate_rows(rows)
    metrics = CandidateMetrics(
        selected_count=len(rows),
        validated_count=len(candidates),
        rejected_count=rejected_count,
    )
    return candidates, metrics


def run_dry_run(limit: int) -> CandidateMetrics:
    """Log a validated, read-only pilot cohort and return its data-quality metrics."""
    logger.info(
        "Starting dry run: property_type=%s, limit=%s, estimate_scope=%s. "
        "No RentCast calls and no Oracle writes will occur.",
        PILOT_PROPERTY_TYPE,
        limit,
        ESTIMATE_SCOPE,
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


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI with a mandatory safety switch for the pre-API phase."""
    parser = argparse.ArgumentParser(
        description="Validate a capped Townhouse rent-estimate pilot without API calls or writes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required safety switch; this version does not support live enrichment.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PILOT_LIMIT,
        help=f"Number of candidates to validate (1-{MAX_PILOT_LIMIT}; default: %(default)s).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        run_dry_run(args.limit)
    except Exception:
        logger.exception("Rent-estimate dry run failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()