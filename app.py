from __future__ import annotations

# Standard-library modules provide CSV output, file paths, database access, security tokens, and date/time handling.
# functools.wraps keeps a decorated route recognizable to Flask.
import io
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

# Pandas is used to organize saved check-ins and calculate summaries for the dashboard and charts.
import pandas as pd
from flask import Flask, abort, flash, make_response, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

# Keep the database and local secret beside this app unless deployment settings override their locations.
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("WELLNEST_DB_PATH", INSTANCE_DIR / "wellnest.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Create a stable local session key once; Flask uses it to sign login-session cookies.
def local_secret_key():
    key_file = BASE_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()

# Create the Flask application object that receives browser requests and returns pages.
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("WELLNEST_SECRET_KEY") or local_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("WELLNEST_HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
# Protect form submissions from cross-site request forgery.
CSRFProtect(app)


# Open one SQLite connection and return rows by column name for readable database code.
def connect_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


# Create the tables on first run; IF NOT EXISTS preserves existing users and records.
def init_db():
    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                height_cm REAL,
                start_weight REAL,
                target_weight REAL,
                daily_water_goal_l REAL,
                sleep_goal_h REAL,
                weekly_exercise_goal_min REAL,
                calorie_goal REAL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                entry_date TEXT NOT NULL,
                weight_kg REAL,
                sleep_h REAL,
                water_l REAL,
                exercise_type TEXT,
                exercise_min REAL,
                calories REAL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, entry_date)
            );
            CREATE TABLE IF NOT EXISTS mood_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                log_date TEXT NOT NULL,
                mood INTEGER NOT NULL CHECK(mood BETWEEN 1 AND 5),
                energy INTEGER NOT NULL CHECK(energy BETWEEN 1 AND 5),
                stress INTEGER NOT NULL CHECK(stress BETWEEN 1 AND 5),
                note TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, log_date)
            );
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'done')),
                created_at TEXT NOT NULL
            );
        """)
        db.commit()


# Read the signed-in user ID from the session, then fetch only that account.
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with connect_db() as db:
        return db.execute("SELECT id, email FROM users WHERE id = ?", (uid,)).fetchone()


# Reusable access-control decorator: send visitors to sign-in before private pages run.
def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


# Return profile settings for this account and fill missing settings with None.
def get_profile(uid):
    profile = {
        "height_cm": None, "start_weight": None, "target_weight": None,
        "daily_water_goal_l": None, "sleep_goal_h": None,
        "weekly_exercise_goal_min": None, "calorie_goal": None,
    }
    with connect_db() as db:
        row = db.execute("SELECT * FROM profiles WHERE user_id = ?", (uid,)).fetchone()
    if row:
        profile.update(dict(row))
    return profile


# Read only this user’s check-ins and convert them into a Pandas DataFrame for analysis.
def get_entries(uid):
    with connect_db() as db:
        rows = db.execute(
            "SELECT * FROM entries WHERE user_id = ? ORDER BY entry_date ASC", (uid,)
        ).fetchall()
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty:
        return pd.DataFrame(columns=[
            "id", "entry_date", "weight_kg", "sleep_h", "water_l",
            "exercise_type", "exercise_min", "calories",
        ])
    frame["entry_date"] = pd.to_datetime(frame["entry_date"], errors="coerce")
    for col in ("weight_kg", "sleep_h", "water_l", "exercise_min", "calories"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


# Validate an optional numeric form field and return either a number or a clear error.
def parse_optional_number(name, label, low, high):
    raw = request.form.get(name, "").strip()
    if raw == "":
        return None, None
    try:
        value = float(raw)
    except ValueError:
        return None, f"{label} must be a number."
    if not low <= value <= high:
        return None, f"{label} must be between {low:g} and {high:g}."
    return value, None


# Collect and validate a daily check-in before any values are written to SQLite.
def parse_entry_form():
    errors = []
    try:
        entry_day = date.fromisoformat(request.form.get("entry_date", ""))
    except ValueError:
        entry_day = date.today()
        errors.append("Choose a valid date.")
    if entry_day > date.today():
        errors.append("Entries cannot be dated in the future.")

    values = {}
    for name, label, low, high in [
        ("weight_kg", "Weight", 20, 400),
        ("sleep_h", "Sleep hours", 0, 24),
        ("water_l", "Water (litres)", 0, 30),
        ("exercise_min", "Exercise duration", 0, 1000),
        ("calories", "Calories", 0, 20000),
    ]:
        value, error = parse_optional_number(name, label, low, high)
        values[name] = value
        if error:
            errors.append(error)

    if all(values[name] is None for name in ("weight_kg", "sleep_h", "water_l", "exercise_min", "calories")):
        errors.append("Add at least one measurement before saving this check-in.")

    kind = request.form.get("exercise_type", "").strip()[:60]
    if values["exercise_min"] and values["exercise_min"] > 0 and not kind:
        errors.append("Choose an exercise type when exercise duration is above zero.")
    values["exercise_type"] = kind or None
    values["entry_date"] = entry_day.isoformat()
    return values, errors


# BMI formula: kilograms divided by height in metres squared; this is only a screening estimate.
def bmi_value(weight, height_cm):
    if weight is None or not height_cm:
        return None
    height_m = float(height_cm) / 100
    return float(weight) / (height_m * height_m) if height_m > 0 else None


# Count consecutive days with recorded movement, allowing a streak that ended yesterday.
def workout_streak(frame, today=None):
    today = today or date.today()
    if frame.empty:
        return 0
    active_days = set(frame.loc[frame["exercise_min"].fillna(0) > 0, "entry_date"].dt.date)
    cursor = today if today in active_days else today - timedelta(days=1)
    streak = 0
    while cursor in active_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# Prepare chart labels and series in Python; the page template passes them to Chart.js.
def chart_payload(frame, goal=None):
    if frame.empty:
        return {"labels": [], "weights": [], "average": [], "goal": []}
    weight = frame.dropna(subset=["weight_kg"]).sort_values("entry_date").copy()
    if weight.empty:
        return {"labels": [], "weights": [], "average": [], "goal": []}
    weight = weight.set_index("entry_date")
    rolling = weight["weight_kg"].rolling("7D", min_periods=1).mean()
    return {
        "labels": [d.strftime("%b %d") for d in weight.index],
        "weights": [round(float(x), 2) for x in weight["weight_kg"]],
        "average": [round(float(x), 2) for x in rolling],
        "goal": [float(goal)] * len(weight) if goal is not None else [],
    }


# Calculate bounded progress from a starting value toward the user’s chosen goal.
def goal_progress(start, current, target):
    if start is None or current is None or target is None or float(start) == float(target):
        return None
    fraction = (float(current) - float(start)) / (float(target) - float(start))
    return max(0.0, min(1.0, fraction))


# Read and normalize optional date filters used by insights and records pages.
def date_range(default_days=30):
    default_start = date.today() - timedelta(days=default_days - 1)
    try:
        start = date.fromisoformat(request.args.get("start", default_start.isoformat()))
        end = date.fromisoformat(request.args.get("end", date.today().isoformat()))
    except ValueError:
        start, end = default_start, date.today()
    if end < start:
        start, end = end, start
    return start, end


# Make the signed-in user and current date available to all HTML templates.
@app.context_processor
def inject_global():
    return {"nav_user": current_user(), "today": date.today()}


# Dashboard: gather current and weekly summaries, then render the overview page.
@app.route("/")
@login_required
def home():
    user = current_user()
    frame = get_entries(user["id"])
    profile = get_profile(user["id"])
    weights = frame.dropna(subset=["weight_kg"])
    latest = weights.tail(1)
    weight = float(latest.iloc[0]["weight_kg"]) if not latest.empty else None
    weight_date = latest.iloc[0]["entry_date"].date() if not latest.empty else None
    bmi = bmi_value(weight, profile["height_cm"])
    today_rows = frame[frame["entry_date"].dt.date == date.today()] if not frame.empty else frame
    today_row = today_rows.iloc[-1].to_dict() if not today_rows.empty else {}
    week_start = date.today() - timedelta(days=date.today().weekday())
    week = frame[frame["entry_date"].dt.date >= week_start] if not frame.empty else frame
    exercise_week = float(week["exercise_min"].fillna(0).sum()) if not week.empty else 0
    sleep_vals = week["sleep_h"].dropna() if not week.empty else pd.Series(dtype=float)
    water_vals = week["water_l"].dropna() if not week.empty else pd.Series(dtype=float)
    start_weight = profile["start_weight"]
    if start_weight is None and not weights.empty:
        start_weight = float(weights.iloc[0]["weight_kg"])
    progress_value = goal_progress(start_weight, weight, profile["target_weight"])
    recent = frame.tail(5).iloc[::-1].to_dict("records") if not frame.empty else []
    return render_template(
        "home.html", weight=weight, weight_date=weight_date, bmi=bmi, profile=profile,
        streak=workout_streak(frame), today_row=today_row, exercise_week=exercise_week,
        sleep_avg=float(sleep_vals.mean()) if not sleep_vals.empty else None,
        water_avg=float(water_vals.mean()) if not water_vals.empty else None,
        goal_progress=progress_value, recent=recent,
        chart=chart_payload(frame.tail(120), profile["target_weight"]),
        entry_count=len(frame),
    )


# Registration: validate details, hash the password, create the user and their profile.
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if "@" not in email or len(email) > 254:
            flash("Enter a valid email address.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        else:
            try:
                with connect_db() as db:
                    cur = db.execute(
                        "INSERT INTO users(email, password_hash, created_at) VALUES (?, ?, ?)",
                        (email, generate_password_hash(password), datetime.utcnow().isoformat()),
                    )
                    db.execute("INSERT INTO profiles(user_id) VALUES (?)", (cur.lastrowid,))
                    db.commit()
                session.clear()
                session["user_id"] = cur.lastrowid
                session.permanent = True
                flash("Your private WellNest account is ready.", "success")
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists. Sign in instead.", "error")
    return render_template("auth.html", mode="register")


# Sign-in: compare the submitted password with its stored secure hash.
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with connect_db() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            return redirect(url_for("home"))
        flash("Email or password is incorrect.", "error")
    return render_template("auth.html", mode="login")


# Clear the login session when the user signs out.
@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# Show the check-in form or validate and save the submitted daily record.
@app.route("/log", methods=["GET", "POST"])
@login_required
def log_entry():
    uid = current_user()["id"]
    if request.method == "POST":
        values, errors = parse_entry_form()
        if not errors:
            try:
                with connect_db() as db:
                    db.execute("""
                        INSERT INTO entries(user_id, entry_date, weight_kg, sleep_h, water_l,
                            exercise_type, exercise_min, calories, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (uid, values["entry_date"], values["weight_kg"], values["sleep_h"],
                          values["water_l"], values["exercise_type"], values["exercise_min"],
                          values["calories"], datetime.utcnow().isoformat()))
                    db.commit()
                flash("Your check-in has been saved.", "success")
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                errors.append("There is already an entry for this date. Edit that record instead.")
        for error in errors:
            flash(error, "error")
    return render_template("entry_form.html", entry=None, action="Add check-in")


