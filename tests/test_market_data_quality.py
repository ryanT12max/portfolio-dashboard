"""Unit tests for pure market watchlist quality rules."""

from __future__ import annotations

from datetime import date, datetime, timezone

from portfolio_dashboard.data_quality import Severity, overall_status
from portfolio_dashboard.market_data_quality import (
    MarketQualitySummary,
    MarketQualityThresholds,
    evaluate_market_quality,
    price_age_in_days,
)


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def market_summary(**overrides: object) -> MarketQualitySummary:
    """Return a valid, fresh baseline summary with optional field overrides."""
    values: dict[str, object] = {
        "total_watchlist_securities": 44,
        "securities_with_price_history": 44,
        "securities_missing_price_history": 0,
        "duplicate_daily_price_groups": 0,
        "securities_with_fundamentals": 44,
        "securities_missing_fundamentals": 0,
        "fundamentals_with_unknown_as_of_date": 0,
        "oldest_latest_price_date": date(2026, 9, 20),
        "newest_latest_price_date": date(2026, 9, 20),
    }
    values.update(overrides)
    return MarketQualitySummary(**values)  # type: ignore[arg-type]


def check_names(issues: list) -> set[str]:
    """Return check names for concise assertions."""
    return {issue.check_name for issue in issues}


def test_fresh_complete_market_data_passes() -> None:
    issues = evaluate_market_quality(
        market_summary(),
        MarketQualityThresholds(),
        now=NOW,
    )

    assert issues == []
    assert overall_status(issues) == "PASS"


def test_missing_price_history_is_an_error() -> None:
    issues = evaluate_market_quality(
        market_summary(
            securities_with_price_history=43,
            securities_missing_price_history=1,
        ),
        MarketQualityThresholds(),
        now=NOW,
    )

    assert "price_history_coverage" in check_names(issues)
    assert any(issue.severity == Severity.ERROR for issue in issues)
    assert overall_status(issues) == "FAIL"


def test_stale_prices_are_an_error() -> None:
    issues = evaluate_market_quality(
        market_summary(
            oldest_latest_price_date=date(2026, 9, 8),
            newest_latest_price_date=date(2026, 9, 8),
        ),
        MarketQualityThresholds(max_price_age_days=7),
        now=NOW,
    )

    assert "price_freshness" in check_names(issues)
    assert overall_status(issues) == "FAIL"


def test_incomplete_fundamentals_are_warnings() -> None:
    issues = evaluate_market_quality(
        market_summary(
            securities_with_fundamentals=3,
            securities_missing_fundamentals=41,
            fundamentals_with_unknown_as_of_date=3,
        ),
        MarketQualityThresholds(),
        now=NOW,
    )

    assert check_names(issues) == {
        "fundamentals_coverage",
        "fundamentals_as_of_date",
    }
    assert all(issue.severity == Severity.WARNING for issue in issues)
    assert overall_status(issues) == "PASS_WITH_WARNINGS"


def test_duplicate_daily_prices_are_an_error() -> None:
    issues = evaluate_market_quality(
        market_summary(duplicate_daily_price_groups=1),
        MarketQualityThresholds(),
        now=NOW,
    )

    assert "daily_price_duplicates" in check_names(issues)
    assert overall_status(issues) == "FAIL"


def test_price_age_uses_calendar_days() -> None:
    assert price_age_in_days(date(2026, 9, 8), NOW) == 13.0