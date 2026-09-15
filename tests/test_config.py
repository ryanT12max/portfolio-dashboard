import pytest

from portfolio_dashboard import config


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Keep cached settings from leaking between tests."""
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def set_valid_environment(monkeypatch):
    monkeypatch.setenv("DB_USER", "test_user")
    monkeypatch.setenv("DB_PASSWORD", "test_password")
    monkeypatch.setenv("DB_DSN", "test_dsn")
    monkeypatch.setenv("RENTCAST_API_KEY", "test_rentcast_key")
    monkeypatch.setenv("RENTCAST_MAX_LISTINGS", "25")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)


def test_get_settings_reads_valid_environment(monkeypatch):
    set_valid_environment(monkeypatch)

    settings = config.get_settings()

    assert settings.database.user == "test_user"
    assert settings.database.password == "test_password"
    assert settings.database.dsn == "test_dsn"
    assert settings.rentcast_api_key == "test_rentcast_key"
    assert settings.rentcast_max_listings == 25
    assert settings.finnhub_api_key is None


def test_get_settings_rejects_invalid_listing_limit(monkeypatch):
    set_valid_environment(monkeypatch)
    monkeypatch.setenv("RENTCAST_MAX_LISTINGS", "not-a-number")

    with pytest.raises(config.ConfigurationError):
        config.get_settings()


def test_get_settings_requires_database_password(monkeypatch):
    set_valid_environment(monkeypatch)
    monkeypatch.delenv("DB_PASSWORD")

    with pytest.raises(config.ConfigurationError):
        config.get_settings()