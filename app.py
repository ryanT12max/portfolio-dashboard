import streamlit as st
import pandas as pd
import requests
import time
import oracledb
import yfinance as yf
import datetime
import plotly.express as px

st.set_page_config(layout="wide", page_title="Live Sector Screener")
st.title("Live Sector Screener Dashboard")

# --- API CONFIGURATION ---
st.sidebar.header("API Settings")
api_key = st.sidebar.text_input("Enter Finnhub API Key:", type="password")

# Predefine a list of tickers to query (Keep under 60 total to avoid rate limits)
TICKERS = {
    "Technology": ["AAPL", "MSFT", "NVDA", "ORCL", "CRM", "INTC", "CSCO", "ADBE", "AMD", "QCOM", "TXN", "IBM", "NOW", "INTU", "AMAT"],
    "Healthcare": ["JNJ", "UNH", "PFE", "ABBV", "TMO", "MRK", "DHR", "ISRG", "LLY", "AMGN", "BMY", "GILD", "CVS", "MDT", "CI"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "VLO", "PSX", "OXY", "WMB", "KMI", "HAL", "BKR", "HES", "TRGP"]
}

COMPANY_NAMES = {
    # Technology
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "ORCL": "Oracle", 
    "CRM": "Salesforce", "INTC": "Intel", "CSCO": "Cisco", "ADBE": "Adobe", 
    "AMD": "AMD", "QCOM": "Qualcomm", "TXN": "Texas Instruments", "IBM": "IBM", 
    "NOW": "ServiceNow", "INTU": "Intuit", "AMAT": "Applied Materials",
    
    # Healthcare
    "JNJ": "Johnson & Johnson", "UNH": "UnitedHealth", "PFE": "Pfizer", 
    "ABBV": "AbbVie", "TMO": "Thermo Fisher", "MRK": "Merck", "DHR": "Danaher", 
    "ISRG": "Intuitive Surgical", "LLY": "Eli Lilly", "AMGN": "Amgen", 
    "BMY": "Bristol Myers Squibb", "GILD": "Gilead", "CVS": "CVS Health", 
    "MDT": "Medtronic", "CI": "Cigna",
    
    # Energy
    "XOM": "ExxonMobil", "CVX": "Chevron", "COP": "ConocoPhillips", 
    "SLB": "Schlumberger", "EOG": "EOG Resources", "MPC": "Marathon Petroleum", 
    "VLO": "Valero", "PSX": "Phillips 66", "OXY": "Occidental", "WMB": "Williams Cos.", 
    "KMI": "Kinder Morgan", "HAL": "Halliburton", "BKR": "Baker Hughes", 
    "HES": "Hess", "TRGP": "Targa Resources"
}

# Cache the data for 1 hour (3600 seconds) so you don't exhaust your API limits
@st.cache_data(ttl=3600)
def fetch_live_data(key):
    data = []
    for sector, symbols in TICKERS.items():
        for sym in symbols:
            # Finnhub endpoint for basic financials
            url = f"https://finnhub.io/api/v1/stock/metric?symbol={sym}&metric=all&token={key}"
            response = requests.get(url)
            
            if response.status_code == 200:
                json_data = response.json()
                metrics = json_data.get("metric", {})
                
                data.append({
                    "TICKER": sym,
                    "COMPANY": COMPANY_NAMES.get(sym, "Unknown"),
                    "SECTOR": sector,
                    "PE_RATIO": metrics.get("peBasicExclExtraTTM", 0) or 0,
                    "PB_RATIO": metrics.get("pbAnnual", 0) or 0,
                    "EPS": metrics.get("epsExclExtraItemsTTM", 0) or 0,
                    "ROE": metrics.get("roeTTM", 0) or 0,
                    "PROFIT_MARGIN": metrics.get("netProfitMarginTTM", 0) or 0,
                    "DE_RATIO": metrics.get("totalDebt/totalEquityAnnual", 0) or 0
                })
            # Pause briefly to respect the 60 calls/min rate limit
            time.sleep(0.5) 
    return pd.DataFrame(data)

@st.cache_data(ttl=3600)
def fetch_stock_insights(ticker_symbol, key):
    # 1. Fetch Growth from Yahoo Finance
    stock = yf.Ticker(ticker_symbol)
    info = stock.info
    
    # 2. Fetch Summarized News from Finnhub (Last 7 Days)
    end_date = datetime.datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    
    finnhub_url = f"https://finnhub.io/api/v1/company-news?symbol={ticker_symbol}&from={start_date}&to={end_date}&token={key}"
    
    news_data = []
    try:
        response = requests.get(finnhub_url)
        if response.status_code == 200:
            news_data = response.json()
    except Exception as e:
        print(f"Error fetching news: {e}")
        
    return info, news_data

