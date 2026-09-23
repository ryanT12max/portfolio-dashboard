"""Unit tests for pure market watchlist quality rules."""

from __future__ import annotations

from datetime import date, datetime, timezone

from portfolio_dashboard.data_quality import Severity, overall_status
from portfolio_dashboard.market_data_quality import (
    MarketQualitySummary,
    MarketQualityThresholds,
    evaluate_fundamentals_refresh_quality,
    evaluate_market_quality,
    evaluate_price_refresh_quality,
    evaluate_quote_refresh_quality,
    price_age_in_days,
    quote_age_in_hours,
)


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def market_summary(**overrides: object) -> MarketQualitySummary:
    """Return a valid, fresh baseline summary with optional field overrides."""
    values: dict[str, object] = {
        "total_watchlist_securities": 44,
        "securities_with_price_history": 44,
        "securities_missing_price_history": 0,
        "duplicate_daily_price_groups": 0,
        "securities_with_current_quote": 44,
        "securities_missing_current_quote": 0,
        "oldest_current_quote_observed_at": datetime(
            2026, 9, 21, 8, tzinfo=timezone.utc
        ),
        "newest_current_quote_observed_at": datetime(
            2026, 9, 21, 11, tzinfo=timezone.utc
        ),
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


def test_missing_current_quotes_are_an_error() -> None:
    issues = evaluate_market_quality(
        market_summary(
            securities_with_current_quote=43,
            securities_missing_current_quote=1,
        ),
        MarketQualityThresholds(),
        now=NOW,
    )

    assert "current_quote_coverage" in check_names(issues)
    assert any(issue.severity == Severity.ERROR for issue in issues)


def test_quote_refresh_scope_ignores_known_legacy_research_gaps() -> None:
    issues = evaluate_quote_refresh_quality(
        market_summary(
            securities_with_fundamentals=3,
            securities_missing_fundamentals=41,
            fundamentals_with_unknown_as_of_date=3,
            oldest_latest_price_date=date(2026, 9, 8),
            newest_latest_price_date=date(2026, 9, 8),
        ),
        MarketQualityThresholds(max_current_quote_age_hours=30),
        now=NOW,
    )

    assert issues == []


def test_quote_refresh_scope_still_fails_for_missing_quotes() -> None:
    issues = evaluate_quote_refresh_quality(
        market_summary(
            securities_with_current_quote=43,
            securities_missing_current_quote=1,
        ),
        MarketQualityThresholds(),
        now=NOW,
    )

    assert check_names(issues) == {"current_quote_coverage"}


def test_price_refresh_scope_ignores_quote_and_fundamentals_lineage() -> None:
    issues = evaluate_price_refresh_quality(
        market_summary(
            oldest_current_quote_observed_at=None,
            newest_current_quote_observed_at=None,
            securities_with_fundamentals=3,
            securities_missing_fundamentals=41,
            fundamentals_with_unknown_as_of_date=3,
        ),
        MarketQualityThresholds(max_price_age_days=7),
        now=NOW,
    )

    assert issues == []


def test_price_refresh_scope_fails_for_stale_or_duplicate_prices() -> None:
    issues = evaluate_price_refresh_quality(
        market_summary(
            oldest_latest_price_date=date(2026, 9, 8),
            newest_latest_price_date=date(2026, 9, 20),
            duplicate_daily_price_groups=1,
        ),
        MarketQualityThresholds(max_price_age_days=7),
        now=NOW,
    )

    assert check_names(issues) == {"daily_price_duplicates", "price_freshness"}


def test_fundamentals_refresh_scope_ignores_price_and_quote_history_gaps() -> None:
    issues = evaluate_fundamentals_refresh_quality(
        market_summary(
            oldest_current_quote_observed_at=None,
            newest_current_quote_observed_at=None,
            oldest_latest_price_date=date(2026, 9, 8),
            newest_latest_price_date=date(2026, 9, 8),
        ),
        MarketQualityThresholds(min_fundamentals_coverage_pct=100),
    )

    assert issues == []


def test_fundamentals_refresh_scope_fails_below_coverage_target() -> None:
    issues = evaluate_fundamentals_refresh_quality(
        market_summary(
            securities_with_fundamentals=43,
            securities_missing_fundamentals=1,
        ),
        MarketQualityThresholds(min_fundamentals_coverage_pct=100),
    )

    assert check_names(issues) == {"fundamentals_coverage"}


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


def test_one_fresh_ticker_cannot_hide_stale_watchlist_prices() -> None:
    issues = evaluate_market_quality(
        market_summary(
            oldest_latest_price_date=date(2026, 9, 8),
            newest_latest_price_date=date(2026, 9, 21),
        ),
        MarketQualityThresholds(max_price_age_days=7),
        now=NOW,
    )

    assert "price_freshness" in check_names(issues)
    assert overall_status(issues) == "FAIL"


def test_stale_current_quotes_are_an_error() -> None:
    issues = evaluate_market_quality(
        market_summary(
            oldest_current_quote_observed_at=datetime(
                2026, 9, 17, 12, tzinfo=timezone.utc
            ),
            newest_current_quote_observed_at=datetime(
                2026, 9, 17, 12, tzinfo=timezone.utc
            ),
        ),
        MarketQualityThresholds(max_current_quote_age_hours=72),
        now=NOW,
    )

    assert "current_quote_freshness" in check_names(issues)
    assert overall_status(issues) == "FAIL"


def test_one_fresh_quote_cannot_hide_stale_watchlist_quotes() -> None:
    issues = evaluate_market_quality(
        market_summary(
            oldest_current_quote_observed_at=datetime(
                2026, 9, 17, 12, tzinfo=timezone.utc
            ),
            newest_current_quote_observed_at=NOW,
        ),
        MarketQualityThresholds(max_current_quote_age_hours=72),
        now=NOW,
    )

    assert "current_quote_freshness" in check_names(issues)


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


def test_quote_age_uses_elapsed_hours() -> None:
    observed_at = datetime(2026, 9, 21, 6, tzinfo=timezone.utc)
    assert quote_age_in_hours(observed_at, NOW) == 6.0
