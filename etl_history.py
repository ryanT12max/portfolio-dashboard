import oracledb
import yfinance as yf

conn = oracledb.connect(
    user="ADMIN",
    password="pi11zNcpv$!db",
    dsn="marketdevdb_high",
    config_dir="wallet",
    wallet_location="wallet",
    wallet_password="rennJenn5011"
)
cursor = conn.cursor()

# Drop and recreate expanded schema
try:
    cursor.execute("DROP TABLE Stock_History")
except oracledb.DatabaseError:
    pass

cursor.execute("""
    CREATE TABLE Stock_History (
        Ticker VARCHAR2(10),
        Trade_Date DATE,
        Close_Price NUMBER,
        Volume NUMBER,
        PRIMARY KEY (Ticker, Trade_Date)
    )
""")

# List of all 45 stocks from your dashboard
tickers = [
    "AAPL", "MSFT", "NVDA", "ORCL", "CRM", "INTC", "CSCO", "ADBE", "AMD", "QCOM", "TXN", "IBM", "NOW", "INTU", "AMAT", 
    "JNJ", "UNH", "PFE", "ABBV", "TMO", "MRK", "DHR", "ISRG", "LLY", "AMGN", "BMY", "GILD", "CVS", "MDT", "CI",
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "VLO", "PSX", "OXY", "WMB", "KMI", "HAL", "BKR", "HES", "TRGP"
]

print(f"Fetching 1 year of historical data for {len(tickers)} stocks... (This takes about 15 seconds)")

historical_data = []

# Extract
for ticker in tickers:
    stock = yf.Ticker(ticker)
    hist = stock.history(period="1y") # Grab a full year
    
    # Transform
    if not hist.empty:
        for index, row in hist.iterrows():
            trade_date = index.strftime('%Y-%m-%d')
            close_price = float(row['Close'])
            volume = int(row['Volume'])
            historical_data.append((ticker, trade_date, close_price, volume))

# Load: Bulk insert over 11,000 rows into Oracle instantly
cursor.executemany(
    "INSERT INTO Stock_History (Ticker, Trade_Date, Close_Price, Volume) VALUES (:1, TO_DATE(:2, 'YYYY-MM-DD'), :3, :4)", 
    historical_data
)
conn.commit()
print(f"Success! Loaded {len(historical_data)} rows into Oracle.")
conn.close()