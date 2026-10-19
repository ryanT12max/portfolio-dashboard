"""Load provider-attributed market fundamentals into the canonical Oracle model.

Provider precedence is deliberate: Finnhub Basic Financials is attempted first.
When that endpoint is unavailable, incomplete, or returns no usable metrics,
yfinance is used as a personal-research fallback. Every stored snapshot keeps
its actual source, retrieval timestamp, provider reporting date when supplied,
and a provenance hash. Missing values stay NULL; this pipeline never fills
unknown values with zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import oracledb
import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from check_market_data_quality import print_report, run_market_quality_check
from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.data_quality import overall_status
from portfolio_dashboard.database import managed_connection
from portfolio_dashboard.market_data_quality import MarketQualityThresholds


FINNHUB_METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
FINNHUB_SOURCE = "FINNHUB_BASIC_FINANCIALS"
YFINANCE_SOURCE = "YFINANCE_FUNDAMENTALS"
RUN_SOURCE = "FUNDAMENTALS_ENRICHMENT"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10
ALL_WATCHLIST_LIMIT = 100
DEFAULT_REQUEST_DELAY_SECONDS = 0.5
MAX_REQUEST_DELAY_SECONDS = 5.0
FINNHUB_REQUIRED_METRICS = (
    "pe_ratio",
    "pb_ratio",
    "earnings_per_share",
    "return_on_equity_pct",
    "profit_margin_pct",
    "debt_to_equity_ratio",
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("market_fundamentals_pipeline.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("MarketFundamentalsETL")


class FundamentalCandidate(BaseModel):
    """A canonical security eligible for a fundamentals provider request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    sector: str | None = Field(default=None, max_length=50)


class FundamentalSnapshotRecord(BaseModel):
    """A validated, source-attributed row ready for MK_FUNDAMENTAL_SNAPSHOT."""

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    source_name: str = Field(min_length=1, max_length=50)
    source_as_of_date: date | None = None
    market_price: float | None = Field(default=None, gt=0)
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    pb_ratio: float | None = None
    earnings_per_share: float | None = None
    return_on_equity_pct: float | None = None
    profit_margin_pct: float | None = None
    debt_to_equity_ratio: float | None = None
    free_cash_flow_value: float | None = None
    free_cash_flow_unit: str | None = Field(default=None, max_length=30)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_at_least_one_fundamental(self) -> "FundamentalSnapshotRecord":
        fields = (
            self.pe_ratio,
            self.peg_ratio,
            self.pb_ratio,
            self.earnings_per_share,
            self.return_on_equity_pct,
            self.profit_margin_pct,
            self.debt_to_equity_ratio,
            self.free_cash_flow_value,
        )
        if all(value is None for value in fields):
            raise ValueError("Provider payload contains no usable fundamental metrics.")
        return self


class ProviderFundamentalError(RuntimeError):
    """A provider failure safe for logs and the canonical run-audit table."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        category: str,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.category = category
        self.http_status = http_status


@dataclass(frozen=True)
class CandidateMetrics:
    """Counts from a no-call candidate selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


@dataclass(frozen=True)
class PipelineMetrics:
    """Measured outcomes from one fundamentals live run."""

    extracted_count: int
    validated_count: int
    rejected_count: int
    loaded_count: int


FUNDAMENTAL_CANDIDATE_QUERY = """
    SELECT security_key, ticker, sector
    FROM (
        SELECT security.security_key, security.ticker, security.sector
        FROM mk_security security
        WHERE security.ticker IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM mk_fundamental_snapshot snapshot
              JOIN mk_etl_run run_audit
                  ON run_audit.run_id = snapshot.run_id
                 AND run_audit.source_name = :run_source
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


def validate_pilot_limit(value: int) -> int:
    """Keep manual live pilots at ten securities or fewer."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def resolve_refresh_limit(all_watchlist: bool, requested_limit: int | None) -> int:
    """Choose a small pilot or the fixed all-watchlist safety cap."""
    if all_watchlist:
        if requested_limit is not None:
            raise ValueError("--all-watchlist cannot be combined with --limit.")
        return ALL_WATCHLIST_LIMIT
    return validate_pilot_limit(requested_limit or DEFAULT_PILOT_LIMIT)


