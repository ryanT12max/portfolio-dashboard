"""Validate or refresh canonical market-quote snapshots from Finnhub.

Dry-run mode reads and validates canonical securities only. Live mode requires
an explicit confirmation, fetches current Finnhub quotes, validates them, and
inserts immutable snapshots into PORTFOLIO_APP.MK_QUOTE_SNAPSHOT. The explicit
daily-refresh mode processes the complete eligible watchlist, up to a fixed
safety cap, and can verify quote coverage and freshness after loading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

import oracledb
import requests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from check_market_data_quality import print_report, run_market_quality_check
from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.data_quality import overall_status
from portfolio_dashboard.database import managed_connection
from portfolio_dashboard.market_data_quality import MarketQualityThresholds


FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
QUOTE_SOURCE = "FINNHUB_QUOTE"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10
DAILY_REFRESH_LIMIT = 100
DEFAULT_REQUEST_DELAY_SECONDS = 0.25
MAX_REQUEST_DELAY_SECONDS = 5.0
DAILY_QUOTE_MAX_AGE_HOURS = 30.0


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("market_quote_pipeline.log"),
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


class FinnhubQuotePayload(BaseModel):
    """The fields required from a Finnhub current-quote response."""

    model_config = ConfigDict(extra="ignore")

    current_price: float = Field(alias="c", gt=0)
    change_amount: float | None = Field(default=None, alias="d")
    change_pct: float | None = Field(default=None, alias="dp")
    day_high_price: float | None = Field(default=None, alias="h", gt=0)
    day_low_price: float | None = Field(default=None, alias="l", gt=0)
    day_open_price: float | None = Field(default=None, alias="o", gt=0)
    prior_close_price: float | None = Field(default=None, alias="pc", gt=0)
    quote_timestamp: int | None = Field(default=None, alias="t", gt=0)

    @model_validator(mode="after")
    def validate_day_range(self) -> "FinnhubQuotePayload":
        """Reject a provider payload whose daily low is above the daily high."""
        if (
            self.day_low_price is not None
            and self.day_high_price is not None
            and self.day_low_price > self.day_high_price
        ):
            raise ValueError("Finnhub quote day_low_price cannot exceed day_high_price.")
        return self


class QuoteSnapshotRecord(BaseModel):
    """A validated market quote ready for historical snapshot insertion."""

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    provider_quote_at: datetime | None
    current_price: float = Field(gt=0)
    prior_close_price: float | None = Field(default=None, gt=0)
    change_amount: float | None = None
    change_pct: float | None = None
    day_open_price: float | None = Field(default=None, gt=0)
    day_high_price: float | None = Field(default=None, gt=0)
    day_low_price: float | None = Field(default=None, gt=0)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_day_range(self) -> "QuoteSnapshotRecord":
        """Keep the application contract aligned with the database constraint."""
        if (
            self.day_low_price is not None
            and self.day_high_price is not None
            and self.day_low_price > self.day_high_price
        ):
            raise ValueError("day_low_price cannot exceed day_high_price.")
        return self


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts from a canonical quote-pilot selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


@dataclass(frozen=True)
class PipelineMetrics:
    """Measured outcomes from one market-quote enrichment run."""

    extracted_count: int
    validated_count: int
    rejected_count: int
    loaded_count: int


QUOTE_CANDIDATE_QUERY = """
    SELECT
        security_key,
        ticker,
        sector
    FROM (
        SELECT
            security.security_key,
            security.ticker,
            security.sector
        FROM mk_security security
        WHERE security.ticker IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM mk_quote_snapshot snapshot
              JOIN mk_etl_run run_audit
                  ON run_audit.run_id = snapshot.run_id
                 AND run_audit.run_status IN ('SUCCEEDED', 'PARTIAL')
              WHERE snapshot.security_key = security.security_key
                AND TRUNC(CAST(snapshot.observed_at AT TIME ZONE 'UTC' AS DATE))
                    = TRUNC(CAST(SYSTIMESTAMP AT TIME ZONE 'UTC' AS DATE))
          )
        ORDER BY security.ticker
    )
    WHERE ROWNUM <= :limit
