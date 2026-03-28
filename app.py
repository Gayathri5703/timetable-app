"""
Automatic Generative Timetable — Single-file Flask App
=======================================================
Run:  pip install flask werkzeug && python app.py
Open: http://127.0.0.1:5000

Default admin credentials: admin / admin123

Pages
-----
  /           → Login
  /workload   → Manage classes, staff, subjects, periods, weekdays
  /generate   → Run timetable generation
  /timetable  → View, edit, save, export
  /logout     → Clear session
"""

import os, random, sqlite3, hashlib, io
from datetime import datetime
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, send_file)

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-timetable-2025")
DB_PATH = os.path.join(os.path.dirname(__file__), "timetable.db")

# FIX (Bug 10): Use server-side filesystem sessions to avoid the 4 KB cookie
# limit that would silently drop tt_result for large timetables.
# Requires: pip install flask-session
try:
    from flask_session import Session
    SESSION_DIR = os.path.join(os.path.dirname(__file__), "flask_sessions")
    os.makedirs(SESSION_DIR, exist_ok=True)
    app.config["SESSION_TYPE"]           = "filesystem"
    app.config["SESSION_FILE_DIR"]       = SESSION_DIR
    app.config["SESSION_PERMANENT"]      = False
    app.config["SESSION_USE_SIGNER"]     = True
    Session(app)
except ImportError:
    # flask-session not installed — fall back to cookie sessions with a warning
    import warnings
    warnings.warn(
        "flask-session is not installed. Large timetables may silently lose "
        "session data due to the 4 KB cookie limit. "
        "Install with: pip install flask-session",
        RuntimeWarning,
        stacklevel=1,
    )

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    sql = """
    CREATE TABLE IF NOT EXISTS users (
        id   INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'admin'
    );
    CREATE TABLE IF NOT EXISTS classes (
        id   INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        section TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS staff (
        id   INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        abbreviation TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS subjects (
        id          INTEGER PRIMARY KEY,
        name        TEXT UNIQUE NOT NULL,
        code        TEXT DEFAULT '',
        is_lab      INTEGER DEFAULT 0,
        staff_id    INTEGER REFERENCES staff(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS weekdays (
        id    INTEGER PRIMARY KEY,
        name  TEXT UNIQUE NOT NULL,
        order_index INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS periods (
        id         INTEGER PRIMARY KEY,
        label      TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time   TEXT NOT NULL,
        order_index INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS timetable_entries (
        id         INTEGER PRIMARY KEY,
        class_id   INTEGER REFERENCES classes(id) ON DELETE CASCADE,
        weekday_id INTEGER REFERENCES weekdays(id) ON DELETE CASCADE,
        period_id  INTEGER REFERENCES periods(id)  ON DELETE CASCADE,
        subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
        staff_id   INTEGER REFERENCES staff(id)    ON DELETE SET NULL,
        is_lab     INTEGER DEFAULT 0,
        generated_at TEXT DEFAULT '',
        UNIQUE(class_id, weekday_id, period_id)
    );
    """
    with get_db() as conn:
        conn.executescript(sql)
        # Default admin
        pw = _hash("admin123")
        conn.execute("INSERT OR IGNORE INTO users(username,password,role) VALUES(?,?,?)",
                     ("admin", pw, "admin"))
        # Default weekdays
        days = [("Monday",1),("Tuesday",2),("Wednesday",3),("Thursday",4),("Friday",5)]
        for d,i in days:
            conn.execute("INSERT OR IGNORE INTO weekdays(name,order_index) VALUES(?,?)", (d,i))
        conn.commit()

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Timetable Generator
# ---------------------------------------------------------------------------