def validate_request_delay_seconds(value: float) -> float:
    """Keep voluntary inter-security pacing within a practical range."""
    if not 0 <= value <= MAX_REQUEST_DELAY_SECONDS:
        raise ValueError(
            "Request delay must be between 0 and "
            f"{MAX_REQUEST_DELAY_SECONDS:g} seconds; received {value}."
        )
    return value


def validate_candidate_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[list[FundamentalCandidate], int]:
    """Validate database rows before any provider request is made."""
    candidates: list[FundamentalCandidate] = []
    rejected_count = 0
    for row in rows:
        payload = dict(zip(CANDIDATE_COLUMNS, row, strict=True))
        try:
            candidates.append(FundamentalCandidate.model_validate(payload))
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected fundamentals candidate security_key=%s: %s",
                payload.get("security_key"),
                error.errors(),
            )
    return candidates, rejected_count


def load_fundamental_candidates(
    cursor: oracledb.Cursor,
    limit: int,
) -> tuple[list[FundamentalCandidate], CandidateMetrics]:
    """Read canonical securities only; this function never calls a provider."""
    cursor.execute(
        FUNDAMENTAL_CANDIDATE_QUERY,
        {"limit": limit, "run_source": RUN_SOURCE},
    )
    rows = cursor.fetchall()
    candidates, rejected_count = validate_candidate_rows(rows)
    return candidates, CandidateMetrics(
        selected_count=len(rows),
        validated_count=len(candidates),
        rejected_count=rejected_count,
    )


def build_http_session() -> requests.Session:
    """Retry transient Finnhub transport failures without exposing credentials."""
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


def _require_finnhub_api_key() -> str:
    api_key = get_settings().finnhub_api_key
    if not api_key:
        raise ConfigurationError(
            "Missing required environment variable: FINNHUB_API_KEY. "
            "Add it to your local .env file or Codespaces secrets."
        )
    return api_key