if not api_key:
    st.warning("Please enter your Finnhub API Key in the sidebar to load data.")
    st.stop()

with st.spinner("Fetching live market data..."):
    df = fetch_live_data(api_key)

# --- FETCH PERSONAL PORTFOLIO ---
@st.cache_resource
def init_oracle():
    return oracledb.connect(
        user="ADMIN",
        password="pi11zNcpv$!db",
        dsn="marketdevdb_high",
        config_dir="wallet",
        wallet_location="wallet",
        wallet_password="rennJenn5011"
    )

conn = init_oracle()

# CTE to aggregate multiple buys/sells into a single row per Ticker
portfolio_query = """
    WITH PortfolioSummary AS (
        SELECT 
            Ticker, 
            SUM(CASE WHEN Txn_Type = 'BUY' THEN Shares ELSE -Shares END) as Total_Shares,
            AVG(Price_Paid) as Avg_Cost_Basis
        FROM Portfolio_Ledger
        GROUP BY Ticker
    )
    SELECT * FROM PortfolioSummary WHERE Total_Shares > 0
"""
df_portfolio = pd.read_sql(portfolio_query, con=conn)

# Merge on the Ticker column
my_portfolio = pd.merge(df_portfolio, df, left_on='TICKER', right_on='TICKER', how='inner')

# Add this layout to a new tab at the bottom of app.py
st.header("My Personal Portfolio")

# Select only the columns you want to view side-by-side
view_cols = ['TICKER', 'TOTAL_SHARES', 'AVG_COST_BASIS', 'PE_RATIO', 'PROFIT_MARGIN', 'DE_RATIO']
st.dataframe(my_portfolio[view_cols], use_container_width=True)

# --- DASHBOARD CONTROLS ---
st.sidebar.header("Dashboard Controls")

sector_list = ["All Sectors"] + list(df['SECTOR'].unique())
selected_sector = st.sidebar.selectbox("Filter by Sector:", sector_list)

if selected_sector != "All Sectors":
    df = df[df['SECTOR'] == selected_sector]

# --- SORTING LOGIC ---
st.sidebar.subheader("Sort Top Results")
sort_metric = st.sidebar.selectbox(
    "Sort by:", 
    ["PE_RATIO", "ROE", "PROFIT_MARGIN", "DE_RATIO"]
)

# Determine if we should sort ascending (low to high) or descending (high to low)
# E.g., We want LOW P/E and Debt, but HIGH ROE and Margins
is_ascending = True if sort_metric in ["PE_RATIO", "DE_RATIO"] else False

# Sort the dataframe, then group by sector to keep the top 15 in each category
df = df.sort_values(by=sort_metric, ascending=is_ascending)
df = df.groupby('SECTOR').head(15)

# --- HIGHLIGHTING LOGIC ---
def color_valuation(val):
    if pd.isna(val) or val == 0: return ''
    if val < 15: return 'background-color: rgba(39, 174, 96, 0.3)'
    if val > 25: return 'background-color: rgba(231, 76, 60, 0.3)'
    return ''

def color_profitability(val):
    if pd.isna(val) or val == 0: return ''
    if val > 20: return 'background-color: rgba(39, 174, 96, 0.3)'
    if val < 10: return 'background-color: rgba(231, 76, 60, 0.3)'
    return ''

def color_debt(val):
    if pd.isna(val) or val == 0: return ''
    if val < 1.0: return 'background-color: rgba(39, 174, 96, 0.3)'
    if val > 1.5: return 'background-color: rgba(231, 76, 60, 0.3)'
    return ''