# Edit only a record that belongs to the signed-in user.
@app.route("/entry/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_entry(entry_id):
    uid = current_user()["id"]
    with connect_db() as db:
        row = db.execute("SELECT * FROM entries WHERE id=? AND user_id=?", (entry_id, uid)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        values, errors = parse_entry_form()
        if not errors:
            try:
                with connect_db() as db:
                    db.execute("""
                        UPDATE entries SET entry_date=?, weight_kg=?, sleep_h=?, water_l=?,
                            exercise_type=?, exercise_min=?, calories=?
                        WHERE id=? AND user_id=?
                    """, (values["entry_date"], values["weight_kg"], values["sleep_h"],
                          values["water_l"], values["exercise_type"], values["exercise_min"],
                          values["calories"], entry_id, uid))
                    db.commit()
                flash("Your check-in has been updated.", "success")
                return redirect(url_for("records"))
            except sqlite3.IntegrityError:
                errors.append("An entry already exists for the selected date.")
        for error in errors:
            flash(error, "error")
    return render_template("entry_form.html", entry=dict(row), action="Edit check-in")


# Delete one of the signed-in user’s entries; the POST form is CSRF-protected.
@app.post("/entry/<int:entry_id>/delete")
@login_required
def delete_entry(entry_id):
    with connect_db() as db:
        result = db.execute("DELETE FROM entries WHERE id=? AND user_id=?",
                            (entry_id, current_user()["id"]))
        db.commit()
    flash("Entry deleted." if result.rowcount else "That entry could not be found.",
          "success" if result.rowcount else "error")
    return redirect(url_for("records"))


# Save height and personal goals, which are used for BMI and progress displays.
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    uid = current_user()["id"]
    keys = [
        ("height_cm", "Height", 50, 260), ("start_weight", "Starting weight", 20, 300),
        ("target_weight", "Target weight", 20, 300), ("daily_water_goal_l", "Daily water goal", 0, 15),
        ("sleep_goal_h", "Sleep goal", 0, 24), ("weekly_exercise_goal_min", "Weekly exercise goal", 0, 5000),
        ("calorie_goal", "Calorie goal", 0, 20000),
    ]
    if request.method == "POST":
        values, errors = {}, []
        for key, label, low, high in keys:
            values[key], error = parse_optional_number(key, label, low, high)
            if error:
                errors.append(error)
        if not errors:
            with connect_db() as db:
                db.execute("""
                    INSERT INTO profiles(user_id, height_cm, start_weight, target_weight,
                        daily_water_goal_l, sleep_goal_h, weekly_exercise_goal_min, calorie_goal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        height_cm=excluded.height_cm, start_weight=excluded.start_weight,
                        target_weight=excluded.target_weight, daily_water_goal_l=excluded.daily_water_goal_l,
                        sleep_goal_h=excluded.sleep_goal_h,
                        weekly_exercise_goal_min=excluded.weekly_exercise_goal_min,
                        calorie_goal=excluded.calorie_goal
                """, (uid, *(values[key] for key, _, _, _ in keys)))
                db.commit()
            flash("Your profile and goals are saved.", "success")
            return redirect(url_for("profile"))
        for error in errors:
            flash(error, "error")
    return render_template("profile.html", profile=get_profile(uid))


# Summarize check-ins for the selected week, including averages and total movement.
@app.route("/progress")
@login_required
def progress():
    frame = get_entries(current_user()["id"])
    try:
        selected = date.fromisoformat(request.args.get("week", date.today().isoformat()))
    except ValueError:
        selected = date.today()
    monday = selected - timedelta(days=selected.weekday())
    sunday = monday + timedelta(days=6)
    week = frame[(frame["entry_date"].dt.date >= monday) & (frame["entry_date"].dt.date <= sunday)] if not frame.empty else frame
    profile_data = get_profile(current_user()["id"])
    def mean(col):
        values = week[col].dropna() if not week.empty else pd.Series(dtype=float)
        return float(values.mean()) if not values.empty else None
    weights = week["weight_kg"].dropna() if not week.empty else pd.Series(dtype=float)
    summary = {
        "days_logged": len(week),
        "exercise_total": float(week["exercise_min"].fillna(0).sum()) if not week.empty else 0,
        "sleep_avg": mean("sleep_h"), "water_avg": mean("water_l"), "calories_avg": mean("calories"),
        "weight_change": float(weights.iloc[-1] - weights.iloc[0]) if len(weights) >= 2 else None,
    }
    rows = week.iloc[::-1].to_dict("records") if not week.empty else []
    return render_template("progress.html", summary=summary, profile=profile_data,
                           week=rows, monday=monday, sunday=sunday, selected=selected.isoformat())


# Filter saved data by date and prepare values for summary cards and charts.
@app.route("/insights")
@login_required
def insights():
    frame = get_entries(current_user()["id"])
    start, end = date_range()
    part = frame[(frame["entry_date"].dt.date >= start) & (frame["entry_date"].dt.date <= end)] if not frame.empty else frame
    chart = chart_payload(part, get_profile(current_user()["id"])["target_weight"])
    def metric(col):
        values = part[col].dropna() if not part.empty else pd.Series(dtype=float)
        return round(float(values.mean()), 1) if not values.empty else None
    summary = {
        "records": len(part), "weight": metric("weight_kg"), "sleep": metric("sleep_h"),
        "water": metric("water_l"),
        "exercise": round(float(part["exercise_min"].fillna(0).sum()), 1) if not part.empty else 0,
    }
    daily = []
    if not part.empty:
        work = part.copy()
        work["day_label"] = work["entry_date"].dt.strftime("%b %d")
        daily = work[["day_label", "sleep_h", "water_l", "exercise_min"]].to_dict("records")
    return render_template("insights.html", start=start.isoformat(), end=end.isoformat(),
                           chart=chart, daily=daily, summary=summary)


# Display the user’s check-ins as a date-filterable table.
@app.route("/records")
@login_required
def records():
    frame = get_entries(current_user()["id"])
    start, end = date_range(default_days=3650)
    part = frame[(frame["entry_date"].dt.date >= start) & (frame["entry_date"].dt.date <= end)] if not frame.empty else frame
    rows = part.iloc[::-1].to_dict("records") if not part.empty else []
    return render_template("records.html", rows=rows, start=start.isoformat(), end=end.isoformat())


# Convert the signed-in user’s records to a downloadable CSV file.
@app.route("/records.csv")
@login_required
def export_csv():
    frame = get_entries(current_user()["id"])
    out = io.StringIO()
    columns = ["entry_date", "weight_kg", "sleep_h", "water_l", "exercise_type", "exercise_min", "calories"]
    if frame.empty:
        pd.DataFrame(columns=columns).to_csv(out, index=False)
    else:
        frame = frame.drop(columns=["id"], errors="ignore")
        frame["entry_date"] = frame["entry_date"].dt.strftime("%Y-%m-%d")
        frame[columns].to_csv(out, index=False)
    response = make_response(out.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=wellnest-records.csv"
    return response


# Public project information page; it does not require login.
@app.route("/about")
def about():
    return render_template("about.html")


# Data tools: load fictional sample records into an empty journal or reset this user’s wellness data.
@app.route("/data-tools", methods=["GET", "POST"])
@login_required
def data_tools():
    uid = current_user()["id"]
    if request.method == "POST":
        action = request.form.get("action", "")
        with connect_db() as db:
            if action == "reset":
                db.execute("DELETE FROM entries WHERE user_id=?", (uid,))
                db.execute("DELETE FROM mood_logs WHERE user_id=?", (uid,))
                db.execute("DELETE FROM appointments WHERE user_id=?", (uid,))
                db.execute("""UPDATE profiles SET height_cm=NULL, start_weight=NULL, target_weight=NULL,
                    daily_water_goal_l=NULL, sleep_goal_h=NULL, weekly_exercise_goal_min=NULL, calorie_goal=NULL
                    WHERE user_id=?""", (uid,))
                db.commit()
                flash("Your wellness data and goals were reset. Your account is still active.", "success")
                return redirect(url_for("data_tools"))
            if action == "demo":
                existing = sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0]
                               for table in ("entries", "mood_logs", "appointments"))
                profile = db.execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
                has_goals = profile and any(profile[key] is not None for key in (
                    "height_cm", "start_weight", "target_weight", "daily_water_goal_l",
                    "sleep_goal_h", "weekly_exercise_goal_min", "calorie_goal"))
                if existing:
                    flash("Sample data is only loaded into an empty wellness journal. Reset your wellness data first if you want to replace it.", "error")
                    return redirect(url_for("data_tools"))
                today = date.today()
                weights = [72.4,72.2,72.5,72.0,71.9,72.1,71.8,71.7,71.9,71.5,71.4,71.6,71.2,71.0]
                for i, weight in enumerate(weights):
                    day = today - timedelta(days=len(weights)-1-i)
                    db.execute("""INSERT INTO entries(user_id, entry_date, weight_kg, sleep_h, water_l,
                        exercise_type, exercise_min, calories, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (uid, day.isoformat(), weight, [7.0,7.5,6.8,8.0,7.2,7.8,6.5][i%7],
                         [1.8,2.1,2.3,1.7,2.5,2.0,2.2][i%7],
                         "Walk" if i%3 else "Yoga", 25+(i%4)*5 if i%2==0 else 0,
                         1900+(i%5)*75, datetime.utcnow().isoformat()))
                    db.execute("INSERT INTO mood_logs(user_id, log_date, mood, energy, stress, note) VALUES (?, ?, ?, ?, ?, ?)",
                        (uid, day.isoformat(), [3,4,3,5,4,3,4][i%7], [3,4,2,4,5,3,4][i%7],
                         [3,2,4,1,2,3,2][i%7], "Sample reflection — replace with your own note."))
                db.execute("""INSERT INTO appointments(user_id, title, appointment_date, appointment_time,
                    location, notes, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)""",
                    (uid, "Sample wellness follow-up", (today+timedelta(days=5)).isoformat(), "10:30",
                     "Add your own location", "Example appointment; edit or remove it after exploring.", datetime.utcnow().isoformat()))
                if not has_goals:
                    db.execute("""UPDATE profiles SET height_cm=168, start_weight=74, target_weight=69,
                        daily_water_goal_l=2.2, sleep_goal_h=8, weekly_exercise_goal_min=150,
                        calorie_goal=2000 WHERE user_id=?""", (uid,))
                db.commit()
                flash("Sample wellness entries loaded. They are clearly labeled in the notes; add your own data after resetting.", "success")
                return redirect(url_for("home"))
        flash("Choose a valid data action.", "error")
    return render_template("data_tools.html")

# Mood journal: save one daily set of mood, energy, stress ratings, and an optional note.
@app.route("/mood", methods=["GET", "POST"])
@login_required
def mood_journal():
    uid = current_user()["id"]
    if request.method == "POST":
        try:
            log_day = date.fromisoformat(request.form.get("log_date", ""))
            mood = int(request.form.get("mood", ""))
            energy = int(request.form.get("energy", ""))
            stress = int(request.form.get("stress", ""))
        except (ValueError, TypeError):
            flash("Choose a valid date and select all three ratings.", "error")
        else:
            if log_day > date.today() or any(v < 1 or v > 5 for v in (mood, energy, stress)):
                flash("Use a date up to today and ratings from 1 to 5.", "error")
            else:
                note = request.form.get("note", "").strip()[:1000]
                with connect_db() as db:
                    db.execute("""INSERT INTO mood_logs(user_id, log_date, mood, energy, stress, note)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, log_date) DO UPDATE SET mood=excluded.mood,
                        energy=excluded.energy, stress=excluded.stress, note=excluded.note""",
                        (uid, log_day.isoformat(), mood, energy, stress, note))
                    db.commit()
                flash("Your mood check-in is saved.", "success")
                return redirect(url_for("mood_journal"))
    with connect_db() as db:
        rows = db.execute("SELECT * FROM mood_logs WHERE user_id=? ORDER BY log_date DESC LIMIT 30", (uid,)).fetchall()
    history = [dict(row) for row in rows]
    today_log = next((row for row in history if row['log_date'] == date.today().isoformat()), None)
    graph = list(reversed(history))
    return render_template("mood.html", history=history, today_log=today_log, graph={
        "labels": [row["log_date"][5:] for row in graph],
        "mood": [row["mood"] for row in graph],
        "energy": [row["energy"] for row in graph],
        "stress": [row["stress"] for row in graph],
    })


# Care planner: record appointment details and list this user’s appointments.
@app.route("/care", methods=["GET", "POST"])
@login_required
def care_planner():
    uid = current_user()["id"]
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:100]
        location = request.form.get("location", "").strip()[:120]
        notes = request.form.get("notes", "").strip()[:1000]
        try:
            appt_day = date.fromisoformat(request.form.get("appointment_date", ""))
            appt_time = datetime.strptime(request.form.get("appointment_time", ""), "%H:%M").time()
        except ValueError:
            flash("Choose a valid appointment date and time.", "error")
        else:
            if not title:
                flash("Add a title for this appointment.", "error")
            else:
                with connect_db() as db:
                    db.execute("""INSERT INTO appointments(user_id, title, appointment_date,
                        appointment_time, location, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (uid, title, appt_day.isoformat(), appt_time.strftime("%H:%M"), location, notes, datetime.utcnow().isoformat()))
                    db.commit()
                flash("Appointment added to your care planner.", "success")
                return redirect(url_for("care_planner"))
    with connect_db() as db:
        appointments = db.execute("""SELECT * FROM appointments WHERE user_id=?
            ORDER BY CASE status WHEN 'planned' THEN 0 ELSE 1 END, appointment_date, appointment_time""", (uid,)).fetchall()
    return render_template("care.html", appointments=[dict(row) for row in appointments])


# Toggle an appointment between planned and completed for its owner.
@app.post("/care/<int:appointment_id>/toggle")
@login_required
def toggle_appointment(appointment_id):
    with connect_db() as db:
        db.execute("""UPDATE appointments SET status=CASE status WHEN 'planned' THEN 'done' ELSE 'planned' END
            WHERE id=? AND user_id=?""", (appointment_id, current_user()["id"]))
        db.commit()
    flash("Appointment status updated.", "success")
    return redirect(url_for("care_planner"))


# Remove an appointment only when it belongs to the signed-in account.
@app.post("/care/<int:appointment_id>/delete")
@login_required
def delete_appointment(appointment_id):
    with connect_db() as db:
        db.execute("DELETE FROM appointments WHERE id=? AND user_id=?", (appointment_id, current_user()["id"]))
        db.commit()
    flash("Appointment removed.", "success")
    return redirect(url_for("care_planner"))

# Show a friendly page when a requested route or record does not exist.
@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="We couldn't find that page."), 404


# Initialize missing database tables when the application starts.
init_db()

# Start Flask’s local development server when this file is run directly.
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG", "0") == "1")
