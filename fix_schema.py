import oracledb

# 1. Connect to Oracle with hardcoded passwords for the patch
conn = oracledb.connect(
    user="ADMIN",
    password="pi11zNcpv$!db",
    dsn="marketdevdb_high",
    config_dir="wallet",
    wallet_location="wallet",
    wallet_password="rennJenn5011"
)
cursor = conn.cursor()

print("Expanding database columns to accommodate larger API text...")

# 2. Modify the column sizes from 50 to 150 characters across all tables
alter_queries = [
    "ALTER TABLE Properties ADD (Latitude NUMBER, Longitude NUMBER)"
]

for query in alter_queries:
    try:
        cursor.execute(query)
        print(f"Success: {query}")
    except Exception as e:
        print(f"Error executing {query}: {e}")

conn.commit()
conn.close()
print("Schema update complete! Your database is now ready for the data.")