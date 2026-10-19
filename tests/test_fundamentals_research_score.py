"""Tests for the transparent market-fundamentals research-score feature."""

from __future__ import annotations

import pandas as pd

from app import (
    RESEARCH_SIGNAL_COLORS,
    RESEARCH_SIGNAL_LABELS,
    build_fundamentals_research_score_frame,
    research_signal_css,
    research_signal_for_score,
)


def _sector_comparison_frame() -> pd.DataFrame:
    """Create a simple six-company peer group plus one incomplete record."""
    return pd.DataFrame(
        {
            "ticker": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "MISSING"],
            "security_name": [
                "Alpha Inc.",
                "Bravo Inc.",
                "Charlie Inc.",
                "Delta Inc.",
                "Echo Inc.",
                "Foxtrot Inc.",
                "Missing Metrics Inc.",
            ],
            "sector": ["Technology"] * 7,
            "pe_ratio": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, None],
            "peg_ratio": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, None],
            "pb_ratio": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, None],
            "return_on_equity_pct": [30.0, 25.0, 20.0, 15.0, 10.0, 5.0, None],
            "profit_margin_pct": [30.0, 25.0, 20.0, 15.0, 10.0, 5.0, None],
            "debt_to_equity_ratio": [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, None],
        }
    )


def test_research_score_uses_readable_signals_and_hides_metric_coverage() -> None:
    score_frame = build_fundamentals_research_score_frame(_sector_comparison_frame())
    signals_by_ticker = score_frame.set_index("Ticker")["Signal"].to_dict()

    assert signals_by_ticker["AAA"] == RESEARCH_SIGNAL_LABELS["higher_priority"]
    assert signals_by_ticker["CCC"] == RESEARCH_SIGNAL_LABELS["favorable"]
    assert signals_by_ticker["DDD"] == RESEARCH_SIGNAL_LABELS["mixed"]
    assert signals_by_ticker["EEE"] == RESEARCH_SIGNAL_LABELS["caution"]
    assert signals_by_ticker["MISSING"] == RESEARCH_SIGNAL_LABELS["insufficient"]
    assert "Metric coverage" not in score_frame.columns


def test_research_score_uses_simple_strength_and_flag_explanations() -> None:
    score_frame = build_fundamentals_research_score_frame(_sector_comparison_frame())
    rows_by_ticker = score_frame.set_index("Ticker")

    assert "Priced lower than similar companies" in rows_by_ticker.loc[
        "AAA", "Simple strengths"
    ]
    assert "Investors are paying more" in rows_by_ticker.loc[
        "EEE", "Simple research flags"
    ]
    assert "More information is needed" in rows_by_ticker.loc[
        "MISSING", "Simple research flags"
    ]


def test_research_signal_colors_follow_the_dashboard_legend() -> None:
    assert RESEARCH_SIGNAL_COLORS[RESEARCH_SIGNAL_LABELS["higher_priority"]] == "#15803D"
    assert RESEARCH_SIGNAL_COLORS[RESEARCH_SIGNAL_LABELS["favorable"]] == "#16A34A"
    assert RESEARCH_SIGNAL_COLORS[RESEARCH_SIGNAL_LABELS["mixed"]] == "#1D4ED8"
    assert RESEARCH_SIGNAL_COLORS[RESEARCH_SIGNAL_LABELS["caution"]] == "#B91C1C"
    assert RESEARCH_SIGNAL_COLORS[RESEARCH_SIGNAL_LABELS["insufficient"]] == "#B91C1C"


def test_research_signal_css_colors_the_signal_cell() -> None:
    assert "#15803D" in research_signal_css(
        RESEARCH_SIGNAL_LABELS["higher_priority"]
    )
    assert "#1D4ED8" in research_signal_css(RESEARCH_SIGNAL_LABELS["mixed"])
    assert "#B91C1C" in research_signal_css(RESEARCH_SIGNAL_LABELS["caution"])


def test_research_signal_boundaries_are_non_prescriptive() -> None:
    assert research_signal_for_score(75.0, 6) == RESEARCH_SIGNAL_LABELS[
        "higher_priority"
    ]
    assert research_signal_for_score(55.0, 6) == RESEARCH_SIGNAL_LABELS["favorable"]
    assert research_signal_for_score(35.0, 6) == RESEARCH_SIGNAL_LABELS["mixed"]
    assert research_signal_for_score(34.9, 6) == RESEARCH_SIGNAL_LABELS["caution"]
    assert research_signal_for_score(100.0, 3) == RESEARCH_SIGNAL_LABELS[
        "insufficient"
    ]
