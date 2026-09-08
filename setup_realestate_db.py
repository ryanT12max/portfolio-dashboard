import oracledb

# 1. Connect to Oracle
conn = oracledb.connect(
    user="ADMIN",
    password="pi11lzNcpv$!db",
    dsn="marketdevdb_high",
    config_dir="wallet",
    wallet_location="wallet",
    wallet_password="rennJenn5011"
)
cursor = conn.cursor()

# 2. Define the SQL DDL (Data Definition Language)
schema_sql = [
    """
    CREATE TABLE Properties (
        Parcel_ID VARCHAR2(50) PRIMARY KEY,
        Street_Address VARCHAR2(150) NOT NULL,
        City VARCHAR2(50),
        State VARCHAR2(2),
        Zip_Code VARCHAR2(10),
        Property_Type VARCHAR2(50) -- e.g., 'Duplex', 'Townhome', 'Single Family'
    )
    """,
    """
    CREATE TABLE Lot_Characteristics (
        Parcel_ID VARCHAR2(50) PRIMARY KEY,
        Lot_Size_SqFt NUMBER,
        Structure_SqFt NUMBER,
        Year_Built NUMBER(4),
        Has_Deed_Restrictions NUMBER(1), -- 1 for Yes, 0 for No
        CONSTRAINT fk_lot_parcel FOREIGN KEY (Parcel_ID) 
            REFERENCES Properties(Parcel_ID) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE Market_Listings (
        Listing_ID VARCHAR2(50) PRIMARY KEY,
        Parcel_ID VARCHAR2(50),
        Listing_Date DATE,
        Asking_Price NUMBER,
        Status VARCHAR2(20), -- e.g., 'Active', 'Pending', 'Sold'
        Est_Monthly_Rent NUMBER,
        CONSTRAINT fk_market_parcel FOREIGN KEY (Parcel_ID) 
            REFERENCES Properties(Parcel_ID) ON DELETE CASCADE
    )
    """
]

# 3. Execute the table creation
for query in schema_sql:
    try:
        cursor.execute(query)
        print("Table created successfully.")
    except oracledb.DatabaseError as e:
        error, = e.args
        if error.code == 955:
            print("Table already exists, skipping...")
        else:
            print(f"Database error: {error.message}")

conn.commit()
conn.close()
print("Real estate schema deployment complete!")