def fetch_finnhub_metric_payload(
    session: requests.Session,
    ticker: str,
    api_key: str,
) -> dict[str, Any]:
    """Fetch a Finnhub metric payload without allowing a token into errors."""
    try:
        response = session.get(
            FINNHUB_METRIC_URL,
            params={"symbol": ticker, "metric": "all", "token": api_key},
            headers={"accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException:
        raise ProviderFundamentalError(
            f"Finnhub fundamentals request could not be completed for ticker {ticker}.",
            provider=FINNHUB_SOURCE,
            category="NETWORK_ERROR",
        ) from None
    if response.status_code >= 400:
        raise ProviderFundamentalError(
            f"Finnhub fundamentals request failed for ticker {ticker} "
            f"with HTTP status {response.status_code}.",
            provider=FINNHUB_SOURCE,
            category="HTTP_ERROR",
            http_status=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError:
        raise ProviderFundamentalError(
            f"Finnhub returned non-JSON fundamentals data for ticker {ticker}.",
            provider=FINNHUB_SOURCE,
            category="INVALID_PAYLOAD",
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("metric"), dict):
        raise ProviderFundamentalError(
            f"Finnhub returned no metric object for ticker {ticker}.",
            provider=FINNHUB_SOURCE,
            category="INVALID_PAYLOAD",
        )
    return payload


def _number_or_none(value: object) -> float | None:
    """Accept finite provider numbers only; preserve unknown values as NULL."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(
    values: Mapping[str, Any],
    candidate_keys: Sequence[str],
) -> float | None:
    """Select the first non-null metric from explicitly documented aliases."""
    for key in candidate_keys:
        number = _number_or_none(values.get(key))
        if number is not None:
            return number
    return None


def _parse_provider_date(value: object) -> date | None:
    """Parse only explicit provider reporting-date fields; never infer a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash normalized source input without storing unbounded provider payloads."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_finnhub_record(
    candidate: FundamentalCandidate,
    payload: Mapping[str, Any],
) -> FundamentalSnapshotRecord:
    """Map Finnhub metric names into the canonical, source-attributed contract."""
    metric = payload.get("metric")
    if not isinstance(metric, Mapping):
        raise ProviderFundamentalError(
            f"Finnhub returned no metric object for ticker {candidate.ticker}.",
            provider=FINNHUB_SOURCE,
            category="INVALID_PAYLOAD",
        )

    try:
        return FundamentalSnapshotRecord.model_validate(
            {
                "security_key": candidate.security_key,
                "ticker": candidate.ticker,
                "source_name": FINNHUB_SOURCE,
                "source_as_of_date": _parse_provider_date(
                    payload.get("asOfDate") or payload.get("date")
                ),
                "pe_ratio": _first_number(
                    metric, ("peBasicExclExtraTTM", "peTTM", "peAnnual")
                ),
                "peg_ratio": _first_number(metric, ("pegTTM", "pegAnnual")),
                "pb_ratio": _first_number(metric, ("pbQuarterly", "pbAnnual")),
                "earnings_per_share": _first_number(
                    metric,
                    ("epsBasicExclExtraItemsTTM", "epsTTM", "epsAnnual"),
                ),
                "return_on_equity_pct": _first_number(
                    metric, ("roeTTM", "roeAnnual")
                ),
                "profit_margin_pct": _first_number(
                    metric, ("netProfitMarginTTM", "netProfitMarginAnnual")
                ),
                "debt_to_equity_ratio": _first_number(
                    metric,
                    (
                        "totalDebt/totalEquityQuarterly",
                        "totalDebt/totalEquityAnnual",
                    ),
                ),
                # Finnhub's metric endpoint does not document a unit for its
                # free-cash-flow metric, so do not store a misleading currency value.
                "free_cash_flow_value": None,
                "free_cash_flow_unit": None,
                "source_payload_hash": payload_hash(payload),
            }
        )
    except ValidationError as error:
        raise ProviderFundamentalError(
            f"Finnhub returned no usable fundamentals for ticker {candidate.ticker}.",
            provider=FINNHUB_SOURCE,
            category="NO_USABLE_METRICS",
        ) from error


def _statement_value(
    frame: Any,
    labels: Sequence[str],
) -> tuple[float | None, date | None]:
    """Extract a latest non-null statement line from a yfinance DataFrame."""
    if frame is None or getattr(frame, "empty", True):
        return None, None
    index_values = {str(label).casefold(): label for label in frame.index}
    for requested_label in labels:
        actual_label = index_values.get(requested_label.casefold())
        if actual_label is None:
            continue
        series = frame.loc[actual_label].dropna()
        if series.empty:
            continue
        number = _number_or_none(series.iloc[0])
        if number is None:
            continue
        source_date = _parse_provider_date(str(series.index[0]))
        return number, source_date
    return None, None


def _decimal_to_pct(value: float | None) -> float | None:
    """Normalize yfinance decimal margins/returns into dashboard percentage units."""
    if value is None:
        return None
    return value * 100 if -1 <= value <= 1 else value


def fetch_yfinance_record(candidate: FundamentalCandidate) -> FundamentalSnapshotRecord:
    """Use yfinance only as a clearly labelled personal-research fallback."""
    try:
        import yfinance as yf
    except ImportError:
        raise ProviderFundamentalError(
            "yfinance is not installed. Add yfinance to requirements.txt before "
            "using the fallback provider.",
            provider=YFINANCE_SOURCE,
            category="DEPENDENCY_MISSING",
        ) from None

    try:
        ticker = yf.Ticker(candidate.ticker)
        info = ticker.get_info()
        balance_sheet = ticker.balance_sheet
        cash_flow = ticker.cashflow
    except Exception:
        raise ProviderFundamentalError(
            f"yfinance fundamentals request could not be completed for ticker {candidate.ticker}.",
            provider=YFINANCE_SOURCE,
            category="REQUEST_FAILED",
        ) from None
    if not isinstance(info, Mapping):
        raise ProviderFundamentalError(
            f"yfinance returned no ticker information for {candidate.ticker}.",
            provider=YFINANCE_SOURCE,
            category="INVALID_PAYLOAD",
        )

    total_debt, debt_as_of = _statement_value(balance_sheet, ("Total Debt",))
    total_equity, equity_as_of = _statement_value(
        balance_sheet,
        ("Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"),
    )
    debt_to_equity = (
        total_debt / total_equity
        if total_debt is not None and total_equity not in (None, 0)
        else None
    )
    free_cash_flow, fcf_as_of = _statement_value(cash_flow, ("Free Cash Flow",))
    source_as_of_date = max(
        (value for value in (debt_as_of, equity_as_of, fcf_as_of) if value is not None),
        default=None,
    )
    normalized_payload = {
        "ticker": candidate.ticker,
        "regularMarketPrice": info.get("regularMarketPrice"),
        "trailingPE": info.get("trailingPE"),
        "pegRatio": info.get("pegRatio") or info.get("trailingPegRatio"),
        "priceToBook": info.get("priceToBook"),
        "trailingEps": info.get("trailingEps"),
        "returnOnEquity": info.get("returnOnEquity"),
        "profitMargins": info.get("profitMargins"),
        "financialCurrency": info.get("financialCurrency"),
        "totalDebt": total_debt,
        "totalEquity": total_equity,
        "freeCashFlow": free_cash_flow,
        "sourceAsOfDate": source_as_of_date,
    }
    try:
        return FundamentalSnapshotRecord.model_validate(
            {
                "security_key": candidate.security_key,
                "ticker": candidate.ticker,
                "source_name": YFINANCE_SOURCE,
                "source_as_of_date": source_as_of_date,
                "market_price": _number_or_none(info.get("regularMarketPrice")),
                "pe_ratio": _number_or_none(info.get("trailingPE")),
                "peg_ratio": _number_or_none(
                    info.get("pegRatio") or info.get("trailingPegRatio")
                ),
                "pb_ratio": _number_or_none(info.get("priceToBook")),
                "earnings_per_share": _number_or_none(info.get("trailingEps")),
                "return_on_equity_pct": _decimal_to_pct(
                    _number_or_none(info.get("returnOnEquity"))
                ),
                "profit_margin_pct": _decimal_to_pct(
                    _number_or_none(info.get("profitMargins"))
                ),
                "debt_to_equity_ratio": debt_to_equity,
                "free_cash_flow_value": free_cash_flow,
                "free_cash_flow_unit": (
                    str(info.get("financialCurrency"))
                    if free_cash_flow is not None
                    and isinstance(info.get("financialCurrency"), str)
                    else None
                ),
                "source_payload_hash": payload_hash(normalized_payload),
            }
        )
    except ValidationError as error:
        raise ProviderFundamentalError(
            f"yfinance returned no usable fundamentals for ticker {candidate.ticker}.",
            provider=YFINANCE_SOURCE,
            category="NO_USABLE_METRICS",
        ) from error


def missing_finnhub_metrics(record: FundamentalSnapshotRecord) -> list[str]:
    """Return canonical metrics missing from a Finnhub result.

    The fallback decision is explicit: a partially populated Finnhub payload is
    not silently mixed with Yahoo fields. Instead, the pipeline requests one
    complete yfinance fallback snapshot and records that provider as the source.
    """
    return [
        field
        for field in FINNHUB_REQUIRED_METRICS
        if getattr(record, field) is None
    ]


def fetch_best_fundamentals(
    session: requests.Session,
    candidate: FundamentalCandidate,
    api_key: str,
) -> FundamentalSnapshotRecord:
    """Use Finnhub first and yfinance only when Finnhub cannot yield a usable row."""
    try:
        finnhub_payload = fetch_finnhub_metric_payload(session, candidate.ticker, api_key)
        finnhub_record = build_finnhub_record(candidate, finnhub_payload)
        missing_metrics = missing_finnhub_metrics(finnhub_record)
        if not missing_metrics:
            return finnhub_record
        raise ProviderFundamentalError(
            f"Finnhub omitted required fundamentals for ticker {candidate.ticker}: "
            f"{', '.join(missing_metrics)}.",
            provider=FINNHUB_SOURCE,
            category="INCOMPLETE_METRICS",
        )
    except ProviderFundamentalError as finnhub_error:
        logger.info(
            "Finnhub fundamentals unavailable for ticker=%s (%s); trying yfinance fallback.",
            candidate.ticker,
            finnhub_error.category,
        )
        try:
            return fetch_yfinance_record(candidate)
        except ProviderFundamentalError as yfinance_error:
            raise ProviderFundamentalError(
                f"No usable fundamentals were returned for ticker {candidate.ticker} "
                f"by Finnhub or yfinance.",
                provider="MULTI_PROVIDER",
                category=(
                    f"FINNHUB_{finnhub_error.category};"
                    f"YFINANCE_{yfinance_error.category}"
                ),
            ) from None


def _returned_number(value: object) -> int:
    """Normalize python-oracledb scalar and one-element RETURNING values."""
    if isinstance(value, list):
        if len(value) != 1:
            raise RuntimeError("Oracle did not return exactly one market ETL run ID.")
        value = value[0]
    if value is None:
        raise RuntimeError("Oracle did not return a market ETL run ID.")
    return int(value)


def create_run(cursor: oracledb.Cursor) -> int:
    """Create a RUNNING canonical fundamentals audit record."""
    run_id = cursor.var(oracledb.DB_TYPE_NUMBER)
    cursor.execute(
        """
        INSERT INTO mk_etl_run (source_name, run_status)
        VALUES (:source_name, 'RUNNING')
        RETURNING run_id INTO :run_id
        """,
        {"source_name": RUN_SOURCE, "run_id": run_id},
    )
    return _returned_number(run_id.getvalue())


def finish_run(
    cursor: oracledb.Cursor,
    run_id: int,
    metrics: PipelineMetrics,
    *,
    status: str,
    error_message: str | None = None,
) -> None:
    """Finalize an audit record without committing outside the caller's transaction."""
    cursor.execute(
        """
        UPDATE mk_etl_run
        SET completed_at = SYSTIMESTAMP,
            run_status = :status,
            extracted_count = :extracted_count,
            validated_count = :validated_count,
            rejected_count = :rejected_count,
            loaded_count = :loaded_count,
            error_message = :error_message
        WHERE run_id = :run_id
        """,
        {
            "run_id": run_id,
            "status": status,
            "extracted_count": metrics.extracted_count,
            "validated_count": metrics.validated_count,
            "rejected_count": metrics.rejected_count,
            "loaded_count": metrics.loaded_count,
            "error_message": error_message[:1000] if error_message else None,
        },
    )


def insert_fundamental_snapshot(
    cursor: oracledb.Cursor,
    record: FundamentalSnapshotRecord,
    run_id: int,
) -> None:
    """Insert one immutable, validated snapshot with its exact provider identity."""
    cursor.setinputsizes(
        security_key=int,
        run_id=int,
        source_name=str,
        source_as_of_date=oracledb.DB_TYPE_DATE,
        market_price=float,
        pe_ratio=float,
        peg_ratio=float,
        pb_ratio=float,
        earnings_per_share=float,
        return_on_equity_pct=float,
        profit_margin_pct=float,
        debt_to_equity_ratio=float,
        free_cash_flow_value=float,
        free_cash_flow_unit=str,
        source_payload_hash=str,
    )
    bind_values = record.model_dump(exclude={"ticker"})
    bind_values["run_id"] = run_id
    cursor.execute(
        """
        INSERT INTO mk_fundamental_snapshot (
            security_key, run_id, source_name, source_as_of_date, market_price,
            pe_ratio, peg_ratio, pb_ratio, earnings_per_share, return_on_equity_pct,
            profit_margin_pct, debt_to_equity_ratio, free_cash_flow_value,
            free_cash_flow_unit, source_payload_hash
        ) VALUES (
            :security_key, :run_id, :source_name, :source_as_of_date, :market_price,
            :pe_ratio, :peg_ratio, :pb_ratio, :earnings_per_share, :return_on_equity_pct,
            :profit_margin_pct, :debt_to_equity_ratio, :free_cash_flow_value,
            :free_cash_flow_unit, :source_payload_hash
        )
        """,
        bind_values,
    )


def run_dry_run(limit: int) -> CandidateMetrics:
    """Read and validate a small candidate cohort without calls or writes."""
    logger.info(
        "Starting fundamentals dry run: limit=%s. No provider calls and no Oracle writes "
        "will occur.",
        limit,
    )
    with managed_connection() as connection:
        with connection.cursor() as cursor:
            candidates, metrics = load_fundamental_candidates(cursor, limit)
    for candidate in candidates:
        logger.info(
            "Candidate security_key=%s | ticker=%s | sector=%s",
            candidate.security_key,
            candidate.ticker,
            candidate.sector or "Unknown",
        )
    logger.info(
        "Fundamentals dry run complete: selected=%s, validated=%s, rejected=%s. "
        "No provider calls and no Oracle writes occurred.",
        metrics.selected_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return metrics


def run_finnhub_probe(ticker: str) -> FundamentalSnapshotRecord:
    """Make exactly one Finnhub API call and intentionally perform no database write."""
    ticker = ticker.strip().upper()
    candidate = FundamentalCandidate(security_key=1, ticker=ticker, sector=None)
    session = build_http_session()
    payload = fetch_finnhub_metric_payload(session, ticker, _require_finnhub_api_key())
    record = build_finnhub_record(candidate, payload)
    missing_metrics = missing_finnhub_metrics(record)
    populated_fields = [
        field
        for field, value in record.model_dump(
            exclude={"security_key", "ticker", "source_name", "source_payload_hash"}
        ).items()
        if value is not None
    ]
    print("FINNHUB FUNDAMENTALS PROBE PASSED")
    print(f"Ticker: {ticker}")
    print(f"Provider: {record.source_name}")
    print(f"Provider as-of date: {record.source_as_of_date or 'not supplied'}")
    print(f"Usable fields: {', '.join(populated_fields)}")
    if missing_metrics:
        print(
            "Missing required Finnhub fields: "
            f"{', '.join(missing_metrics)}. Live enrichment will try yfinance "
            "for this ticker instead of mixing providers."
        )
    else:
        print("Finnhub supplied the required canonical metrics for this ticker.")
    print("No Oracle rows were written.")
    return record


def run_live_refresh(limit: int, request_delay_seconds: float) -> PipelineMetrics:
    """Enrich a staged cohort with Finnhub-first/yfinance-fallback provenance."""
    request_delay_seconds = validate_request_delay_seconds(request_delay_seconds)
    api_key = _require_finnhub_api_key()
    session = build_http_session()
    logger.info(
        "Starting live fundamentals refresh: limit=%s, request_delay_seconds=%.2f.",
        limit,
        request_delay_seconds,
    )

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = create_run(cursor)
            connection.commit()
            try:
                candidates, candidate_metrics = load_fundamental_candidates(cursor, limit)
                extracted_count = 0
                validated_count = 0
                rejected_count = candidate_metrics.rejected_count
                loaded_count = 0

                for index, candidate in enumerate(candidates):
                    extracted_count += 1
                    try:
                        record = fetch_best_fundamentals(session, candidate, api_key)
                    except ProviderFundamentalError as error:
                        rejected_count += 1
                        logger.warning(
                            "Rejected fundamentals for ticker=%s: %s",
                            candidate.ticker,
                            error,
                        )
                    else:
                        validated_count += 1
                        insert_fundamental_snapshot(cursor, record, run_id)
                        loaded_count += 1

                    if index < len(candidates) - 1 and request_delay_seconds > 0:
                        time.sleep(request_delay_seconds)

                metrics = PipelineMetrics(
                    extracted_count=extracted_count,
                    validated_count=validated_count,
                    rejected_count=rejected_count,
                    loaded_count=loaded_count,
                )
                finish_run(
                    cursor,
                    run_id,
                    metrics,
                    status="SUCCEEDED" if rejected_count == 0 else "PARTIAL",
                )
                connection.commit()
            except Exception as error:
                connection.rollback()
                metrics = PipelineMetrics(0, 0, 0, 0)
                finish_run(cursor, run_id, metrics, status="FAILED", error_message=str(error))
                connection.commit()
                logger.exception("Live fundamentals refresh run %s failed.", run_id)
                raise

    logger.info(
        "Live fundamentals refresh run %s completed: extracted=%s, validated=%s, "
        "rejected=%s, loaded=%s.",
        run_id,
        metrics.extracted_count,
        metrics.validated_count,
        metrics.rejected_count,
        metrics.loaded_count,
    )
    return metrics


def verify_fundamentals_quality() -> None:
    """Run the read-only 100%-coverage fundamentals quality gate."""
    thresholds = MarketQualityThresholds(min_fundamentals_coverage_pct=100.0)
    summary, issues = run_market_quality_check(
        thresholds,
        scope="fundamentals-refresh",
    )
    print_report(summary, issues, scope="fundamentals-refresh")
    if overall_status(issues) == "FAIL":
        raise RuntimeError(
            "Fundamentals refresh completed, but the coverage quality gate failed."
        )
    logger.info(
        "Fundamentals quality gate completed: coverage=%s/%s.",
        summary.securities_with_fundamentals,
        summary.total_watchlist_securities,
    )


def positive_int(value: str) -> int:
    """Argparse validator for a positive whole number."""
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a whole number.") from error
    if converted <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return converted


def nonnegative_float(value: str) -> float:
    """Argparse validator for an allowed request-pacing value."""
    try:
        return validate_request_delay_seconds(float(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    """Build safe dry-run, probe, pilot, and all-watchlist controls."""
    parser = argparse.ArgumentParser(
        description="Validate or enrich canonical market fundamentals with provider lineage."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate canonical securities only; no provider calls or Oracle writes.",
    )
    mode.add_argument(
        "--probe-finnhub",
        action="store_true",
        help="Make one Finnhub request for --ticker; no Oracle rows are written.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Use Finnhub first, then yfinance if needed, and insert validated snapshots.",
    )
    parser.add_argument(
        "--confirm-live-run",
        action="store_true",
        help="Required with --live to acknowledge provider usage and Oracle writes.",
    )
    parser.add_argument(
        "--ticker",
        default="AAPL",
        help="Ticker used only for --probe-finnhub (default: AAPL).",
    )
    parser.add_argument(
        "--all-watchlist",
        action="store_true",
        help=(
            "Process the complete canonical watchlist up to the fixed "
            f"{ALL_WATCHLIST_LIMIT}-security safety cap. Cannot be combined with --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help=(
            f"Small pilot size (1-{MAX_PILOT_LIMIT}; default: {DEFAULT_PILOT_LIMIT}). "
            "Do not use with --all-watchlist."
        ),
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=nonnegative_float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=(
            "Delay between live securities "
            f"(0-{MAX_REQUEST_DELAY_SECONDS:g}; default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--verify-fundamentals-quality",
        action="store_true",
        help=(
            "After a live run, run the read-only fundamentals coverage/as-of-date "
            "quality gate with a 100% coverage target."
        ),
    )
    return parser


def main() -> None:
    """Run the selected safety gate, provider probe, pilot, or full refresh."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        limit = resolve_refresh_limit(args.all_watchlist, args.limit)
    except ValueError as error:
        parser.error(str(error))

    try:
        if args.dry_run:
            if args.verify_fundamentals_quality:
                parser.error("--verify-fundamentals-quality requires --live.")
            run_dry_run(limit)
            return
        if args.probe_finnhub:
            if args.verify_fundamentals_quality:
                parser.error("--verify-fundamentals-quality requires --live.")
            run_finnhub_probe(args.ticker)
            return
        if not args.confirm_live_run:
            parser.error("--live requires --confirm-live-run.")
        run_live_refresh(limit, args.request_delay_seconds)
        if args.verify_fundamentals_quality:
            verify_fundamentals_quality()
    except Exception:
        logger.exception("Market-fundamentals pipeline failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
