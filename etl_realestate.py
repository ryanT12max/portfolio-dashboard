import oracledb
import requests
import datetime
import os

# --- 1. EXTRACT: Fetch live data from the Real Estate API ---
# Fetch secrets safely from the environment
API_KEY = os.environ.get("RENTCAST_API_KEY")
DB_PASSWORD = os.environ.get("ORACLE_ADMIN_PASSWORD")
WALLET_PASSWORD = os.environ.get("ORACLE_WALLET_PASSWORD")
url = "https://api.rentcast.io/v1/listings/sale"

# We are specifically targeting Houston Multi-Family properties (Duplexes/Townhomes)
params = {
    "city": "Houston",
    "state": "TX",
    "propertyType": "Multi-Family", 
    "status": "Active",
    "limit": 50 
}
headers = {
    "X-Api-Key": API_KEY,
    "accept": "application/json"
}

print("Extracting live Multi-Family listings in Houston...")
response = requests.get(url, headers=headers, params=params)

# --- NEW: Defensive Error Handling ---
if response.status_code != 200:
    print(f"API Error! Code: {response.status_code}")
    print(f"Message: {response.text}")
    print("Please check your API key and try again.")
    exit() # Stops the script gracefully

listings = response.json()

# --- 2. TRANSFORM: Clean and map the JSON data to our SQL schema ---
properties_data = []
lot_data = []
market_data = []

today = datetime.datetime.now().strftime('%Y-%m-%d')

for prop in listings:
    # Safely extract values with fallbacks if the API leaves a field blank
    parcel_id = prop.get('id', 'N/A')
    address = prop.get('formattedAddress', 'Unknown')
    city = prop.get('city', 'Houston')
    state = prop.get('state', 'TX')
    zip_code = prop.get('zipCode', '00000')
    prop_type = "Duplex" if prop.get('propertyType') == "Multi-Family" else prop.get('propertyType')
    lat = prop.get('latitude', None)
    lon = prop.get('longitude', None)
    
    lot_size = prop.get('lotSize', 0)
    structure_sqft = prop.get('squareFootage', 0)
    year_built = prop.get('yearBuilt', 0)
    
    # We mock Deed Restrictions here. In a real scenario, you could cross-reference a city zoning API.
    has_restrictions = 0 if lot_size > 5000 else 1 
    
    asking_price = prop.get('price', 0)
    
    # If the API doesn't provide an exact rent estimate, we use the investor's "1% Rule" as a baseline
    est_rent = asking_price * 0.01 if asking_price else 0
    
    properties_data.append((parcel_id, address, city, state, zip_code, prop_type, lat, lon))
    lot_data.append((parcel_id, lot_size, structure_sqft, year_built, has_restrictions))
    market_data.append((f"LST-{parcel_id}", parcel_id, today, asking_price, 'Active', est_rent))

# --- 3. LOAD: Upsert into Oracle Database ---
conn = oracledb.connect(
    user="ADMIN",
    password="pi11zNcpv$!db",
    dsn="marketdevdb_high",
    config_dir="wallet",
    wallet_location="wallet",
    wallet_password="rennJenn5011"
)
cursor = conn.cursor()
print("Loading data into Oracle using MERGE statements...")

# MERGE Properties: Insert if new, skip if exists
cursor.executemany("""
    MERGE INTO Properties p
    USING (SELECT :1 AS Parcel_ID, :2 AS Street_Address, :3 AS City, :4 AS State, :5 AS Zip_Code, :6 AS Property_Type, :7 AS Latitude, :8 AS Longitude FROM dual) src
    ON (p.Parcel_ID = src.Parcel_ID)
    WHEN MATCHED THEN
        UPDATE SET Latitude = src.Latitude, Longitude = src.Longitude
    WHEN NOT MATCHED THEN
        INSERT (Parcel_ID, Street_Address, City, State, Zip_Code, Property_Type, Latitude, Longitude)
        VALUES (src.Parcel_ID, src.Street_Address, src.City, src.State, src.Zip_Code, src.Property_Type, src.Latitude, src.Longitude)
""", properties_data)

# MERGE Lot Characteristics: Update size/year if it changed, insert if new
cursor.executemany("""
    MERGE INTO Lot_Characteristics l
    USING (SELECT :1 AS Parcel_ID, :2 AS Lot_Size_SqFt, :3 AS Structure_SqFt, :4 AS Year_Built, :5 AS Has_Deed_Restrictions FROM dual) src
    ON (l.Parcel_ID = src.Parcel_ID)
    WHEN MATCHED THEN
        UPDATE SET Lot_Size_SqFt = src.Lot_Size_SqFt, Structure_SqFt = src.Structure_SqFt, Year_Built = src.Year_Built
    WHEN NOT MATCHED THEN
        INSERT (Parcel_ID, Lot_Size_SqFt, Structure_SqFt, Year_Built, Has_Deed_Restrictions)
        VALUES (src.Parcel_ID, src.Lot_Size_SqFt, src.Structure_SqFt, src.Year_Built, src.Has_Deed_Restrictions)
""", lot_data)

# MERGE Market Listings: Update the price and status if the listing exists, insert if new
cursor.executemany("""
    MERGE INTO Market_Listings m
    USING (SELECT :1 AS Listing_ID, :2 AS Parcel_ID, TO_DATE(:3, 'YYYY-MM-DD') AS Listing_Date, :4 AS Asking_Price, :5 AS Status, :6 AS Est_Monthly_Rent FROM dual) src
    ON (m.Listing_ID = src.Listing_ID)
    WHEN MATCHED THEN
        UPDATE SET Asking_Price = src.Asking_Price, Status = src.Status, Est_Monthly_Rent = src.Est_Monthly_Rent
    WHEN NOT MATCHED THEN
        INSERT (Listing_ID, Parcel_ID, Listing_Date, Asking_Price, Status, Est_Monthly_Rent)
        VALUES (src.Listing_ID, src.Parcel_ID, src.Listing_Date, src.Asking_Price, src.Status, src.Est_Monthly_Rent)
""", market_data)

conn.commit()
conn.close()
print(f"Success! {len(listings)} multi-family properties processed and loaded.")