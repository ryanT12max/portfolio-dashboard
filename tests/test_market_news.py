"""Focused unit tests for the non-persistent market-news dashboard helpers."""

from app import (
    NEWS_SUMMARY_MAX_CHARACTERS,
    news_timestamp_text,
    normalize_yfinance_news_item,
    truncate_news_summary,
)


def test_normalize_yfinance_news_item_reads_nested_current_shape() -> None:
    item = {
        "content": {
            "title": "Example Apple story",
            "summary": "A short provider-supplied summary.",
            "pubDate": "2026-09-25T15:00:00Z",
            "canonicalUrl": {"url": "https://example.com/apple-story"},
            "provider": {"displayName": "Example Publisher"},
            "relatedTickers": ["AAPL"],
        }
    }

    story = normalize_yfinance_news_item(item, "AAPL")

    assert story is not None
    assert story["title"] == "Example Apple story"
    assert story["summary"] == "A short provider-supplied summary."
    assert story["publisher"] == "Example Publisher"
    assert story["url"] == "https://example.com/apple-story"
    assert news_timestamp_text(story["published_at"]) == "2026-09-25 15:00 UTC"


def test_normalize_yfinance_news_item_rejects_explicitly_unrelated_story() -> None:
    item = {
        "title": "Story for a different company",
        "link": "https://example.com/other-company",
        "relatedTickers": ["MSFT"],
    }

    assert normalize_yfinance_news_item(item, "AAPL") is None


def test_normalize_yfinance_news_item_accepts_related_ticker_objects() -> None:
    item = {
        "title": "Apple-related story",
        "link": "https://example.com/apple-related-story",
        "relatedTickers": [{"symbol": "AAPL"}],
    }

    assert normalize_yfinance_news_item(item, "AAPL") is not None


def test_normalize_yfinance_news_item_rejects_non_web_link() -> None:
    item = {
        "title": "Invalid link story",
        "link": "javascript:alert('not a story')",
        "relatedTickers": ["AAPL"],
    }

    assert normalize_yfinance_news_item(item, "AAPL") is None


def test_truncate_news_summary_keeps_provider_text_brief() -> None:
    summary = "x" * (NEWS_SUMMARY_MAX_CHARACTERS + 1)

    truncated = truncate_news_summary(summary)

    assert truncated.endswith("…")
    assert len(truncated) == NEWS_SUMMARY_MAX_CHARACTERS + 1