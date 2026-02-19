# PG MIS – Producer Group Management Information System (MVP)

## Tech Stack

- Backend: Python 3, Flask (Blueprints, session auth, RBAC)
- Database: MongoDB
- Frontend: HTML5, CSS3, vanilla JS, Bootstrap (layout only)

## Setup

1. Create a virtualenv and install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Create a `.env` file:

   ```bash
   SECRET_KEY=change-this-in-production
   MONGO_URI=mongodb://localhost:27017/pg_mis
   DEBUG=True
   ```

3. Start MongoDB (`mongod`).

4. Seed demo data:

   ```bash
   python -m seeds.seed_demo
   ```

5. (One-time) Import LokOS SHG Master data (54k+ rows) for cascading dropdowns:

   The XLSX file is included at:
   `seeds/data/SHG_Details_Report_LokOS_12.06.25.xlsx`

   Run:

   ```bash
   python seeds/import_shg_master.py "seeds/data/SHG_Details_Report_LokOS_12.06.25.xlsx"
   ```

6. Run the dev server:

   ```bash
   flask --app run.py --debug run
   ```

   or

   ```bash
   python run.py
   ```

7. Open the app at `http://127.0.0.1:5000/` and log in:

   - `superadmin / Super@123`

## Notes

- **Cascading SHG selection:** Open any PG → **Members** and use the new LokOS SHG selector:
  State → District → Block → Gram Panchayat → Village → SHG Code → SHG Name.
  When selected, the member record stores a snapshot of the full SHG master row.

- This is an MVP skeleton. Extend blueprints for all forms (loans, monthly business, stocks, MPR aggregation, validation workflow) as per your project requirements.
- For production, run with Gunicorn behind Nginx:

   ```bash
   gunicorn -w 4 -b 0.0.0.0:8000 run:app
   ```


## Master Data (TRESP)

Old LokOS master has been removed. Import the new TRESP member master:

```bash
python3 seeds/import_shg_master.py "seeds/data/TRESP_area_SHG_Members_Details.xlsx"
python seeds/import_shg_master.py "seeds/data/TRESP_area_SHG_Members_Details.xlsx"
```

If you already imported LokOS earlier, clear collections in MongoDB first (shg_master, shg_members_master).
