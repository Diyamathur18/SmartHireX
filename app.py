import os
import re
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

from PyPDF2 import PdfReader


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SMARTHIREX_SECRET_KEY", "dev-secret-change-me")

# Store uploaded resumes on disk (not in the database).
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# SQLite database file (created automatically on first run).
DATABASE_PATH = os.path.join(BASE_DIR, "smarthirex.sqlite3")
app.config["DATABASE_PATH"] = DATABASE_PATH


# Only allow PDF uploads for resumes.
ALLOWED_EXTENSIONS = {"pdf"}


def get_db():
    """Open a SQLite connection for a request."""
    conn = sqlite3.connect(app.config["DATABASE_PATH"])
    conn.row_factory = sqlite3.Row
    # Safety and performance pragmas per connection
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    """Create the database schema if tables do not exist yet."""
    conn = get_db()
    cur = conn.cursor()

    # Disable FKs before dropping to avoid IntegrityError during reset.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.commit()

    # Drop tables in dependency order.
    cur.execute("DROP TABLE IF EXISTS applications")
    cur.execute("DROP TABLE IF EXISTS jobs")
    cur.execute("DROP TABLE IF EXISTS users")
    conn.commit()

    # Force reset users table to a canonical, backward-compatible schema that
    # includes the requested columns: id, name, email, password, skills.
    # We also retain legacy fields used by the app (username, password_hash, role, created_at, resume_*).
    cur.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Requested fields
            name TEXT,
            email TEXT UNIQUE,
            password TEXT,              -- stores hashed password (same content as password_hash)
            skills TEXT,

            -- Backward-compat fields for existing app features
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT CHECK(role IN ('job_seeker','employer')) DEFAULT 'job_seeker',
            created_at TEXT,
            resume_filename TEXT,
            resume_skills TEXT
        );
        """
    )

    # Jobs posted by employers (create function provides canonical schema).
    def create_jobs_table() -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                -- Requested columns
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                skills_required TEXT,
                description TEXT NOT NULL,

                -- Backward-compat/operational fields
                employer_id INTEGER,
                required_skills TEXT,
                created_at TEXT,
                FOREIGN KEY (employer_id) REFERENCES users(id)
            );
            """
        )
        conn.commit()

    create_jobs_table()

    # Job applications table (ensure minimal required columns exist).
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            job_id INTEGER,
            cover_letter TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            created_at TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )

    conn.commit()
    # Ensure new fields exist for simplified auth (name, skills).
    def ensure_column(table: str, column: str, ddl: str) -> None:
        """
        Add a column only if it doesn't already exist.
        - Case-insensitive check to avoid duplicate column errors.
        - Gracefully ignore concurrent/legacy additions that may already exist.
        """
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing_cols = {str(row[1]).lower() for row in info}
        if column.lower() not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                conn.commit()
            except sqlite3.OperationalError as e:
                # Ignore if another process or legacy migration already added it.
                if "duplicate column name" in str(e).lower():
                    pass
                else:
                    raise

    # Create canonical users table if missing; otherwise perform safe, additive migrations.
    def create_users_table() -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                name TEXT,
                email TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('job_seeker','employer')) DEFAULT 'job_seeker',
                created_at TEXT NOT NULL,
                resume_filename TEXT,
                resume_skills TEXT,
                skills TEXT
            );
            """
        )
        conn.commit()

    create_users_table()
    users_info = conn.execute("PRAGMA table_info(users)").fetchall()
    user_cols = {str(r[1]).lower() for r in users_info}
    skills_missing = "skills" not in user_cols
    # If table exists but lacks 'skills' and has no data, rebuild to stable schema.
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        user_count = int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        user_count = 0
    if skills_missing and user_count == 0:
        conn.execute("DROP TABLE IF EXISTS users_backup")
        conn.execute("ALTER TABLE users RENAME TO users_backup")
        conn.commit()
        create_users_table()
        conn.execute("DROP TABLE IF EXISTS users_backup")
        conn.commit()
    else:
        # Additive-only migrations for live DBs
        ensure_column("users", "name", "TEXT")
        ensure_column("users", "skills", "TEXT")
    # If jobs table exists but lacks 'skills_required' and has no rows, rebuild to canonical schema.
    jobs_info = conn.execute("PRAGMA table_info(jobs)").fetchall()
    job_cols = {str(r[1]).lower() for r in jobs_info} if jobs_info else set()
    jobs_missing_sk = "skills_required" not in job_cols
    jobs_count = 0
    try:
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        jobs_count = int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        jobs_count = 0
    if jobs_missing_sk and jobs_count == 0:
        # Recreate from scratch without using a backup table name.
        conn.execute("DROP TABLE IF EXISTS jobs")
        conn.commit()
        create_jobs_table()
    else:
        # Back-compat field for jobs to meet requested schema name.
        ensure_column("jobs", "skills_required", "TEXT")

    # Applications: if missing minimal columns, add them non-destructively.
    apps_info = conn.execute("PRAGMA table_info(applications)").fetchall()
    app_cols = {str(r[1]).lower() for r in apps_info} if apps_info else set()
    if "user_id" not in app_cols:
        ensure_column("applications", "user_id", "INTEGER")
    if "job_id" not in app_cols:
        ensure_column("applications", "job_id", "INTEGER")

    # Helpful indexes for common queries and relationships.
    def ensure_index(name: str, ddl: str) -> None:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (name,))
        if cur.fetchone() is None:
            conn.execute(ddl)
            conn.commit()

    ensure_index("idx_jobs_employer_id", "CREATE INDEX idx_jobs_employer_id ON jobs(employer_id)")
    ensure_index("idx_jobs_created_at", "CREATE INDEX idx_jobs_created_at ON jobs(created_at)")
    ensure_index("idx_applications_job_id", "CREATE INDEX idx_applications_job_id ON applications(job_id)")
    # Indexes reflect current schema (user_id, job_id)
    ensure_index("idx_applications_created_at", "CREATE INDEX idx_applications_created_at ON applications(created_at)")

    # Re-enable FKs after schema creation.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()


# Initialize schema when the app module is imported.
# This keeps the app compatible with Flask versions that removed
# the old `before_first_request` hook.
init_db()


def allowed_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower().strip()
    return ext in ALLOWED_EXTENSIONS


def extract_text_from_resume(file_path: str) -> str:
    """Extract text from a .txt or .pdf resume file."""
    ext = os.path.splitext(file_path)[1].lower().strip(".")
    if ext == "txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    if ext == "pdf":
        reader = PdfReader(file_path)
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)

    return ""


# A small, deterministic skill keyword map for demo recommendations.
# Keys are lowercase keyword patterns; values are normalized skill labels.
SKILL_KEYWORDS = {
    r"\bpython\b": "Python",
    r"\bdjango\b": "Django",
    r"\bflask\b": "Flask",
    r"\bjavascript\b": "JavaScript",
    r"\breact\b": "React",
    r"\bnode\.?js\b": "Node.js",
    r"\bhtml\b": "HTML",
    r"\bcss\b": "CSS",
    r"\bsql\b": "SQL",
    r"\bpostgresql\b": "PostgreSQL",
    r"\bmysql\b": "MySQL",
    r"\ba?ws\b|amazon web services": "AWS",
    r"\bdocker\b": "Docker",
    r"\bkubernetes\b": "Kubernetes",
    r"\baws\s*lambda\b": "AWS Lambda",
    r"\bgit\b": "Git",
    r"\blinux\b": "Linux",
    r"\bmachine learning\b": "Machine Learning",
    r"\bpandas\b": "Pandas",
    r"\bnumpy\b": "NumPy",
}


def extract_skills(text: str) -> list[str]:
    """
    Convert resume text into normalized skill labels using keyword matching.
    This is intentionally lightweight so it works without external NLP services.
    """
    normalized = (text or "").lower()
    found = []
    for pattern, label in SKILL_KEYWORDS.items():
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            found.append(label)

    # Deterministic output order for stable ranking.
    return sorted(set(found))


def to_skill_set(skills_csv: str) -> set[str]:
    """Convert a comma-separated skills string to a normalized set."""
    if not skills_csv:
        return set()
    parts = [p.strip() for p in skills_csv.split(",") if p.strip()]

    # Map lowercase canonical labels to their normalized/display form.
    canonical_map = {value.lower(): value for value in SKILL_KEYWORDS.values()}

    def normalize_token(token: str) -> str:
        t = token.strip().lower()
        if not t:
            return ""
        if t in canonical_map:
            return canonical_map[t]

        # Common user input variations.
        t_no_space = t.replace(" ", "")
        if t_no_space == "nodejs":
            return "Node.js"
        if t_no_space in {"postgres", "postgresql"}:
            return "PostgreSQL"
        if t_no_space in {"webservices", "amazonwebservices"}:
            return "AWS"

        # Fallback: keep token as-is (trimmed) so skills matching isn't too strict.
        return token.strip()

    normalized = set()
    for p in parts:
        norm = normalize_token(p)
        if norm:
            normalized.add(norm)
    return normalized


def skill_similarity(seeker_skills: set[str], job_skills: set[str]) -> float:
    """
    Jaccard similarity: |intersection| / |union|.
    If either set is empty, return 0.0.
    """
    if not seeker_skills or not job_skills:
        return 0.0
    inter = seeker_skills.intersection(job_skills)
    union = seeker_skills.union(job_skills)
    return len(inter) / max(len(union), 1)


def aggregate_user_skills(user_row) -> set[str]:
    """
    Combine user-entered `skills` with extracted `resume_skills`.
    """
    manual = to_skill_set(user_row["skills"]) if "skills" in user_row.keys() else set()
    extracted = to_skill_set(user_row["resume_skills"]) if "resume_skills" in user_row.keys() else set()
    return manual.union(extracted)


def extract_job_required_skills(job_row) -> set[str]:
    """
    Prefer `skills_required`, fallback to legacy `required_skills`.
    """
    raw = job_row["skills_required"] if "skills_required" in job_row.keys() and job_row["skills_required"] else job_row.get("required_skills")
    return to_skill_set(raw)


def verify_user_password(user_row, candidate_password: str) -> bool:
    """
    Verify a user's password in a backward-compatible way.
    Priority:
    1) If password_hash is present, verify using werkzeug's check_password_hash.
    2) Else if password column is present:
       - First attempt check_password_hash in case it stores a hash.
       - Fallback to plain-text equality if not a valid hash.
    """
    if not user_row:
        return False
    # Prefer password_hash
    if "password_hash" in user_row.keys() and user_row["password_hash"]:
        try:
            return check_password_hash(user_row["password_hash"], candidate_password)
        except Exception:
            pass
    # Fallback to password column
    if "password" in user_row.keys() and user_row["password"]:
        # Try interpreting as a hash first
        try:
            if check_password_hash(user_row["password"], candidate_password):
                return True
        except Exception:
            # Not a recognized hash format; fallback to plaintext compare
            return user_row["password"] == candidate_password
        # If check_password_hash ran and returned False, final fallback to plaintext
        return user_row["password"] == candidate_password
    return False
def login_required(role: str | None = None):
    """Decorator to require login; optionally enforce a role."""

    def decorator(view_fn):
        @wraps(view_fn)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in first.", "warning")
                # Job seekers have a separate login page; default to it when role is not specified.
                if role is None or role == "job_seeker":
                    return redirect(url_for("login_seeker"))
                return redirect(url_for("login"))

            if role is not None and session.get("role") != role:
                flash("You do not have access to that page.", "warning")
                return redirect(url_for("dashboard"))

            return view_fn(*args, **kwargs)

        return wrapper

    return decorator


def current_user():
    """Fetch the currently logged in user from the database."""
    if "user_id" not in session:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],))
    row = cur.fetchone()
    conn.close()
    return row


@app.get("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login_seeker"))
    return redirect(url_for("dashboard"))

#
# Simple authentication (email + password) endpoints
# These coexist with role-specific logins already present.
#
@app.route("/auth/register", methods=["GET", "POST"])
def auth_register():
    """
    Simplified user registration capturing: name, email, password, skills.
    Users created here default to job_seeker role to fit existing flows.
    """
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        skills = request.form.get("skills", "").strip()

        if not name or not email or not password:
            flash("Name, email, and password are required.", "danger")
            return redirect(url_for("auth_register"))

        password_hash = generate_password_hash(password)
        created_at = datetime.utcnow().isoformat()

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO users (username, name, email, password, password_hash, role, created_at, resume_skills, skills)
                VALUES (?, ?, ?, ?, ?, 'job_seeker', ?, ?, ?)
                """,
                (
                    # Keep username to avoid breaking existing unique constraints: derive from email prefix if needed
                    (email.split("@")[0] or name.replace(" ", "_"))[:50],
                    name,
                    email,
                    password_hash,  # store hashed in `password` (column name requirement)
                    password_hash,
                    created_at,
                    skills,  # also initialize resume_skills to entered skills
                    skills,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("An account with that username or email already exists.", "danger")
            return redirect(url_for("auth_register"))
        conn.close()

        flash("Account created. Please log in.", "success")
        return redirect(url_for("auth_login"))

    return render_template("auth_register.html")


@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    """
    Simplified login by email + password. Sets Flask session.
    Works for any role; redirects to dashboard after login.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required.", "danger")
            return redirect(url_for("auth_login"))

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        conn.close()

        if user and verify_user_password(user, password):
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["username"] = user["username"]
            flash("Logged in.", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "danger")
        return redirect(url_for("auth_login"))

    return render_template("auth_login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        skills = request.form.get("skills", "").strip()
        role = request.form.get("role", "job_seeker").strip()

        if role not in ("job_seeker", "employer"):
            flash("Invalid role.", "danger")
            return redirect(url_for("register"))

        if not username or not password:
            flash("Username and password are required.", "danger")
            return redirect(url_for("register"))

        password_hash = generate_password_hash(password)
        created_at = datetime.utcnow().isoformat()

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO users (username, email, password, password_hash, role, created_at, skills)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    email if email else None,
                    password_hash,  # store hashed in `password` (column name requirement)
                    password_hash,
                    role,
                    created_at,
                    skills if skills else None,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("Username or email already exists.", "danger")
            return redirect(url_for("register"))

        conn.close()
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login_seeker" if role == "job_seeker" else "login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login for employers/recruiters."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ? AND role = 'employer'", (username,))
        user = cur.fetchone()
        conn.close()

        if user and verify_user_password(user, password):
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["username"] = user["username"]
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid login for employer.", "danger")
        return redirect(url_for("login"))

    return render_template("login_employer.html")


@app.route("/login_seeker", methods=["GET", "POST"])
def login_seeker():
    """Separate login for job seekers."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ? AND role = 'job_seeker'", (username,))
        user = cur.fetchone()
        conn.close()

        if user and verify_user_password(user, password):
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["username"] = user["username"]
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid login for job seeker.", "danger")
        return redirect(url_for("login_seeker"))

    return render_template("login_seeker.html")