def generate_timetable(class_id: int) -> dict:
    """
    Basic constraint-satisfying timetable generator.
    Constraints:
      - No staff clashes across classes in the same slot
      - Lab subjects occupy 3 consecutive periods on the same day
      - Every period must be filled (no free periods)
    Returns dict: { (weekday_id, period_id): {subject_id, staff_id, is_lab} }
    or raises ValueError with a human-readable message.
    """
    # FIX (Bug 8): the original code fetched all_classes here but never used it
    with get_db() as conn:
        days    = conn.execute("SELECT * FROM weekdays ORDER BY order_index").fetchall()
        periods = conn.execute("SELECT * FROM periods ORDER BY order_index").fetchall()
        subjects = conn.execute(
            "SELECT s.*, st.id AS sid FROM subjects s LEFT JOIN staff st ON s.staff_id=st.id"
        ).fetchall()
        # Pre-collect already-committed entries for OTHER classes (staff clash detection)
        other_entries = conn.execute(
            "SELECT weekday_id, period_id, staff_id FROM timetable_entries WHERE class_id != ?",
            (class_id,)
        ).fetchall()

    if not subjects:
        raise ValueError("No subjects defined. Please add subjects first.")
    if not periods:
        raise ValueError("No periods defined. Please add periods first.")

    # Build sets of (weekday_id, period_id) already occupied by each staff in other classes
    staff_busy: dict = {}  # staff_id -> set of (weekday_id, period_id)
    for e in other_entries:
        if e["staff_id"]:
            staff_busy.setdefault(e["staff_id"], set()).add((e["weekday_id"], e["period_id"]))

    labs    = [s for s in subjects if s["is_lab"]]
    nonlabs = [s for s in subjects if not s["is_lab"]]

    pid_list  = [p["id"] for p in periods]
    did_list  = [d["id"] for d in days]
    n_periods = len(pid_list)

    result = {}  # (day_id, period_id) -> assignment

    MAX_ATTEMPTS = 200
    for _ in range(MAX_ATTEMPTS):
        result.clear()
        # FIX (Bug 7): original had dead variables here that were never read
        merged_staff_busy = {k: set(v) for k, v in staff_busy.items()}

        for day_id in did_list:
            # available_set tracks which period IDs are still free this day
            available_set = set(pid_list)

            # ── 1. Place labs first (need 3 consecutive periods) ────────────
            day_labs = list(labs)
            random.shuffle(day_labs)

            for lab in day_labs[:1]:  # at most one lab block per day
                if len(available_set) < 3:
                    break

                # FIX (Bug 5 & 6): iterate over pid_list positions directly
                # (never shuffle pid_list itself) so consecutive indices are
                # always truly adjacent periods.  The original dead-code list
                # comprehension (computing candidate starts but never using them)
                # is removed entirely.
                starts = list(range(n_periods - 2))
                random.shuffle(starts)          # randomise which triple we try first
                for orig0 in starts:
                    s0 = pid_list[orig0]
                    s1 = pid_list[orig0 + 1]
                    s2 = pid_list[orig0 + 2]
                    # All three slots must still be free this day
                    if not {s0, s1, s2}.issubset(available_set):
                        continue
                    # Check staff availability
                    staff_id = lab["sid"]
                    if staff_id and any(
                        (day_id, p) in merged_staff_busy.get(staff_id, set())
                        for p in (s0, s1, s2)
                    ):
                        continue
                    # Commit the lab block
                    for p in (s0, s1, s2):
                        result[(day_id, p)] = {
                            "subject_id": lab["id"],
                            "staff_id":   staff_id,
                            "is_lab":     1,
                        }
                        available_set.discard(p)
                        if staff_id:
                            merged_staff_busy.setdefault(staff_id, set()).add((day_id, p))
                    break

            # ── 2. Fill remaining slots with non-lab subjects ────────────────
            remaining = list(available_set)
            random.shuffle(remaining)
            subj_pool = list(nonlabs) if nonlabs else list(labs)
            random.shuffle(subj_pool)

            pool_idx = 0
            for p_id in remaining:
                placed = False
                for _ in range(len(subj_pool)):
                    subj     = subj_pool[pool_idx % len(subj_pool)]
                    pool_idx += 1
                    staff_id = subj["sid"]
                    if staff_id and (day_id, p_id) in merged_staff_busy.get(staff_id, set()):
                        continue
                    result[(day_id, p_id)] = {
                        "subject_id": subj["id"],
                        "staff_id":   staff_id,
                        "is_lab":     0,
                    }
                    if staff_id:
                        merged_staff_busy.setdefault(staff_id, set()).add((day_id, p_id))
                    placed = True
                    break

                if not placed:
                    # Fallback: assign any subject, ignoring staff clash this slot
                    subj = random.choice(subj_pool)
                    result[(day_id, p_id)] = {
                        "subject_id": subj["id"],
                        "staff_id":   subj["sid"],
                        "is_lab":     0,
                    }

        # Verify every slot is filled
        if len(result) == len(did_list) * n_periods:
            return result

    raise ValueError(
        "Could not generate a valid timetable after many attempts. "
        "Please check that staff are not over-assigned across classes."
    )

# ---------------------------------------------------------------------------
# CSS / JS (shared across all pages)
# ---------------------------------------------------------------------------

