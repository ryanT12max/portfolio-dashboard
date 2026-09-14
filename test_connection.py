"""Verify that local environment configuration can connect to Oracle."""

import os
import sys

import oracledb
from dotenv import load_dotenv


load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    try:
        with oracledb.connect(
            user=required_env("DB_USER"),
            password=required_env("DB_PASSWORD"),
            dsn=required_env("DB_DSN"),
        ) as connection:
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