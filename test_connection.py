"""Verify that centralized configuration can connect to Oracle."""

from __future__ import annotations

import sys

from portfolio_dashboard.database import get_connection


def main() -> None:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT SYS_CONTEXT('USERENV', 'CURRENT_USER') FROM dual"
                )
                connected_user = cursor.fetchone()[0]

        print(f"Connected successfully as: {connected_user}")

    except Exception as error:
        print(f"Connection failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()