@app.get("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login_seeker"))


@app.route("/dashboard")
@login_required()
def dashboard():
    user = current_user()
    if user is None:
        flash("Please log in again.", "warning")
        return redirect(url_for("login_seeker"))

    # Use union of manual and extracted skills for matching
    seeker_skills = aggregate_user_skills(user)
    recommendations = []
    all_jobs = []

    # Only job seekers get recommendations.
    if user["role"] == "job_seeker":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs ORDER BY id DESC")
        jobs = cur.fetchall()
        all_jobs = jobs
        for job in jobs:
            job_skills = extract_job_required_skills(job)
            # Simple keyword matching: score by number of overlapping skills
            score = len(seeker_skills.intersection(job_skills))
            recommendations.append((score, job))

        # Sort by score (desc), then most recent job.
        recommendations.sort(key=lambda t: (t[0], t[1]["id"]), reverse=True)
        recommendations = [job for score, job in recommendations if score > 0.0][:5]
        conn.close()

        # Fetch applications.
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.*, j.title, j.company
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.user_id = ?
            ORDER BY a.created_at DESC
            """,
            (user["id"],),
        )
        applications = cur.fetchall()
        conn.close()
    else:
        applications = []
        # Employers: show their posted jobs first
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        all_jobs = cur.fetchall()
        conn.close()

    # Render employer or seeker-specific UI.
    return render_template(
        "dashboard.html",
        user=user,
        recommendations=recommendations,
        applications=applications,
        all_jobs=all_jobs,
    )


@app.route("/resume", methods=["GET", "POST"])
@login_required(role="job_seeker")
def resume():
    """Allow a job seeker to upload a resume (PDF/TXT) and extract skills."""
    user = current_user()
    if user is None:
        flash("Please log in again.", "warning")
        return redirect(url_for("login_seeker"))

    if request.method == "POST":
        file = request.files.get("resume")
        if not file or file.filename == "":
            flash("Please select a resume file.", "danger")
            return redirect(url_for("resume"))
        if not allowed_file(file.filename):
            flash("Only PDF resumes are supported.", "danger")
            return redirect(url_for("resume"))

        # Save file using a deterministic name (avoids collisions).
        ext = file.filename.rsplit(".", 1)[1].lower()
        safe_base = re.sub(r"[^a-zA-Z0-9_-]+", "_", user["username"])
        filename = f"{safe_base}_resume.{ext}"
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(file_path)

        text = extract_text_from_resume(file_path)
        skills = extract_skills(text)
        skills_csv = ", ".join(skills)
        # Store relative path for convenience
        resume_rel_path = os.path.join("uploads", filename)

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET resume_filename = ?, resume_skills = ?
            WHERE id = ?
            """,
            (resume_rel_path, skills_csv, user["id"]),
        )
        conn.commit()
        conn.close()

        flash(f"Resume uploaded. Extracted {len(skills)} skills.", "success")
        return redirect(url_for("dashboard"))

    return render_template("resume.html", user=user)


