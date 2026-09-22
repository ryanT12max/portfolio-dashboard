"""Validate a capped canonical market-quote pilot without provider calls.

This first phase is intentionally dry-run only. It reads candidate securities
from PORTFOLIO_APP.MK_SECURITY, validates them, and writes no Oracle quote rows.
It is the safety gate before enabled Finnhub quote enrichment.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

import oracledb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from portfolio_dashboard.database import managed_connection


QUOTE_SOURCE = "FINNHUB_QUOTE"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("market_quote_dry_run.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("MarketQuoteETL")


class QuoteCandidate(BaseModel):
    """A valid canonical security eligible for a market-quote request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    sector: str | None = Field(default=None, max_length=50)


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts from a read-only quote-pilot selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


QUOTE_CANDIDATE_QUERY = """
    SELECT
        security_key,
        ticker,
        sector
    FROM (
        SELECT
            security_key,
            ticker,
            sector
        FROM mk_security
        WHERE ticker IS NOT NULL
        ORDER BY ticker
    )
    WHERE ROWNUM <= :limit
"""

CANDIDATE_COLUMNS = ("security_key", "ticker", "sector")


def validate_limit(value: int) -> int:
    """Bound the pilot to a small, deliberate number of provider requests."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def validate_candidate_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[list[QuoteCandidate], int]:
    """Apply the quote-candidate contract and count rejected database rows."""
    valid_candidates: list[QuoteCandidate] = []
    rejected_count = 0

    for row in rows:
        payload = dict(zip(CANDIDATE_COLUMNS, row, strict=True))
        try:
            candidate = QuoteCandidate.model_validate(payload)
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected quote candidate security_key=%s: %s",
                payload.get("security_key"),
                error.errors(),
            )
            continue
        valid_candidates.append(candidate)

    return valid_candidates, rejected_count


def load_quote_candidates(
    cursor: oracledb.Cursor,
    limit: int,
) -> tuple[list[QuoteCandidate], CandidateMetrics]:
    """Read and validate candidate securities without a provider call or write."""
    limit = validate_limit(limit)
    cursor.execute(QUOTE_CANDIDATE_QUERY, {"limit": limit})
    rows = cursor.fetchall()
    candidates, rejected_count = validate_candidate_rows(rows)
    metrics = CandidateMetrics(
        selected_count=len(rows),
        validated_count=len(candidates),
        rejected_count=rejected_count,
    )
    return candidates, metrics


def run_dry_run(limit: int) -> CandidateMetrics:
    """Log the validated pilot cohort without calling Finnhub or writing Oracle rows."""
    logger.info(
        "Starting market-quote dry run: source=%s, limit=%s. "
        "No Finnhub calls and no Oracle writes will occur.",
        QUOTE_SOURCE,
        limit,
    )

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            candidates, metrics = load_quote_candidates(cursor, limit)

    for candidate in candidates:
        logger.info(
            "Candidate security_key=%s | ticker=%s | sector=%s",
            candidate.security_key,
            candidate.ticker,
            candidate.sector or "Unknown",
        )

    logger.info(
        "Dry run complete: selected=%s, validated=%s, rejected=%s. "
        "No Finnhub calls and no Oracle writes occurred.",
        metrics.selected_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return metrics


def build_parser() -> argparse.ArgumentParser:
    """Build a CLI that requires the explicit pre-provider safety switch."""
    parser = argparse.ArgumentParser(
        description="Validate a capped canonical market-quote pilot without API calls or writes."
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
        help=f"Securities to validate (1-{MAX_PILOT_LIMIT}; default: %(default)s).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        run_dry_run(args.limit)
    except Exception:
        logger.exception("Market-quote dry run failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()