COMMON_STYLE = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:      #0d0f14;
    --surface: #151820;
    --card:    #1c2030;
    --border:  #2a3050;
    --accent:  #4f8ef7;
    --accent2: #f7a24f;
    --green:   #4fd9a4;
    --red:     #f76f6f;
    --text:    #e8eaf2;
    --muted:   #7a82a0;
    --radius:  10px;
    --font-head: 'Syne', sans-serif;
    --font-mono: 'Space Mono', monospace;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.6;
    min-height: 100vh;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* Nav */
  nav {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 2rem;
    display: flex;
    align-items: center;
    gap: 2rem;
    height: 56px;
  }
  nav .brand {
    font-family: var(--font-head);
    font-weight: 800;
    font-size: 1.1rem;
    color: var(--accent);
    letter-spacing: 0.05em;
    margin-right: auto;
  }
  nav a {
    color: var(--muted);
    font-size: 12px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 4px 0;
    border-bottom: 2px solid transparent;
    transition: color .2s, border-color .2s;
  }
  nav a:hover, nav a.active { color: var(--text); border-color: var(--accent); text-decoration: none; }

  /* Layout */
  .page { max-width: 1100px; margin: 0 auto; padding: 2rem 1.5rem; }
  h1 { font-family: var(--font-head); font-size: 1.7rem; font-weight: 800; margin-bottom: .25rem; }
  h2 { font-family: var(--font-head); font-size: 1.15rem; font-weight: 700; margin-bottom: 1rem; color: var(--accent2); }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 2rem; }

  /* Cards */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.5rem;
    margin-bottom: 1.5rem;
  }

  /* Forms */
  label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 4px; }
  input[type=text], input[type=password], input[type=time], select {
    width: 100%; padding: 9px 12px;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-family: var(--font-mono); font-size: 13px;
    transition: border-color .2s;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .form-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .form-row .field { flex: 1; min-width: 140px; }
  .check-row { display: flex; align-items: center; gap: .5rem; margin-bottom: 1rem; }
  .check-row input[type=checkbox] { width: 16px; height: 16px; accent-color: var(--accent); }
  .check-row label { margin: 0; font-size: 12px; text-transform: none; }

  /* Buttons */
  .btn {
    display: inline-flex; align-items: center; gap: .4rem;
    padding: 9px 18px; border-radius: 6px; border: none; cursor: pointer;
    font-family: var(--font-mono); font-size: 12px; font-weight: 700;
    letter-spacing: .08em; text-transform: uppercase;
    transition: opacity .2s, transform .1s;
  }
  .btn:active { transform: scale(.97); }
  .btn-primary   { background: var(--accent);  color: #fff; }
  .btn-secondary { background: var(--border);  color: var(--text); }
  .btn-success   { background: var(--green);   color: #0d0f14; }
  .btn-danger    { background: var(--red);     color: #fff; }
  .btn-warning   { background: var(--accent2); color: #0d0f14; }
  .btn:hover { opacity: .85; }
  .btn-row { display: flex; gap: .75rem; flex-wrap: wrap; margin-top: 1rem; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th {
    background: var(--surface); color: var(--muted);
    font-size: 10px; letter-spacing: .12em; text-transform: uppercase;
    padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border);
  }
  td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(79,142,247,.05); }

  /* Tags / badges */
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 10px; letter-spacing: .08em; text-transform: uppercase; font-weight: 700;
  }
  .badge-lab   { background: rgba(247,162,79,.15); color: var(--accent2); }
  .badge-admin { background: rgba(79,142,247,.15); color: var(--accent); }

  /* Alerts */
  .alert {
    padding: 12px 16px; border-radius: 6px; margin-bottom: 1.25rem; font-size: 12px;
    border-left: 3px solid;
  }
  .alert-error   { background: rgba(247,111,111,.1); border-color: var(--red);   color: var(--red); }
  .alert-success { background: rgba(79,217,164,.1);  border-color: var(--green); color: var(--green); }
  .alert-info    { background: rgba(79,142,247,.1);  border-color: var(--accent);color: var(--accent); }

  /* Timetable grid */
  .tt-wrap { overflow-x: auto; }
  .tt-table th { min-width: 110px; text-align: center; }
  .tt-table td { text-align: center; min-width: 110px; }
  .tt-cell { font-size: 11px; line-height: 1.4; }
  .tt-cell .subj { font-weight: 700; color: var(--text); }
  .tt-cell .stf  { color: var(--muted); }
  .tt-cell.lab   { background: rgba(247,162,79,.08); }
  .tt-cell.editing select { width: 100%; margin-top: 4px; }

  /* Login page */
  .login-wrap {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(ellipse at 60% 40%, rgba(79,142,247,.12) 0%, transparent 65%),
                var(--bg);
  }
  .login-box {
    width: 360px; background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 2.5rem 2rem; box-shadow: 0 24px 60px rgba(0,0,0,.5);
  }
  .login-box h1 { font-size: 1.4rem; margin-bottom: .25rem; }
  .login-box .sub { margin-bottom: 1.5rem; }
  .login-box .field { margin-bottom: 1rem; }
  .login-box .btn { width: 100%; justify-content: center; padding: 11px; margin-top: .5rem; }

  /* Spinner */
  .spinner {
    display:inline-block; width:16px; height:16px; border:2px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Section tabs */
  .tabs { display: flex; gap: .5rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  .tab-btn {
    padding: 7px 16px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--muted); font-family: var(--font-mono);
    font-size: 11px; letter-spacing: .08em; text-transform: uppercase; cursor: pointer;
    transition: all .2s;
  }
  .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }

  /* Responsive */
  @media (max-width: 640px) {
    nav { padding: 0 1rem; gap: 1rem; }
    .page { padding: 1rem; }
    .form-row { flex-direction: column; }
  }
</style>
"""

NAV = """
<nav>
  <span class="brand">&#9632; AutoTT</span>
  <a href="/workload" class="{w}">Workload</a>
  <a href="/timetable" class="{t}">Timetable</a>
  <a href="/logout">Logout</a>
</nav>
"""

def nav(active=""):
    return NAV.format(
        w="active" if active == "workload" else "",
        t="active" if active == "timetable" else ""
    )

# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — AutoTT</title>""" + COMMON_STYLE + """
</head><body>
<div class="login-wrap">
  <div class="login-box">
    <h1>AutoTT</h1>
    <p class="sub">Automatic Generative Timetable</p>
    {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
    {% if msg   %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
    <form method="post">
    <div class="field"><label>Username</label>
      <input type="text" name="username" autocomplete="username" required>
    </div>
    <div class="field"><label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required>
    </div>
    <button class="btn btn-primary" type="submit">Sign in</button>
    </form>
    <p style="margin-top:1.5rem;color:var(--muted);font-size:11px;text-align:center;">
      Default: admin / admin123
    </p>
  </div>
</div>
</body></html>"""

@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("workload"))
    error = msg = ""
    if request.method == "POST":
        uname = request.form.get("username","").strip()
        pw    = _hash(request.form.get("password",""))
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username=? AND password=?", (uname, pw)
            ).fetchone()
        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"]     = user["role"]
            return redirect(url_for("workload"))
        error = "Invalid credentials."
    return render_template_string(LOGIN_HTML, error=error, msg=msg)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Workload Page
# ---------------------------------------------------------------------------

WORKLOAD_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workload — AutoTT</title>""" + COMMON_STYLE + """
</head><body>
{{ nav|safe }}
<div class="page">
  <h1>Workload Manager</h1>
  <p class="sub">Define classes, staff, subjects, periods and weekdays</p>

  {% if flash_msg %}
  <div class="alert alert-{{ flash_type }}">{{ flash_msg }}</div>
  {% endif %}

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('classes',this)">Classes</button>
    <button class="tab-btn" onclick="switchTab('staff',this)">Staff</button>
    <button class="tab-btn" onclick="switchTab('subjects',this)">Subjects</button>
    <button class="tab-btn" onclick="switchTab('periods',this)">Periods</button>
    <button class="tab-btn" onclick="switchTab('weekdays',this)">Weekdays</button>
  </div>

  <!-- CLASSES -->
  <div class="tab-pane active" id="tab-classes">
    <div class="card">
      <h2>Add Class</h2>
      <form method="post" action="/api/classes">
        <div class="form-row">
          <div class="field"><label>Class Name</label>
            <input type="text" name="name" placeholder="e.g. XII A" required></div>
          <div class="field"><label>Section / Label</label>
            <input type="text" name="section" placeholder="optional"></div>
        </div>
        <button class="btn btn-primary" type="submit">Add Class</button>
      </form>
    </div>
    <div class="card">
      <h2>Classes</h2>
      <table>
        <thead><tr><th>#</th><th>Name</th><th>Section</th><th>Action</th></tr></thead>
        <tbody>
          {% for c in classes %}
          <tr>
            <td>{{ loop.index }}</td><td>{{ c.name }}</td><td>{{ c.section }}</td>
            <td><form method="post" action="/api/classes/{{ c.id }}/delete" style="display:inline">
              <button class="btn btn-danger" type="submit" style="padding:4px 10px">Delete</button>
            </form></td>
          </tr>
          {% else %}<tr><td colspan="4" style="color:var(--muted)">No classes yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- STAFF -->
  <div class="tab-pane" id="tab-staff">
    <div class="card">
      <h2>Add Staff</h2>
      <form method="post" action="/api/staff">
        <div class="form-row">
          <div class="field"><label>Full Name</label>
            <input type="text" name="name" placeholder="e.g. Dr. Smith" required></div>
          <div class="field"><label>Abbreviation</label>
            <input type="text" name="abbreviation" placeholder="e.g. SMS"></div>
        </div>
        <button class="btn btn-primary" type="submit">Add Staff</button>
      </form>
    </div>
    <div class="card">
      <h2>Staff List</h2>
      <table>
        <thead><tr><th>#</th><th>Name</th><th>Abbr.</th><th>Action</th></tr></thead>
        <tbody>
          {% for s in staff %}
          <tr>
            <td>{{ loop.index }}</td><td>{{ s.name }}</td><td>{{ s.abbreviation }}</td>
            <td><form method="post" action="/api/staff/{{ s.id }}/delete" style="display:inline">
              <button class="btn btn-danger" type="submit" style="padding:4px 10px">Delete</button>
            </form></td>
          </tr>
          {% else %}<tr><td colspan="4" style="color:var(--muted)">No staff yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- SUBJECTS -->
  <div class="tab-pane" id="tab-subjects">
    <div class="card">
      <h2>Add Subject</h2>
      <form method="post" action="/api/subjects">
        <div class="form-row">
          <div class="field"><label>Subject Name</label>
            <input type="text" name="name" placeholder="e.g. Physics" required></div>
          <div class="field"><label>Code</label>
            <input type="text" name="code" placeholder="e.g. PHY"></div>
          <div class="field"><label>Assigned Staff</label>
            <select name="staff_id">
              <option value="">— none —</option>
              {% for s in staff %}
              <option value="{{ s.id }}">{{ s.name }}</option>
              {% endfor %}
            </select>
          </div>
        </div>
        <div class="check-row">
          <input type="checkbox" name="is_lab" id="is_lab" value="1">
          <label for="is_lab">Lab subject (will occupy 3 consecutive periods)</label>
        </div>
        <button class="btn btn-primary" type="submit">Add Subject</button>
      </form>
    </div>
    <div class="card">
      <h2>Subjects</h2>
      <table>
        <thead><tr><th>#</th><th>Name</th><th>Code</th><th>Staff</th><th>Lab?</th><th>Action</th></tr></thead>
        <tbody>
          {% for s in subjects %}
          <tr>
            <td>{{ loop.index }}</td>
            <td>{{ s.name }}</td>
            <td>{{ s.code }}</td>
            <td>{{ s.staff_name or '—' }}</td>
            <td>{% if s.is_lab %}<span class="badge badge-lab">Lab</span>{% else %}—{% endif %}</td>
            <td><form method="post" action="/api/subjects/{{ s.id }}/delete" style="display:inline">
              <button class="btn btn-danger" type="submit" style="padding:4px 10px">Delete</button>
            </form></td>
          </tr>
          {% else %}<tr><td colspan="6" style="color:var(--muted)">No subjects yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- PERIODS -->
  <div class="tab-pane" id="tab-periods">
    <div class="card">
      <h2>Add Period</h2>
      <form method="post" action="/api/periods">
        <div class="form-row">
          <div class="field"><label>Label</label>
            <input type="text" name="label" placeholder="e.g. 1st Period" required></div>
          <div class="field"><label>Start Time</label>
            <input type="time" name="start_time" required></div>
          <div class="field"><label>End Time</label>
            <input type="time" name="end_time" required></div>
          <div class="field"><label>Order</label>
            <input type="text" name="order_index" placeholder="1"></div>
        </div>
        <button class="btn btn-primary" type="submit">Add Period</button>
      </form>
    </div>
    <div class="card">
      <h2>Periods</h2>
      <table>
        <thead><tr><th>Order</th><th>Label</th><th>Start</th><th>End</th><th>Action</th></tr></thead>
        <tbody>
          {% for p in periods %}
          <tr>
            <td>{{ p.order_index }}</td><td>{{ p.label }}</td>
            <td>{{ p.start_time }}</td><td>{{ p.end_time }}</td>
            <td><form method="post" action="/api/periods/{{ p.id }}/delete" style="display:inline">
              <button class="btn btn-danger" type="submit" style="padding:4px 10px">Delete</button>
            </form></td>
          </tr>
          {% else %}<tr><td colspan="5" style="color:var(--muted)">No periods yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- WEEKDAYS -->
  <div class="tab-pane" id="tab-weekdays">
    <div class="card">
      <h2>Add Weekday</h2>
      <form method="post" action="/api/weekdays">
        <div class="form-row">
          <div class="field"><label>Day Name</label>
            <input type="text" name="name" placeholder="e.g. Saturday" required></div>
          <div class="field"><label>Order</label>
            <input type="text" name="order_index" placeholder="6"></div>
        </div>
        <button class="btn btn-primary" type="submit">Add Day</button>
      </form>
    </div>
    <div class="card">
      <h2>Weekdays</h2>
      <table>
        <thead><tr><th>Order</th><th>Day</th><th>Action</th></tr></thead>
        <tbody>
          {% for d in weekdays %}
          <tr>
            <td>{{ d.order_index }}</td><td>{{ d.name }}</td>
            <td><form method="post" action="/api/weekdays/{{ d.id }}/delete" style="display:inline">
              <button class="btn btn-danger" type="submit" style="padding:4px 10px">Delete</button>
            </form></td>
          </tr>
          {% else %}<tr><td colspan="3" style="color:var(--muted)">No days yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Generate -->
  <div class="card" style="margin-top:2rem;border-color:var(--accent)">
    <h2 style="color:var(--green)">&#9654; Generate Timetable</h2>
    <p style="color:var(--muted);margin-bottom:1rem;font-size:12px;">
      Select a class and run the constraint-based generator.
      Results will be saved in session and visible on the Timetable page.
    </p>
    <form method="post" action="/generate" id="gen-form">
      <div class="form-row" style="align-items:flex-end">
        <div class="field">
          <label>Class</label>
          <select name="class_id" required>
            <option value="">— select class —</option>
            {% for c in classes %}
            <option value="{{ c.id }}">{{ c.name }} {{ c.section }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <button class="btn btn-success" type="submit" id="gen-btn">Generate</button>
        </div>
      </div>
    </form>
  </div>
</div>
<script>
function switchTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  btn.classList.add('active');
}
document.getElementById('gen-form').addEventListener('submit', function(){
  document.getElementById('gen-btn').innerHTML = '<span class="spinner"></span> Generating...';
  document.getElementById('gen-btn').disabled = true;
});
</script>
</body></html>"""

@app.route("/workload")
@login_required
def workload():
    fm = session.pop("flash_msg", "")
    ft = session.pop("flash_type", "info")
    with get_db() as conn:
        classes  = conn.execute("SELECT * FROM classes ORDER BY name").fetchall()
        staff    = conn.execute("SELECT * FROM staff ORDER BY name").fetchall()
        subjects = conn.execute(
            "SELECT s.*, st.name as staff_name FROM subjects s "
            "LEFT JOIN staff st ON s.staff_id=st.id ORDER BY s.name"
        ).fetchall()
        periods  = conn.execute("SELECT * FROM periods ORDER BY order_index").fetchall()
        weekdays = conn.execute("SELECT * FROM weekdays ORDER BY order_index").fetchall()

    return render_template_string(WORKLOAD_HTML,
        nav=nav("workload"), flash_msg=fm, flash_type=ft,
        classes=classes, staff=staff, subjects=subjects,
        periods=periods, weekdays=weekdays)

# ---------------------------------------------------------------------------
# CRUD API routes (form POSTs → redirect back to workload)
# ---------------------------------------------------------------------------

def wl_redirect(msg, typ="success"):
    session["flash_msg"]  = msg
    session["flash_type"] = typ
    return redirect(url_for("workload"))

# Classes
@app.route("/api/classes", methods=["POST"])
@login_required
def api_add_class():
    name    = request.form.get("name","").strip()
    section = request.form.get("section","").strip()
    if not name:
        return wl_redirect("Class name is required.", "error")
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO classes(name,section) VALUES(?,?)", (name, section))
            conn.commit()
    except sqlite3.IntegrityError:
        return wl_redirect(f"Class '{name}' already exists.", "error")
    return wl_redirect(f"Class '{name}' added.")

@app.route("/api/classes/<int:cid>/delete", methods=["POST"])
@login_required
def api_del_class(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM classes WHERE id=?", (cid,))
        conn.commit()
    return wl_redirect("Class deleted.")

# Staff
@app.route("/api/staff", methods=["POST"])
@login_required
def api_add_staff():
    name = request.form.get("name","").strip()
    abbr = request.form.get("abbreviation","").strip()
    if not name:
        return wl_redirect("Staff name is required.", "error")
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO staff(name,abbreviation) VALUES(?,?)", (name, abbr))
            conn.commit()
    except sqlite3.IntegrityError:
        return wl_redirect(f"Staff '{name}' already exists.", "error")
    return wl_redirect(f"Staff '{name}' added.")

@app.route("/api/staff/<int:sid>/delete", methods=["POST"])
@login_required
def api_del_staff(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM staff WHERE id=?", (sid,))
        conn.commit()
    return wl_redirect("Staff deleted.")

# Subjects
@app.route("/api/subjects", methods=["POST"])
@login_required
def api_add_subject():
    name     = request.form.get("name","").strip()
    code     = request.form.get("code","").strip()
    staff_id = request.form.get("staff_id") or None
    is_lab   = 1 if request.form.get("is_lab") else 0
    if not name:
        return wl_redirect("Subject name is required.", "error")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO subjects(name,code,is_lab,staff_id) VALUES(?,?,?,?)",
                (name, code, is_lab, staff_id)
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return wl_redirect(f"Subject '{name}' already exists.", "error")
    return wl_redirect(f"Subject '{name}' added.")

@app.route("/api/subjects/<int:sid>/delete", methods=["POST"])
@login_required
def api_del_subject(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM subjects WHERE id=?", (sid,))
        conn.commit()
    return wl_redirect("Subject deleted.")

# Periods
@app.route("/api/periods", methods=["POST"])
@login_required
def api_add_period():
    label  = request.form.get("label","").strip()
    start  = request.form.get("start_time","").strip()
    end    = request.form.get("end_time","").strip()
    order  = request.form.get("order_index","0").strip() or "0"
    if not (label and start and end):
        return wl_redirect("Label, start and end time are required.", "error")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO periods(label,start_time,end_time,order_index) VALUES(?,?,?,?)",
            (label, start, end, int(order))
        )
        conn.commit()
    return wl_redirect(f"Period '{label}' added.")

@app.route("/api/periods/<int:pid>/delete", methods=["POST"])
@login_required
def api_del_period(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM periods WHERE id=?", (pid,))
        conn.commit()
    return wl_redirect("Period deleted.")

# Weekdays
@app.route("/api/weekdays", methods=["POST"])
@login_required
def api_add_weekday():
    name  = request.form.get("name","").strip()
    order = request.form.get("order_index","0").strip() or "0"
    if not name:
        return wl_redirect("Day name is required.", "error")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO weekdays(name,order_index) VALUES(?,?)", (name, int(order))
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return wl_redirect(f"Day '{name}' already exists.", "error")
    return wl_redirect(f"Day '{name}' added.")

@app.route("/api/weekdays/<int:did>/delete", methods=["POST"])
@login_required
def api_del_weekday(did):
    with get_db() as conn:
        conn.execute("DELETE FROM weekdays WHERE id=?", (did,))
        conn.commit()
    return wl_redirect("Day deleted.")

# ---------------------------------------------------------------------------
# Generate timetable
# ---------------------------------------------------------------------------

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    class_id = request.form.get("class_id")
    if not class_id:
        session["flash_msg"]  = "Please select a class."
        session["flash_type"] = "error"
        return redirect(url_for("workload"))
    try:
        result = generate_timetable(int(class_id))
        # Store in session (serialise keys as strings)
        session["tt_class_id"] = int(class_id)
        session["tt_result"]   = {f"{k[0]},{k[1]}": v for k, v in result.items()}
        return redirect(url_for("timetable_view"))
    except ValueError as e:
        session["flash_msg"]  = str(e)
        session["flash_type"] = "error"
        return redirect(url_for("workload"))

# ---------------------------------------------------------------------------
# Timetable View
# ---------------------------------------------------------------------------

TIMETABLE_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Timetable — AutoTT</title>""" + COMMON_STYLE + """
<style>
  @media print {
    nav, .no-print { display: none !important; }
    body { background: #fff; color: #000; }
    .card { border-color: #ccc; background: #fff; }
    .tt-table th { background: #eee; }
  }
</style>
</head><body>
{{ nav|safe }}
<div class="page">
  <h1>Timetable</h1>
  <p class="sub">View, edit and export the generated timetable</p>

  {% if msg %}<div class="alert alert-{{ msg_type }}">{{ msg }}</div>{% endif %}

  {% if not tt %}
  <div class="card">
    <p style="color:var(--muted)">No timetable generated yet. Go to
      <a href="/workload">Workload</a> and click <strong>Generate Timetable</strong>.</p>
  </div>
  {% else %}
  <!-- Class selector for saved timetables -->
  <div class="card no-print" style="padding:1rem">
    <div class="form-row" style="align-items:flex-end;margin:0">
      <div class="field">
        <label>Load saved timetable for class</label>
        <select id="load-class" onchange="loadSaved()">
          <option value="">— select —</option>
          {% for c in classes %}
          <option value="{{ c.id }}" {% if c.id == class_id %}selected{% endif %}>
            {{ c.name }} {{ c.section }}
          </option>
          {% endfor %}
        </select>
      </div>
      <div>
        <button class="btn btn-secondary" onclick="loadSaved()">Load</button>
      </div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;flex-wrap:wrap;gap:.5rem">
      <h2 style="margin:0">{{ class_name }} — Timetable</h2>
      <div class="btn-row no-print" style="margin:0">
        <button class="btn btn-success"  onclick="saveTT()">&#10003; Save</button>
        <button class="btn btn-warning"  onclick="exportPDF()">&#8659; Export PDF</button>
        <button class="btn btn-secondary" onclick="window.print()">&#9113; Print</button>
        <button class="btn btn-primary"  id="edit-btn" onclick="toggleEdit()">&#9998; Edit</button>
      </div>
    </div>

    <div class="tt-wrap">
      <table class="tt-table" id="tt-table">
        <thead>
          <tr>
            <th>Day / Period</th>
            {% for p in periods %}
            <th>{{ p.label }}<br><span style="color:var(--muted);font-size:10px">{{ p.start_time }}–{{ p.end_time }}</span></th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          {% for day in days %}
          <tr>
            <td><strong>{{ day.name }}</strong></td>
            {% for period in periods %}
            {% set key = day.id|string + ',' + period.id|string %}
            {% set cell = tt.get(key, {}) %}
            <td class="tt-cell {% if cell.get('is_lab') %}lab{% endif %}"
                data-day="{{ day.id }}" data-period="{{ period.id }}"
                data-subject="{{ cell.get('subject_id','') }}"
                data-staff="{{ cell.get('staff_id','') }}"
                data-lab="{{ cell.get('is_lab', 0) }}">
              <div class="cell-view">
                <div class="subj">{{ cell.get('subject_name', '—') }}</div>
                <div class="stf">{{ cell.get('staff_name', '') }}</div>
                {% if cell.get('is_lab') %}<span class="badge badge-lab">Lab</span>{% endif %}
              </div>
              <div class="cell-edit" style="display:none">
                <select onchange="cellChanged(this)">
                  <option value="">— none —</option>
                  {% for s in subjects %}
                  <option value="{{ s.id }}" {% if s.id == cell.get('subject_id') %}selected{% endif %}>
                    {{ s.name }}{% if s.is_lab %} (Lab){% endif %}
                  </option>
                  {% endfor %}
                </select>
              </div>
            </td>
            {% endfor %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}
</div>

<script>
let editMode = false;
const classId = {{ class_id or 'null' }};

function toggleEdit() {
  editMode = !editMode;
  document.querySelectorAll('.cell-view').forEach(el => el.style.display = editMode ? 'none' : 'block');
  document.querySelectorAll('.cell-edit').forEach(el => el.style.display = editMode ? 'block' : 'none');
  document.getElementById('edit-btn').textContent = editMode ? '✕ Done' : '✎ Edit';
}

function cellChanged(sel) {
  const td = sel.closest('td');
  const subjId = sel.value;
  td.dataset.subject = subjId;
  // Update view label optimistically
  const opt = sel.options[sel.selectedIndex];
  td.querySelector('.subj').textContent = opt ? opt.text.replace(' (Lab)', '') : '—';
}

function collectTT() {
  const data = [];
  document.querySelectorAll('#tt-table td[data-day]').forEach(td => {
    data.push({
      day_id:     parseInt(td.dataset.day),
      period_id:  parseInt(td.dataset.period),
      subject_id: td.dataset.subject || null,
      staff_id:   td.dataset.staff   || null,
      is_lab:     parseInt(td.dataset.lab || 0)
    });
  });
  return data;
}

async function saveTT() {
  if (!classId) return alert('No class loaded.');
  const entries = collectTT();
  const res = await fetch('/api/timetable/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({class_id: classId, entries})
  });
  const j = await res.json();
  showAlert(j.ok ? 'Timetable saved successfully!' : ('Error: ' + j.error), j.ok ? 'success' : 'error');
}

async function exportPDF() {
  if (!classId) return alert('No class loaded.');
  window.location.href = '/api/timetable/export?class_id=' + classId;
}

function loadSaved() {
  const cid = document.getElementById('load-class').value;
  if (cid) window.location.href = '/timetable?class_id=' + cid;
}

function showAlert(msg, type) {
  let el = document.getElementById('dyn-alert');
  if (!el) {
    el = document.createElement('div'); el.id = 'dyn-alert';
    document.querySelector('.page').prepend(el);
  }
  el.className = 'alert alert-' + type;
  el.textContent = msg;
  setTimeout(() => el.remove(), 4000);
}
</script>
</body></html>"""

@app.route("/timetable")
@login_required
def timetable_view():
    msg = session.pop("tt_msg", "")
    msg_type = session.pop("tt_msg_type", "info")

    class_id = request.args.get("class_id", type=int)
    class_name = ""
    tt = {}

    with get_db() as conn:
        classes  = conn.execute("SELECT * FROM classes ORDER BY name").fetchall()
        days     = conn.execute("SELECT * FROM weekdays ORDER BY order_index").fetchall()
        periods  = conn.execute("SELECT * FROM periods  ORDER BY order_index").fetchall()
        subjects = conn.execute("SELECT * FROM subjects ORDER BY name").fetchall()

        subj_map  = {s["id"]: s for s in subjects}

        if class_id:
            # Load from DB — FIX (Bug 3): join staff in one query; no nested get_db()
            cl = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
            if cl:
                class_name = f"{cl['name']} {cl['section']}".strip()
            entries = conn.execute(
                "SELECT te.*, st.name as staff_name_db "
                "FROM timetable_entries te "
                "LEFT JOIN staff st ON te.staff_id = st.id "
                "WHERE te.class_id=?", (class_id,)
            ).fetchall()
            for e in entries:
                key = f"{e['weekday_id']},{e['period_id']}"
                subj = subj_map.get(e["subject_id"])
                tt[key] = {
                    "subject_id":   e["subject_id"],
                    "subject_name": subj["name"] if subj else "—",
                    "staff_id":     e["staff_id"],
                    "staff_name":   e["staff_name_db"] or "",
                    "is_lab":       e["is_lab"],
                }

        elif "tt_result" in session:
            # Load from session (freshly generated)
            class_id = session.get("tt_class_id")
            cl = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone() if class_id else None
            class_name = (f"{cl['name']} {cl['section']}".strip()) if cl else ""
            raw = session.get("tt_result", {})
            staff_map = {s["id"]: s for s in conn.execute("SELECT * FROM staff").fetchall()}
            for k, v in raw.items():
                subj   = subj_map.get(v.get("subject_id"))
                st_obj = staff_map.get(v.get("staff_id"))
                tt[k]  = {
                    "subject_id":   v.get("subject_id"),
                    "subject_name": subj["name"] if subj else "—",
                    "staff_id":     v.get("staff_id"),
                    "staff_name":   st_obj["name"] if st_obj else "",
                    "is_lab":       v.get("is_lab", 0),
                }

    return render_template_string(TIMETABLE_HTML,
        nav=nav("timetable"), msg=msg, msg_type=msg_type,
        tt=tt, class_id=class_id, class_name=class_name,
        classes=classes, days=days, periods=periods, subjects=subjects)

# ---------------------------------------------------------------------------
# API: Save timetable
# ---------------------------------------------------------------------------

@app.route("/api/timetable/save", methods=["POST"])
@login_required
def api_save_timetable():
    data = request.get_json(force=True)
    class_id = data.get("class_id")
    entries  = data.get("entries", [])
    if not class_id:
        return jsonify({"ok": False, "error": "class_id required"})
    ts = datetime.utcnow().isoformat()
    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM timetable_entries WHERE class_id=?", (class_id,)
            )
            for e in entries:
                if not e.get("subject_id"):
                    continue
                conn.execute(
                    "INSERT INTO timetable_entries"
                    "(class_id,weekday_id,period_id,subject_id,staff_id,is_lab,generated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (class_id, e["day_id"], e["period_id"],
                     e.get("subject_id"), e.get("staff_id"), e.get("is_lab",0), ts)
                )
            conn.commit()
        # Clear session cache
        session.pop("tt_result", None)
        session.pop("tt_class_id", None)
        return jsonify({"ok": True})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})

