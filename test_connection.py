import oracledb

try:
    # We use "Thin mode" which reads the wallet files directly
    connection = oracledb.connect(
        user="ADMIN",
        password="pi11zNcpv$!db",
        dsn="marketdevdb_high", # e.g., portfolio_ledger_high
        config_dir="wallet",
        wallet_location="wallet",
        wallet_password="rennJenn5011"
    )
    print("Success! Connected to Oracle Database.")
    connection.close()
except Exception as e:
    print(f"Connection failed: {e}")