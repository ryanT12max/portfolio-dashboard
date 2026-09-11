# 🏙️ Real Estate ETL Pipeline & Interactive Dashboard

An end-to-end data engineering pipeline and interactive analytics dashboard built to monitor real estate investment opportunities in Houston, TX. 

## 🏗️ Architecture & Tech Stack
* **Data Extraction:** Python `requests` pulling live property data from the RentCast API.
* **Data Validation:** Strict data typing and schema enforcement using **Pydantic**.
* **Database (OLTP):** **Oracle Cloud Autonomous Database** accessed via the modern `oracledb` Thin driver.
* **Data Loading (ETL):** Upsert logic using SQL `MERGE` statements to handle daily schema updates without duplicating records.
* **Frontend Dashboard:** Interactive **Streamlit** web app with dynamic geospatial **Plotly** mapping and an integrated mortgage cash-flow calculator.

## 🚀 Key Engineering Highlights
1. **Production-Grade Logging:** Implemented Python's `logging` module to track API request statuses, validation drop-rates, and database execution states.
2. **Database Administration (DBA) Optimizations:** 
   * Handled Oracle `ORA-12838` table lock errors by dynamically disabling Auto Parallel DML (`ALTER SESSION DISABLE PARALLEL DML`) during batch processing.
   * Configured Oracle Access Control Lists (ACL) for wallet-less TLS connections.
3. **Resilient Data Contracts:** Built Pydantic models to catch `None` types, negative integers, and missing keys before they can corrupt the database schema.

## ⚙️ How to Run Locally
1. Clone the repository.
2. Install dependencies: `pip install -r requirements.txt`
3. Create a `.env` file with your credentials:
   * `RENTCAST_API_KEY`, `DB_USER`, `DB_PASSWORD`, `DB_DSN`
4. Initialize the database schema: `python init_db.py`
5. Run the daily ETL pipeline: `python etl_realestate.py`
6. Launch the frontend: `streamlit run app.py`