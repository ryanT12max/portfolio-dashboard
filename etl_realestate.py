import os
import logging
import requests
import pandas as pd
import oracledb
from pydantic import BaseModel, Field, ValidationError
from typing import Optional
from dotenv import load_dotenv

# --- THIS IS THE MAGIC LINE THAT LOADS YOUR .ENV FILE ---
load_dotenv()

# 1. Configure Production Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler("etl_pipeline.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("RealEstateETL")

# 2. Define Strict Data Contract using Pydantic
class PropertyListing(BaseModel):
    street_address: str = Field(..., alias="formattedAddress")
    asking_price: float = Field(..., gt=0, alias="price")
    est_monthly_rent: Optional[float] = Field(0.0, alias="rent")
    bedrooms: Optional[int] = Field(0, alias="bedrooms")
    bathrooms: Optional[float] = Field(0.0, alias="bathrooms")
    square_footage: Optional[float] = Field(0.0, alias="squareFootage")
    lot_size_sqft: Optional[float] = Field(0.0, alias="lotSize")
    latitude: float = Field(..., alias="latitude")
    longitude: float = Field(..., alias="longitude")

def fetch_and_validate_listings():
    logger.info("Starting RentCast API data fetch...")
    
    api_key = os.getenv("RENTCAST_API_KEY")
    if not api_key:
        logger.error("RENTCAST_API_KEY environment variable is missing!")
        raise ValueError("API key not found.")
        
    url = "https://api.rentcast.io/v1/listings/sale"
    params = {
        "city": "Houston",
        "state": "TX",
        "propertyType": "Duplex,Triplex,Quadplex",
        "status": "Active",
        "limit": "50"
    }
    headers = {
        "accept": "json",
        "X-Api-Key": api_key
    }
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        logger.error(f"API request failed with status code {response.status_code}: {response.text}")
        response.raise_for_status()
        
    raw_data = response.json()
    logger.info(f"Retrieved {len(raw_data)} raw records from RentCast API.")
    
    valid_records = []
    for item in raw_data:
        try:
            validated_record = PropertyListing(**item)
            valid_records.append(validated_record.model_dump(by_alias=False))
        except ValidationError as e:
            logger.warning(f"Data validation failed for record: {e}")
            
    logger.info(f"Successfully validated {len(valid_records)} out of {len(raw_data)} records.")
    return valid_records

def load_to_oracle(records):
    if not records:
        logger.warning("No valid records to load into Oracle.")
        return

    logger.info("Connecting to Oracle database...")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    dsn = os.getenv("DB_DSN")

    connection = oracledb.connect(user=user, password=password, dsn=dsn)
    cursor = connection.cursor()

    # --- THE DBA FIX ---
    # Disable Auto Parallel DML to prevent ORA-12838 table locks during our loop
    cursor.execute("ALTER SESSION DISABLE PARALLEL DML")

    logger.info(f"Inserting/Updating {len(records)} records in Oracle...")
    
    inserted_count = 0
    for rec in records:
        try:
            sql = """
                MERGE INTO real_estate_listings t
                using (select :addr as street_address from dual) s
                on (t.street_address = s.street_address)
                when matched then
                    update set t.asking_price = :price, 
                               t.est_monthly_rent = :rent, 
                               t.gross_yield_pct = (:rent * 12 / :price) * 100
                when not matched then
                    insert (street_address, asking_price, est_monthly_rent, bedrooms, bathrooms, square_footage, lot_size_sqft, latitude, longitude, gross_yield_pct)
                    values (:addr, :price, :rent, :beds, :baths, :sqft, :lot, :lat, :lon, (:rent * 12 / :price) * 100)
            """
            
            cursor.execute(sql, {
                'addr': rec['street_address'],
                'price': rec['asking_price'],
                'rent': rec['est_monthly_rent'],
                'beds': rec['bedrooms'],
                'baths': rec['bathrooms'],
                'sqft': rec['square_footage'],
                'lot': rec['lot_size_sqft'],
                'lat': rec['latitude'],
                'lon': rec['longitude']
            })
            inserted_count += 1
        except Exception as e:
            logger.error(f"Error loading record {rec.get('street_address')}: {e}")

    connection.commit()
    cursor.close()
    connection.close()
    logger.info(f"ETL pipeline completed successfully. Loaded/Updated {inserted_count} rows.")

if __name__ == "__main__":
    try:
        listings = fetch_and_validate_listings()
        load_to_oracle(listings)
    except Exception as e:
        logger.critical(f"ETL Pipeline failed critically: {e}")