# ---------------------------------------------------------------------------
# API: Export PDF (HTML-based download)
# ---------------------------------------------------------------------------

EXPORT_HTML = """<!DOCTYPE html><html>
<head><meta charset="UTF-8">
<title>Timetable Export</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 12px; padding: 20px; }}
  h2   {{ margin-bottom: 4px; }}
  p    {{ color: #666; font-size: 11px; margin-bottom: 16px; }}
  table{{ border-collapse: collapse; width: 100%; }}
  th,td{{ border: 1px solid #ccc; padding: 6px 10px; text-align: center; }}
  th   {{ background: #f0f0f0; }}
  .lab {{ background: #fff8e8; }}
  @media print {{ button {{ display:none; }} }}
</style>
</head>
<body>
<h2>Timetable: {class_name}</h2>
<p>Generated: {ts} &nbsp;|&nbsp; AutoTT</p>
<button onclick="window.print()" style="margin-bottom:12px;padding:6px 14px;cursor:pointer">
  Print / Save as PDF
</button>
<table>{table}</table>
</body></html>"""

@app.route("/api/timetable/export")
@login_required
def api_export_timetable():
    class_id = request.args.get("class_id", type=int)
    if not class_id:
        return "class_id required", 400
    with get_db() as conn:
        cl       = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
        days     = conn.execute("SELECT * FROM weekdays ORDER BY order_index").fetchall()
        periods  = conn.execute("SELECT * FROM periods  ORDER BY order_index").fetchall()
        entries  = conn.execute(
            "SELECT te.*, s.name as sname, st.name as stname "
            "FROM timetable_entries te "
            "LEFT JOIN subjects s ON te.subject_id=s.id "
            "LEFT JOIN staff st   ON te.staff_id=st.id "
            "WHERE te.class_id=?", (class_id,)
        ).fetchall()

    class_name = f"{cl['name']} {cl['section']}".strip() if cl else str(class_id)

    # FIX (Bug 9): warn if timetable has not been saved yet
    if not entries:
        return (
            "<h3 style='font-family:sans-serif;padding:2rem'>"
            "&#9888; No saved timetable found for this class.<br><br>"
            "Please click <strong>Save</strong> on the Timetable page before exporting.</h3>",
            404,
        )
    cell_map = {}
    for e in entries:
        cell_map[(e["weekday_id"], e["period_id"])] = e

    # Build HTML table
    header = "<tr><th>Day</th>" + "".join(
        f"<th>{p['label']}<br>{p['start_time']}–{p['end_time']}</th>" for p in periods
    ) + "</tr>"
    rows = ""
    for d in days:
        row = f"<tr><td><strong>{d['name']}</strong></td>"
        for p in periods:
            e = cell_map.get((d["id"], p["id"]))
            if e:
                lab_cls = " class='lab'" if e["is_lab"] else ""
                row += f"<td{lab_cls}><strong>{e['sname'] or '—'}</strong><br>{e['stname'] or ''}"
                if e["is_lab"]: row += "<br><em>(Lab)</em>"
                row += "</td>"
            else:
                row += "<td>—</td>"
        row += "</tr>"
        rows += row

    html = EXPORT_HTML.format(
        class_name=class_name,
        ts=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        table=header + rows
    )
    buf = io.BytesIO(html.encode())
    return send_file(
        buf,
        mimetype="text/html",
        as_attachment=True,
        download_name=f"timetable_{class_name.replace(' ','_')}.html"
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("=" * 58)
    print("  AutoTT — Automatic Generative Timetable")
    print("=" * 58)
    print("  URL:  http://127.0.0.1:5000")
    print("  Login: admin / admin123")
    print("=" * 58)
    app.run(debug=True, port=5000)