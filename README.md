# WellNest — Personal Health & Wellness

WellNest is a responsive Python website for recording personal wellness check-ins and viewing your own trends. It uses Flask, SQLite, Pandas, HTML/CSS, and Chart.js.

The mood journal supports personal reflection only and does not diagnose or treat mental health conditions. The app does not provide medical advice or send appointment notifications. BMI is a limited screening measure, not a diagnosis or a complete measure of health.

## What it includes

- Private accounts with password hashing and per-account records
- Responsive custom website with top navigation (not a Streamlit interface)
- Daily check-ins for weight, sleep, water, exercise, and optional calories
- Add, edit, and delete individual check-ins
- Profile measurements and goals chosen by the user
- Dashboard with workout streak, weekly summaries, and weight history
- Weekly progress and date-filtered analytics`r`n- Private daily mood, energy, and stress journal with notes and a 30-entry trend chart`r`n- Care planner for appointments and follow-ups, including location, notes, completion status, and removal
- Optional sample-data loader and a confirmed reset for the signed-in account's wellness data
- Records table and CSV export
- SQLite persistence
- CSRF protection on forms

The app starts with an empty account. Sample entries are optional and only load when the wellness journal is empty. Reset clears that account's wellness records and goals but keeps the account active.

## Run on Windows (PowerShell)

Open PowerShell in the folder containing `app.py`, then run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app run
```

Open http://127.0.0.1:5000 and create an account.

If the `py` launcher is unavailable but `python` works, replace `py` with `python` in the first command.

The database is created automatically at `instance/wellnest.sqlite3`. Keep the `instance` folder to retain local records. Do not upload that database to a public repository.

## Run on macOS or Linux

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
flask --app app run
```

Open http://127.0.0.1:5000.

## Deploy online

1. Put this project in a private GitHub repository. Do not include `.venv`, `instance/wellnest.sqlite3`, or secrets.
2. Choose a Python host that supports a persistent disk or persistent volume. SQLite data must live on that disk or it may disappear when the service restarts or redeploys.
3. Configure these environment variables in the host:
   - `WELLNEST_SECRET_KEY`: a long random secret that remains the same across restarts.
   - `WELLNEST_DB_PATH`: an absolute path on the host's persistent disk, such as `/var/data/wellnest.sqlite3`.
   - `WELLNEST_HTTPS=1`: enable secure session cookies when the host serves the site over HTTPS.
4. Install dependencies from `requirements.txt`.
5. Start with `gunicorn app:app`.
6. Open the deployed HTTPS address on your phone, tablet, or computer and sign in to the same account.

A local database does not sync between different computers. Cloud access requires a correctly configured deployment and persistent disk. Back up the SQLite database regularly. For a larger multi-instance deployment, use a managed database rather than a SQLite file.

## Python concepts for the viva

- **Functions:** form validation, BMI, progress, streak, chart preparation, and page handlers are separate functions.
- **Lists and dictionaries:** Python dictionaries hold profile and check-in values; lists of database records are prepared for tables and charts.
- **Database handling:** SQLite stores accounts, profile values, and daily entries. Queries use placeholders to keep user data separated and avoid inserting raw form text into SQL.
- **Pandas:** database results are converted to DataFrames for date filtering, averages, weekly totals, and chart preparation.
- **Charts:** Chart.js renders the prepared data as responsive line and bar charts in the browser.
- **Modules:** the Flask app is organized around routes and reusable helpers in `app.py`; page templates and styling are separate files.

## Important deployment note

For local use, the app creates and reuses a private `.secret_key` file. For deployment, set `WELLNEST_SECRET_KEY` in the host settings and keep it the same across restarts. Changing it logs users out. Set `WELLNEST_DB_PATH` to a file on persistent storage so saved entries survive restarts.