# --- METRIC GLOSSARY ---
with st.expander("📚 Metric Definitions for Beginners (Click to open)"):
    st.markdown("""
    **Valuation (Is the stock cheap or expensive?)**
    *   **P/E Ratio (Price-to-Earnings):** Think of this as the "price tag" on a company's profits. It tells you how many dollars you have to pay to get $1 of their earnings.
        *   🟢 **Lower is usually better (Cheaper):** You are getting a good deal on their profits. A P/E under 15 is often considered a "value" stock.
        *   🔴 **Higher is usually worse (Expensive):** You are paying a premium, usually because investors expect massive growth in the future.
    *   **P/B Ratio (Price-to-Book):** Compares the stock's price to the "scrap value" of the company (what it would be worth if it sold off every building, patent, and piece of inventory tomorrow).
        *   🟢 **Lower is better (Undervalued):** A number under 1.0 means the stock is literally selling for *less* than its physical parts are worth!
        *   🔴 **Higher is worse (Overvalued):** You are paying a large premium just for the company's brand name or future potential.
    
    **Profitability (Is the company making good money?)**
    *   **EPS (Earnings Per Share):** The total profit of the company divided by the number of shares. 
        *   🟢 **Higher is better:** The company is generating more profit for every share you own.
        *   🔴 **Lower or Negative is worse:** The company is struggling to make a profit or is actively losing money.
    *   **ROE (Return on Equity):** Think of this as the "Return on Investment" for the CEO. How effectively is management using investors' money to generate profit?
        *   🟢 **Higher is better (Efficient):** They are excellent at turning your invested dollars into profit. Anything above 15% to 20% is generally considered great.
        *   🔴 **Lower is worse (Inefficient):** Management is struggling to generate a good return on the money they have.
    *   **Profit Margin:** The percentage of revenue the company actually keeps as pure profit after paying all its bills, employees, and taxes.
        *   🟢 **Higher is better:** The company has strong pricing power and keeps costs low. 
        *   🔴 **Lower is worse:** The company has tight margins and makes very little actual profit on the items they sell.
    
    **Health (Is the company financially safe?)**
    *   **D/E Ratio (Debt-to-Equity):** How much debt the company uses to fund its operations compared to its own money.
        *   🟢 **Lower is better (Safer):** The company relies on its own cash to operate, reducing the risk of bankruptcy.
        *   🔴 **Higher is worse (Riskier):** The company is heavily burdened by loans. A ratio over 1.5 or 2.0 can signal high risk if interest rates rise.
    """)

# --- METRIC GROUPING ---
tab1, tab2, tab3 = st.tabs([
    "Valuation Metrics", 
    "Profitability & Efficiency", 
    "Financial Health & Solvency"
])

base_cols = ['TICKER', 'COMPANY', 'SECTOR']

with tab1:
    st.subheader(f"Top Valuation Results")
    val_cols = base_cols + ['PE_RATIO', 'PB_RATIO']
    styled_val = df[val_cols].style.map(color_valuation, subset=['PE_RATIO'])
    st.dataframe(styled_val, use_container_width=True)

with tab2:
    st.subheader(f"Top Profitability Results")
    prof_cols = base_cols + ['EPS', 'ROE', 'PROFIT_MARGIN']
    styled_prof = df[prof_cols].style.map(color_profitability, subset=['ROE', 'PROFIT_MARGIN'])
    st.dataframe(styled_prof, use_container_width=True)

with tab3:
    st.subheader(f"Top Health Results")
    health_cols = base_cols + ['DE_RATIO']
    styled_health = df[health_cols].style.map(color_debt, subset=['DE_RATIO'])
    st.dataframe(styled_health, use_container_width=True)

# --- HISTORICAL TRENDS & MOVING AVERAGES ---
st.divider()
st.header("Technical Analysis: Moving Averages")

# 1. Create a dropdown so the user can select ANY of the 45 stocks
# We use the unique tickers from the live dataframe we built earlier
selected_trend_ticker = st.selectbox("Select a Stock to Analyze:", df['TICKER'].unique())

st.subheader(f"1-Year Trend for {selected_trend_ticker}")

# 2. Update the CTE to calculate both 50-day and 200-day averages
trend_query = f"""
    WITH MovingAverages AS (
        SELECT 
            Trade_Date,
            Close_Price,
            AVG(Close_Price) OVER (ORDER BY Trade_Date ROWS 49 PRECEDING) as MA_50,
            AVG(Close_Price) OVER (ORDER BY Trade_Date ROWS 199 PRECEDING) as MA_200
        FROM Stock_History
        WHERE Ticker = '{selected_trend_ticker}'
    )
    SELECT * FROM MovingAverages 
    ORDER BY Trade_Date ASC
"""

try:
    df_trends = pd.read_sql(trend_query, con=conn)
    df_trends.set_index('TRADE_DATE', inplace=True)
    
    # 3. Streamlit will plot all three lines on the same graph
    st.line_chart(df_trends[['CLOSE_PRICE', 'MA_50', 'MA_200']])
