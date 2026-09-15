from portfolio_dashboard import database
from portfolio_dashboard.config import DatabaseConfig


def test_get_connection_uses_supplied_database_config(monkeypatch):
    captured_arguments = {}

    def fake_connect(**kwargs):
        captured_arguments.update(kwargs)
        return "fake connection"

    monkeypatch.setattr(database.oracledb, "connect", fake_connect)

    result = database.get_connection(
        DatabaseConfig(
            user="test_user",
            password="test_password",
            dsn="test_dsn",
        )
    )

    assert result == "fake connection"
    assert captured_arguments == {
        "user": "test_user",
        "password": "test_password",
        "dsn": "test_dsn",
    }