@app.get("/uploads/<path:filename>")
@login_required()
def uploaded_file(filename: str):
    """
    Serve uploaded resumes. In a production app you would add proper access controls.
    """
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/jobs")
@login_required()
def jobs():
    """List all jobs (employer can still view for admin/testing)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs ORDER BY created_at DESC")
    job_list = cur.fetchall()
    conn.close()
    return render_template("jobs.html", user=current_user(), jobs=job_list)


@app.route("/jobs/new", methods=["GET", "POST"])
@login_required(role="employer")
def job_post():
    """Employer job posting form + persistence."""
    if request.method == "POST":
        user = current_user()
        if user is None:
            flash("Please log in again.", "warning")
            return redirect(url_for("login"))
        title = request.form.get("title", "").strip()
        company = request.form.get("company", "").strip()
        description = request.form.get("description", "").strip()
        # Accept either name; prefer skills_required for new schema
        skills_required = request.form.get("skills_required", "").strip() or request.form.get("required_skills", "").strip()

        if not title or not company or not description or not skills_required:
            flash("Title, company, description, and required skills are required.", "danger")
            return redirect(url_for("job_post"))

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO jobs (employer_id, title, company, description, required_skills, skills_required, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                title,
                company,
                description,
                skills_required,
                skills_required,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        flash("Job posted successfully.", "success")
        return redirect(url_for("dashboard"))

    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    return render_template("job_post.html", user=user)


@app.route("/apply/<int:job_id>", methods=["GET", "POST"])
@login_required(role="job_seeker")
def apply(job_id: int):
    user = current_user()
    if user is None:
        flash("Please log in again.", "warning")
        return redirect(url_for("login_seeker"))

    conn = get_db()
    cur = conn.cursor()
    # Use primary jobs table for lookup
    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = cur.fetchone()
    if job is None:
        conn.close()
        flash("Job not found.", "danger")
        return redirect(url_for("jobs"))

    if request.method == "POST":
        cover_letter = request.form.get("cover_letter", "").strip()
        created_at = datetime.utcnow().isoformat()
        # Insert using minimal required columns; optional fields if present
        cur.execute(
            "INSERT INTO applications (user_id, job_id, cover_letter, status, created_at) VALUES (?, ?, ?, 'submitted', ?)",
            (user["id"], job_id, cover_letter if cover_letter else None, created_at),
        )
        conn.commit()
        conn.close()
        flash("Application submitted.", "success")
        return redirect(url_for("dashboard"))

    conn.close()
    return render_template("apply.html", user=user, job=job)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)