except Exception as e:
    st.error(f"Error loading chart: {e}")

# --- GROWTH TARGETS & RECENT NEWS ---
st.divider()

with st.spinner(f"Fetching latest analyst estimates and news for {selected_trend_ticker}..."):
    info, news = fetch_stock_insights(selected_trend_ticker, api_key)

# Split the screen into two columns
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("📈 Forward Growth Estimates")
    
    # Extract variables with fallbacks in case the data is missing
    current_price = info.get('currentPrice', 'N/A')
    target_price = info.get('targetMeanPrice', 'N/A')
    earnings_growth = info.get('earningsGrowth', 'N/A')
    
    if current_price != 'N/A' and target_price != 'N/A':
        growth_pct = ((target_price - current_price) / current_price) * 100
        
        # Display using Streamlit's built-in metric component
        st.metric(
            label="Wall St. 1-Year Price Target", 
            value=f"${target_price:,.2f}", 
            delta=f"{growth_pct:.2f}% Implied Growth"
        )
    else:
        st.write("1-Year Price Target: Data currently unavailable.")

    if earnings_growth != 'N/A':
        # earnings_growth is returned as a decimal (e.g., 0.15 for 15%)
        st.metric(
            label="Projected Earnings Growth",
            value=f"{(earnings_growth * 100):.2f}%",
            help="Analysts' consensus on the company's long-term profitability growth trajectory."
        )

with col2:
    st.subheader("📰 Recent News & Summaries")
    
    if news:
        articles_shown = 0
        for article in news:
            # Limit to top 3 articles to keep the dashboard clean
            if articles_shown >= 3:
                break
                
            headline = article.get('headline', '')
            summary = article.get('summary', '')
            source = article.get('source', 'Unknown Source')
            url = article.get('url', '#')
            
            # Only display articles that Finnhub successfully summarized
            if headline and summary:
                st.markdown(f"**[{headline}]({url})** *(Source: {source})*")
                st.markdown(f"> {summary}")
                st.write("") # Adds a small space between articles
                articles_shown += 1
                
        if articles_shown == 0:
            st.write("No summarized news found for this ticker.")
    else:
        st.write("No recent news found for this ticker.")

# --- REAL ESTATE OPPORTUNITIES ---
st.divider()
st.header("🏠 Real Estate & Development Opportunities")
st.write("Filtering for active Multi-Family and Townhome listings.")

# 1. The SQL JOIN Query
# We use INNER JOINs to link the APN (Parcel_ID) across all three tables
real_estate_query = """
    SELECT 
        p.Street_Address,
        p.City,
        p.Property_Type,
        p.Latitude AS "latitude",
        p.Longitude AS "longitude",
        l.Lot_Size_SqFt,
        l.Year_Built,
        l.Has_Deed_Restrictions,
        m.Asking_Price,
        m.Est_Monthly_Rent,
        ROUND(((m.Est_Monthly_Rent * 12) / NULLIF(m.Asking_Price, 0)) * 100, 2) AS Gross_Yield_Pct
    FROM Properties p
    JOIN Lot_Characteristics l ON p.Parcel_ID = l.Parcel_ID
    JOIN Market_Listings m ON p.Parcel_ID = m.Parcel_ID
    WHERE m.Status = 'Active' 
      AND p.Property_Type IN ('Duplex', 'Townhome')
    ORDER BY Gross_Yield_Pct DESC
"""

