"""Pure quality and freshness rules for canonical market-watchlist data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from portfolio_dashboard.data_quality import QualityIssue, Severity


@dataclass(frozen=True)
class MarketQualitySummary:
    """Aggregated facts read from the canonical market reporting model."""

    total_watchlist_securities: int
    securities_with_price_history: int
    securities_missing_price_history: int
    duplicate_daily_price_groups: int
    securities_with_current_quote: int
    securities_missing_current_quote: int
    oldest_current_quote_observed_at: date | datetime | None
    newest_current_quote_observed_at: date | datetime | None
    securities_with_fundamentals: int
    securities_missing_fundamentals: int
    fundamentals_with_unknown_as_of_date: int
    oldest_latest_price_date: date | datetime | None
    newest_latest_price_date: date | datetime | None

    @property
    def price_history_coverage_pct(self) -> float | None:
        """Return latest-price coverage only when securities exist."""
        if self.total_watchlist_securities == 0:
            return None
        return self.securities_with_price_history / self.total_watchlist_securities * 100

    @property
    def fundamentals_coverage_pct(self) -> float | None:
        """Return fundamentals coverage only when securities exist."""
        if self.total_watchlist_securities == 0:
            return None
        return self.securities_with_fundamentals / self.total_watchlist_securities * 100

    @property
    def current_quote_coverage_pct(self) -> float | None:
        """Return current-quote coverage only when securities exist."""
        if self.total_watchlist_securities == 0:
            return None
        return self.securities_with_current_quote / self.total_watchlist_securities * 100


@dataclass(frozen=True)
class MarketQualityThresholds:
    """Explicit expectations for market-data completeness and freshness."""

    min_price_history_coverage_pct: float = 100.0
    max_price_age_days: float = 7.0
    min_current_quote_coverage_pct: float = 100.0
    max_current_quote_age_hours: float = 72.0
    min_fundamentals_coverage_pct: float = 25.0


def _as_utc_date(value: date | datetime) -> date:
    """Normalize an Oracle DATE or timestamp to a UTC calendar date."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.date()
    return value


def price_age_in_days(
    latest_price_date: date | datetime | None,
    now: datetime,
) -> float | None:
    """Return calendar-day age of the newest stored market price."""
    if latest_price_date is None:
        return None
    return float((_as_utc_date(now) - _as_utc_date(latest_price_date)).days)


def quote_age_in_hours(
    observed_at: date | datetime | None,
    now: datetime,
) -> float | None:
    """Return elapsed hours since a quote was observed by the pipeline."""
    if observed_at is None:
        return None

    if isinstance(observed_at, datetime):
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        else:
            observed_at = observed_at.astimezone(timezone.utc)
    else:
        observed_at = datetime.combine(observed_at, time.min, tzinfo=timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    return (now - observed_at).total_seconds() / 3600


def evaluate_market_quality(
    summary: MarketQualitySummary,
    thresholds: MarketQualityThresholds,
    now: datetime | None = None,
) -> list[QualityIssue]:
    """Evaluate market quality without database access for unit testing."""
    now = now or datetime.now(timezone.utc)
    issues: list[QualityIssue] = []

    if summary.total_watchlist_securities == 0:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "watchlist_population",
                "No securities were found in the canonical market watchlist.",
            )
        )

    if summary.duplicate_daily_price_groups > 0:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "daily_price_duplicates",
                f"{summary.duplicate_daily_price_groups} duplicate security/date "
                "daily-price group(s) were found.",
            )
        )

    quote_coverage = summary.current_quote_coverage_pct
    if (
        quote_coverage is not None
        and quote_coverage < thresholds.min_current_quote_coverage_pct
    ):
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "current_quote_coverage",
                f"Current quote coverage is {quote_coverage:.2f}%, below the configured "
                f"{thresholds.min_current_quote_coverage_pct:.2f}% baseline. "
                f"{summary.securities_missing_current_quote} security or securities "
                "have no current quote snapshot.",
            )
        )

    # As with daily prices, use the oldest quote observation across the watchlist.
    # One recently refreshed ticker must not hide stale quotes for other tickers.
    quote_age_hours = quote_age_in_hours(
        summary.oldest_current_quote_observed_at,
        now,
    )
    if quote_age_hours is None:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "current_quote_freshness",
                "No current market-quote observation timestamp is available for the watchlist.",
            )
        )
    elif quote_age_hours > thresholds.max_current_quote_age_hours:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "current_quote_freshness",
                f"The oldest current quote observation across the watchlist is "
                f"{quote_age_hours:.1f} hour(s) old, above the "
                f"{thresholds.max_current_quote_age_hours:.1f}-hour threshold.",
            )
        )

    coverage = summary.price_history_coverage_pct
    if coverage is not None and coverage < thresholds.min_price_history_coverage_pct:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "price_history_coverage",
                f"Latest-price coverage is {coverage:.2f}%, below the configured "
                f"{thresholds.min_price_history_coverage_pct:.2f}% baseline. "
                f"{summary.securities_missing_price_history} security or securities "
                "have no price history.",
            )
        )

    # Use the oldest per-security latest-price date. Using the newest date would
    # let one freshly refreshed ticker hide stale prices for the rest of the
    # watchlist.
    price_age_days = price_age_in_days(summary.oldest_latest_price_date, now)
    if price_age_days is None:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "price_freshness",
                "No latest market-price date is available for the watchlist.",
            )
        )
    elif price_age_days > thresholds.max_price_age_days:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "price_freshness",
                f"The oldest latest market price across the watchlist is "
                f"{price_age_days:.0f} day(s) old, above the "
                f"{thresholds.max_price_age_days:.0f}-day threshold.",
            )
        )

    fundamentals_coverage = summary.fundamentals_coverage_pct
    if (
        fundamentals_coverage is not None
        and fundamentals_coverage < thresholds.min_fundamentals_coverage_pct
    ):
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "fundamentals_coverage",
                f"Fundamentals coverage is {fundamentals_coverage:.2f}%, below the "
                f"configured {thresholds.min_fundamentals_coverage_pct:.2f}% baseline. "
                f"{summary.securities_missing_fundamentals} security or securities "
                "are missing fundamentals.",
            )
        )

    if summary.fundamentals_with_unknown_as_of_date > 0:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "fundamentals_as_of_date",
                f"{summary.fundamentals_with_unknown_as_of_date} fundamental snapshot(s) "
                "have no provider as-of date and must be treated as historical context.",
            )
        )

    return issues
