import os
import oracledb
from dotenv import load_dotenv

load_dotenv()

def create_table():
    print("Connecting to Oracle to build the schema...")
    connection = oracledb.connect(
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        dsn=os.getenv("DB_DSN")
    )
    cursor = connection.cursor()

    # SQL to create the table
    sql = """
    CREATE TABLE real_estate_listings (
        street_address VARCHAR2(255) PRIMARY KEY,
        asking_price NUMBER,
        est_monthly_rent NUMBER,
        bedrooms NUMBER,
        bathrooms NUMBER,
        square_footage NUMBER,
        lot_size_sqft NUMBER,
        latitude NUMBER,
        longitude NUMBER,
        gross_yield_pct NUMBER
    )
    """
    
    try:
        cursor.execute(sql)
        print("Success! The real_estate_listings table has been created.")
    except Exception as e:
        print(f"Error (it might already exist): {e}")

    connection.commit()
    cursor.close()
    connection.close()

if __name__ == "__main__":
    create_table()