# 2. Execute and Display
try:
    df_real_estate = pd.read_sql(real_estate_query, con=conn)
    
    if df_real_estate.empty:
        st.info("The database tables are currently empty. Run an insert script to load properties!")
    else:
        # 1. Display the table with single-row selection enabled
        event = st.dataframe(
            df_real_estate.drop(columns=['latitude', 'longitude']).style.format({
                'ASKING_PRICE': '${:,.0f}',
                'EST_MONTHLY_RENT': '${:,.0f}',
                'GROSS_YIELD_PCT': '{:.2f}%',
                'LOT_SIZE_SQFT': '{:,.0f}'
            }),
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="real_estate_table"
        )
        
        # Extract the selected row index if a user clicks a property
        selected_rows = event.selection.rows
        selected_address = None
        if selected_rows:
            selected_idx = selected_rows[0]
            selected_address = df_real_estate.iloc[selected_idx]['STREET_ADDRESS']
        
        # 2. Display the map safely
        st.subheader("📍 Property Map")
        
        # Use Pandas to drop properties missing coordinates just for the map
        df_map = df_real_estate.dropna(subset=['latitude', 'longitude'])
        
        if not df_map.empty:
            # Default view: Center of Houston
            map_center = {"lat": 29.7604, "lon": -95.3698}
            zoom_level = 9
            
            # If a row in the table is clicked, shift camera to that property!
            if selected_address:
                selected_prop = df_map[df_map['STREET_ADDRESS'] == selected_address]
                if not selected_prop.empty:
                    map_center = {
                        "lat": float(selected_prop['latitude'].values[0]),
                        "lon": float(selected_prop['longitude'].values[0])
                    }
                    zoom_level = 14  # Zoom in close to street view

            # Create an interactive Plotly map
            fig = px.scatter_map(
                df_map,
                lat="latitude",
                lon="longitude",
                hover_name="STREET_ADDRESS",
                hover_data={
                    "latitude": False,
                    "longitude": False,
                    "ASKING_PRICE": True,
                    "GROSS_YIELD_PCT": True
                },
                color="GROSS_YIELD_PCT",
                color_continuous_scale="Viridis",
                center=map_center,
                zoom=zoom_level,
                height=500
            )
            
            # Make standard dots larger
            fig.update_traces(marker_size=15)
            
            # Overlay a distinct red marker on the selected property if clicked
            if selected_address:
                selected_prop = df_map[df_map['STREET_ADDRESS'] == selected_address]
                if not selected_prop.empty:
                    import plotly.graph_objects as go
                    fig.add_trace(go.Scattermap(
                        lat=selected_prop['latitude'],
                        lon=selected_prop['longitude'],
                        mode='markers',
                        marker=dict(size=26, color='red', symbol='circle'),
                        name='Selected Property',
                        hovertext=selected_prop['STREET_ADDRESS']
                    ))
            
            fig.update_layout(map_style="open-street-map", margin={"r":0,"t":0,"l":0,"b":0})
            st.plotly_chart(fig, use_container_width=True)

            # --- INTERACTIVE MORTGAGE ESTIMATOR ---
            st.divider()
            st.subheader("💰 Interactive Mortgage Estimator")

            col1, col2 = st.columns([1, 1.5])

            with col1:
                # Sync the calculator selectbox with the table selection if possible
                default_idx = 0
                if selected_address and selected_address in df_map['STREET_ADDRESS'].tolist():
                    default_idx = df_map['STREET_ADDRESS'].tolist().index(selected_address)

                selected_address_calc = st.selectbox(
                    "Select a Property:", 
                    df_map['STREET_ADDRESS'].tolist(),
                    index=default_idx
                )
                
                property_price = float(df_map[df_map['STREET_ADDRESS'] == selected_address_calc]['ASKING_PRICE'].values[0])
                est_rent = float(df_map[df_map['STREET_ADDRESS'] == selected_address_calc]['EST_MONTHLY_RENT'].values[0])
                
                down_payment_pct = st.slider("Down Payment (%)", min_value=0, max_value=100, value=20, step=5)
                interest_rate = st.number_input("Annual Interest Rate (%)", min_value=1.0, max_value=15.0, value=6.5, step=0.1)
                loan_term = st.selectbox("Loan Term (Years)", [15, 20, 30], index=2)

            with col2:
                down_payment_amount = property_price * (down_payment_pct / 100)
                principal = property_price - down_payment_amount
                
                r = (interest_rate / 100) / 12
                n = loan_term * 12
                
                if principal > 0 and r > 0:
                    monthly_mortgage = principal * (r * (1 + r)**n) / ((1 + r)**n - 1)
                else:
                    monthly_mortgage = 0
                
                est_cash_flow = est_rent - monthly_mortgage

                st.write("### Property Financials")
                st.write(f"**Asking Price:** ${property_price:,.0f} | **Principal Loan:** ${principal:,.0f}")
                
                metric_col1, metric_col2 = st.columns(2)
                metric_col1.metric(label="Est. Monthly Payment (P&I)", value=f"${monthly_mortgage:,.0f}")
                metric_col2.metric(
                    label="Est. Monthly Cash Flow", 
                    value=f"${est_cash_flow:,.0f}",
                    delta=f"${est_cash_flow:,.0f}",
                    delta_color="normal"
                )
        else:
            st.warning("Map Data Missing: Run python etl_realestate.py to backfill the geographic coordinates!")

except Exception as e:
    st.error(f"Error loading real estate data: {e}")