import oracledb

conn = oracledb.connect(
    user="ADMIN",
    password="pi11zNcpv$!db",
    dsn="marketdevdb_high",
    config_dir="wallet",
    wallet_location="wallet",
    wallet_password="rennJenn5011"
)
cursor = conn.cursor()

# Drop the old table
try:
    cursor.execute("DROP TABLE Market_Data")
except oracledb.DatabaseError:
    pass # Skips if the table doesn't exist

# Create the expanded schema
cursor.execute("""
    CREATE TABLE Market_Data (
        Ticker VARCHAR2(10) PRIMARY KEY,
        Sector VARCHAR2(50),
        Current_Price NUMBER,
        PE_Ratio NUMBER,
        PEG_Ratio NUMBER,
        PB_Ratio NUMBER,
        EPS NUMBER,
        ROE NUMBER,
        Profit_Margin NUMBER,
        DE_Ratio NUMBER,
        FCF NUMBER
    )
""")

# Insert expanded sample data
data = [
    ('AAPL', 'Technology', 150.00, 28.5, 1.2, 35.0, 5.9, 140.0, 25.0, 1.5, 90.0),
    ('JNJ', 'Healthcare', 160.00, 15.2, 2.0, 5.5, 10.5, 25.0, 15.0, 0.5, 17.0),
    ('XOM', 'Energy', 110.00, 9.8, 0.8, 1.9, 11.2, 20.0, 10.0, 0.3, 30.0)
]
cursor.executemany("INSERT INTO Market_Data VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9, :10, :11)", data)
conn.commit()
print("Expanded table created and seeded!")
conn.close()