"""

CANDIDATE_COLUMNS = ("security_key", "ticker", "sector")


def validate_limit(value: int) -> int:
    """Bound one refresh run to the fixed API-usage safety cap."""
    if not 1 <= value <= DAILY_REFRESH_LIMIT:
        raise ValueError(
            f"Refresh limit must be between 1 and {DAILY_REFRESH_LIMIT}; received {value}."
        )
    return value


def validate_pilot_limit(value: int) -> int:
    """Keep the manual pilot mode deliberately small."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def resolve_refresh_limit(
    daily_refresh: bool,
    requested_limit: int | None,
) -> int:
    """Select either a small pilot or the fixed all-watchlist daily cap."""
    if daily_refresh:
        if requested_limit is not None:
            raise ValueError("--daily-refresh cannot be combined with --limit.")
        return DAILY_REFRESH_LIMIT

    return validate_pilot_limit(requested_limit or DEFAULT_PILOT_LIMIT)


def validate_request_delay_seconds(value: float) -> float:
    """Keep voluntary inter-request pacing within a practical safety range."""
    if not 0 <= value <= MAX_REQUEST_DELAY_SECONDS:
        raise ValueError(
            "Request delay must be between 0 and "
            f"{MAX_REQUEST_DELAY_SECONDS:g} seconds; received {value}."
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
    """Read unquoted-today candidates without a provider call or database write."""
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


def build_http_session() -> requests.Session:
    """Create a retrying session for transient provider failures only."""
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_quote_payload(
    session: requests.Session,
    ticker: str,
    api_key: str,
) -> FinnhubQuotePayload:
    """Fetch a Finnhub quote without exposing the API key in errors or logs."""
    try:
        response = session.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": ticker, "token": api_key},
            headers={"accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException:
        # A Requests exception can include the full URL and token parameter.
        raise RuntimeError(
            f"Finnhub quote request could not be completed for ticker {ticker}."
        ) from None

    if response.status_code >= 400:
        # Do not use response.raise_for_status(); its error message includes the URL.
        raise RuntimeError(
            f"Finnhub quote request failed for ticker {ticker} "
            f"with HTTP status {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"Finnhub returned non-JSON data for ticker {ticker}.") from error

    if not isinstance(payload, dict):
        raise RuntimeError(f"Finnhub returned an unexpected payload for ticker {ticker}.")

    try:
        return FinnhubQuotePayload.model_validate(payload)
    except ValidationError as error:
        raise RuntimeError(
            f"Finnhub quote payload failed validation for ticker {ticker}: "
            f"{error.errors()}"
        ) from error


def build_quote_snapshot(
    candidate: QuoteCandidate,
    quote_payload: FinnhubQuotePayload,
) -> QuoteSnapshotRecord:
    """Convert one validated provider quote into a canonical snapshot record."""
    provider_quote_at: datetime | None = None

    if quote_payload.quote_timestamp is not None:
        try:
            provider_quote_at = datetime.fromtimestamp(
                quote_payload.quote_timestamp,
                tz=timezone.utc,
            )
        except (OSError, OverflowError, ValueError):
            raise RuntimeError(
                f"Finnhub quote timestamp was invalid for ticker {candidate.ticker}."
            ) from None

    normalized_payload = json.dumps(
        quote_payload.model_dump(by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()

    try:
        return QuoteSnapshotRecord.model_validate(
            {
                "security_key": candidate.security_key,
                "ticker": candidate.ticker,
                "provider_quote_at": provider_quote_at,
                "current_price": quote_payload.current_price,
                "prior_close_price": quote_payload.prior_close_price,
                "change_amount": quote_payload.change_amount,
                "change_pct": quote_payload.change_pct,
                "day_open_price": quote_payload.day_open_price,
                "day_high_price": quote_payload.day_high_price,
                "day_low_price": quote_payload.day_low_price,
                "source_payload_hash": payload_hash,
            }
        )
    except ValidationError as error:
        raise RuntimeError(
            f"Finnhub quote record failed validation for ticker {candidate.ticker}: "
            f"{error.errors()}"
        ) from error


def _returned_number(value: object) -> int:
    """Normalize python-oracledb scalar or one-element DML RETURNING values."""
    if isinstance(value, list):
        if len(value) != 1:
            raise RuntimeError("Oracle did not return exactly one market quote run ID.")
        value = value[0]
    if value is None:
        raise RuntimeError("Oracle did not return a market quote run ID.")
    return int(value)


def create_run(cursor: oracledb.Cursor) -> int:
    """Create a RUNNING audit record and return its generated run ID."""
    run_id_var = cursor.var(oracledb.DB_TYPE_NUMBER)
    cursor.execute(
        """
        INSERT INTO mk_etl_run (source_name, run_status)
        VALUES (:source_name, 'RUNNING')
        RETURNING run_id INTO :run_id
        """,
        {"source_name": QUOTE_SOURCE, "run_id": run_id_var},
    )
    return _returned_number(run_id_var.getvalue())


def insert_quote_snapshot(
    cursor: oracledb.Cursor,
    record: QuoteSnapshotRecord,
    run_id: int,
) -> None:
    """Insert one validated quote as immutable historical snapshot data."""
    cursor.execute(
        """
        INSERT INTO mk_quote_snapshot (
            security_key,
            run_id,
            provider_quote_at,
            source_name,
            current_price,
            prior_close_price,
            change_amount,
            change_pct,
            day_open_price,
            day_high_price,
            day_low_price,
            source_payload_hash
        )
        VALUES (
            :security_key,
            :run_id,
            :provider_quote_at,
            :source_name,
            :current_price,
            :prior_close_price,
            :change_amount,
            :change_pct,
            :day_open_price,
            :day_high_price,
            :day_low_price,
            :source_payload_hash
        )
        """,
        {
            "security_key": record.security_key,
            "run_id": run_id,
            "provider_quote_at": record.provider_quote_at,
            "source_name": QUOTE_SOURCE,
            "current_price": record.current_price,
            "prior_close_price": record.prior_close_price,
            "change_amount": record.change_amount,
            "change_pct": record.change_pct,
            "day_open_price": record.day_open_price,
            "day_high_price": record.day_high_price,
            "day_low_price": record.day_low_price,
            "source_payload_hash": record.source_payload_hash,
        },
    )


def complete_run(
    cursor: oracledb.Cursor,
    run_id: int,
    metrics: PipelineMetrics,
) -> None:
    """Mark an audit record as successfully completed with measured counts."""
    cursor.execute(
        """
        UPDATE mk_etl_run
        SET
            completed_at = SYSTIMESTAMP,
            run_status = 'SUCCEEDED',
            extracted_count = :extracted_count,
            validated_count = :validated_count,
            rejected_count = :rejected_count,
            loaded_count = :loaded_count,
            error_message = NULL
        WHERE run_id = :run_id
        """,
        {
            "run_id": run_id,
            "extracted_count": metrics.extracted_count,
            "validated_count": metrics.validated_count,
            "rejected_count": metrics.rejected_count,
            "loaded_count": metrics.loaded_count,
        },
    )


def fail_run(
    cursor: oracledb.Cursor,
    run_id: int,
    error_message: str,
) -> None:
    """Persist a failed audit status after the quote transaction rolls back."""
    cursor.execute(
        """
        UPDATE mk_etl_run
        SET
            completed_at = SYSTIMESTAMP,
            run_status = 'FAILED',
            error_message = SUBSTR(:error_message, 1, 1000)
        WHERE run_id = :run_id
        """,
        {"run_id": run_id, "error_message": error_message},
    )


def _require_finnhub_api_key() -> str:
    """Read the provider key without exposing it in logs or console output."""
    api_key = get_settings().finnhub_api_key
    if not api_key:
        raise ConfigurationError(
            "Missing FINNHUB_API_KEY. Add it to your local .env file or Codespaces "
            "secrets before enabling live market quote enrichment."
        )
    return api_key


def run_dry_run(limit: int) -> CandidateMetrics:
    """Log a read-only validated cohort without calling Finnhub or writing Oracle rows."""
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


def run_live_refresh(
    limit: int,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
) -> PipelineMetrics:
    """Fetch and insert confirmed quote snapshots for eligible securities."""
    limit = validate_limit(limit)
    request_delay_seconds = validate_request_delay_seconds(request_delay_seconds)
    api_key = _require_finnhub_api_key()
    session = build_http_session()

    logger.info(
        "Starting live market-quote refresh: source=%s, limit=%s, request_delay_seconds=%.2f.",
        QUOTE_SOURCE,
        limit,
        request_delay_seconds,
    )

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = create_run(cursor)
            connection.commit()

            try:
                candidates, candidate_metrics = load_quote_candidates(cursor, limit)
                extracted_count = 0
                validated_count = 0
                rejected_count = candidate_metrics.rejected_count
                loaded_count = 0

                for index, candidate in enumerate(candidates):
                    quote_payload = fetch_quote_payload(
                        session=session,
                        ticker=candidate.ticker,
                        api_key=api_key,
                    )
                    extracted_count += 1
                    quote_snapshot = build_quote_snapshot(candidate, quote_payload)
                    validated_count += 1
                    insert_quote_snapshot(cursor, quote_snapshot, run_id)
                    loaded_count += 1

                    if index < len(candidates) - 1 and request_delay_seconds > 0:
                        time.sleep(request_delay_seconds)

                if not candidates:
                    logger.info(
                        "No eligible securities require a quote refresh today. "
                        "The run will complete successfully as an idempotent no-op."
                    )

                metrics = PipelineMetrics(
                    extracted_count=extracted_count,
                    validated_count=validated_count,
                    rejected_count=rejected_count,
                    loaded_count=loaded_count,
                )
                complete_run(cursor, run_id, metrics)
                connection.commit()
            except Exception as error:
                connection.rollback()
                fail_run(cursor, run_id, str(error))
                connection.commit()
                logger.exception("Live market-quote refresh run %s failed.", run_id)
                raise

    logger.info(
        "Live market-quote refresh run %s completed: extracted=%s, validated=%s, "
        "rejected=%s, loaded=%s.",
        run_id,
        metrics.extracted_count,
        metrics.validated_count,
        metrics.rejected_count,
        metrics.loaded_count,
    )
    return metrics


def verify_quote_refresh_quality() -> None:
    """Run the read-only daily quote gate after a successful refresh."""
    thresholds = MarketQualityThresholds(
        max_current_quote_age_hours=DAILY_QUOTE_MAX_AGE_HOURS,
    )
    summary, issues = run_market_quality_check(
        thresholds,
        scope="quote-refresh",
    )
    print_report(summary, issues, scope="quote-refresh")

    if overall_status(issues) == "FAIL":
        raise RuntimeError(
            "Market quote refresh completed, but the quote-refresh quality gate failed."
        )

    logger.info(
        "Quote-refresh quality gate passed: coverage=%s/%s.",
        summary.securities_with_current_quote,
        summary.total_watchlist_securities,
    )


def positive_int(value: str) -> int:
    """Argparse validator for a whole number greater than zero."""
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a whole number.") from error
    if converted <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return converted


def nonnegative_float(value: str) -> float:
    """Argparse validator for a non-negative request pacing value."""
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a number.") from error

    try:
        return validate_request_delay_seconds(converted)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    """Build dry-run, pilot, and daily-refresh controls for quote enrichment."""
    parser = argparse.ArgumentParser(
        description="Validate or refresh canonical market-quote snapshots."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate canonical securities only; no API calls or Oracle writes.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Call Finnhub and insert validated market-quote snapshots into Oracle.",
    )
    parser.add_argument(
        "--confirm-live-run",
        action="store_true",
        help="Required with --live to acknowledge API usage and Oracle writes.",
    )
    parser.add_argument(
        "--daily-refresh",
        action="store_true",
        help=(
            "Process every security not successfully quoted today, up to the fixed "
            f"{DAILY_REFRESH_LIMIT}-security safety cap. Cannot be combined with --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help=(
            f"Small pilot size (1-{MAX_PILOT_LIMIT}; default: {DEFAULT_PILOT_LIMIT}). "
            "Do not use with --daily-refresh."
        ),
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=nonnegative_float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=(
            "Delay between provider requests during live refreshes "
            f"(0-{MAX_REQUEST_DELAY_SECONDS:g}; default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--verify-quote-quality",
        action="store_true",
        help=(
            "After a live refresh, run the read-only quote coverage and freshness gate "
            f"using a {DAILY_QUOTE_MAX_AGE_HOURS:g}-hour freshness threshold."
        ),
    )
    return parser


def main() -> None:
    """Run a no-call gate, small pilot, or confirmed daily quote refresh."""
    parser = build_parser()
    args = parser.parse_args()

    try:
        limit = resolve_refresh_limit(args.daily_refresh, args.limit)
    except ValueError as error:
        parser.error(str(error))

    try:
        if args.dry_run:
            if args.verify_quote_quality:
                parser.error("--verify-quote-quality requires --live.")
            run_dry_run(limit)
            return

        if not args.confirm_live_run:
            parser.error("--live requires --confirm-live-run.")

        run_live_refresh(limit, args.request_delay_seconds)
        if args.verify_quote_quality:
            verify_quote_refresh_quality()
    except Exception:
        logger.exception("Market-quote pipeline failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
