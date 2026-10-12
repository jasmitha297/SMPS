import sqlite3
import re
import uuid
import math
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = "smart-park-secret-key-production-ready"

DATABASE = "parking.db"


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    # Phase 1: User Authentication Table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Phase 2: Parking Slots Table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parking_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_number TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'Available'
                CHECK(status IN ('Available', 'Occupied')),
            vehicle_number TEXT,
            booked_by INTEGER,
            FOREIGN KEY (booked_by) REFERENCES users(id)
        )
    """)

    # Phase 4: Vehicle Management Table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            vehicle_number TEXT NOT NULL,
            vehicle_type TEXT NOT NULL,
            model TEXT,
            color TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, vehicle_number)
        )
    """)

    # Phase 3: Bookings and Reservations Table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_code TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            slot_id INTEGER NOT NULL,
            vehicle_id INTEGER,
            vehicle_number TEXT NOT NULL,
            booking_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_hours INTEGER DEFAULT 2,
            end_time TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'Active'
                CHECK(status IN ('Active', 'Completed', 'Cancelled')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (slot_id) REFERENCES parking_slots(id),
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)
        )
    """)

    # Phase 6: Pricing Configuration Table (Task 6.2)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            vehicle_type TEXT NOT NULL DEFAULT 'All',
            hourly_rate REAL NOT NULL,
            minimum_charge REAL NOT NULL DEFAULT 0.0,
            billing_interval_minutes INTEGER NOT NULL DEFAULT 60,
            rounding_policy TEXT NOT NULL DEFAULT 'ceiling_hour'
                CHECK(rounding_policy IN ('ceiling_hour', 'exact_minute', 'half_hour')),
            effective_start TIMESTAMP,
            effective_end TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Phase 5: Parking Sessions Table (Task 5.1)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parking_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_code TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            vehicle_id INTEGER,
            vehicle_number TEXT NOT NULL,
            slot_id INTEGER NOT NULL,
            booking_id INTEGER,
            entry_time TIMESTAMP NOT NULL,
            exit_time TIMESTAMP,
            duration_minutes INTEGER DEFAULT 0,
            billed_hours INTEGER DEFAULT 0,
            hourly_rate REAL NOT NULL,
            pricing_rule_id INTEGER,
            pricing_rule_name TEXT,
            calculated_fee REAL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'Active'
                CHECK(status IN ('Active', 'Completed', 'Cancelled')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id),
            FOREIGN KEY (slot_id) REFERENCES parking_slots(id),
            FOREIGN KEY (booking_id) REFERENCES bookings(id),
            FOREIGN KEY (pricing_rule_id) REFERENCES pricing_rules(id)
        )
    """)

    # Phase 6: Billing Records Table (Task 6.6)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_reference TEXT NOT NULL UNIQUE,
            session_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            vehicle_number TEXT NOT NULL,
            slot_id INTEGER NOT NULL,
            pricing_rule_name TEXT NOT NULL,
            hourly_rate REAL NOT NULL,
            entry_time TIMESTAMP NOT NULL,
            exit_time TIMESTAMP NOT NULL,
            duration_minutes INTEGER NOT NULL,
            billed_hours INTEGER NOT NULL,
            rounding_policy TEXT NOT NULL DEFAULT 'ceiling_hour',
            subtotal REAL NOT NULL,
            discount_amount REAL NOT NULL DEFAULT 0.0,
            tax_amount REAL NOT NULL DEFAULT 0.0,
            final_amount REAL NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'Pending Payment'
                CHECK(payment_status IN ('Pending Payment', 'Paid', 'Waived')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES parking_sessions(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (slot_id) REFERENCES parking_slots(id)
        )
    """)

    # Indexes for performance and query efficiency
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON parking_sessions(user_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_slot_status ON parking_sessions(slot_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_billing_session ON billing_records(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_billing_user ON billing_records(user_id)")

    # Ensure is_admin column in users table
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_admin" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    # Add sample slots if table is empty
    slot_count = conn.execute("SELECT COUNT(*) FROM parking_slots").fetchone()[0]
    if slot_count == 0:
        sample_slots = [
            ("A01",), ("A02",), ("A03",), ("A04",),
            ("B01",), ("B02",), ("B03",), ("B04",),
            ("C01",), ("C02",), ("C03",), ("C04",)
        ]
        conn.executemany("INSERT INTO parking_slots (slot_number) VALUES (?)", sample_slots)

    # Seed default pricing rules if empty (Task 6.2)
    pricing_count = conn.execute("SELECT COUNT(*) FROM pricing_rules").fetchone()[0]
    if pricing_count == 0:
        sample_pricing_rules = [
            ("Standard Four-Wheeler (Car)", "Car", 30.0, 30.0, 60, "ceiling_hour", 1),
            ("Two-Wheeler (Bike / Scooter)", "Two-Wheeler", 15.0, 15.0, 60, "ceiling_hour", 1),
            ("Heavy / SUV Parking", "SUV", 45.0, 45.0, 60, "ceiling_hour", 1),
            ("Electric Vehicle (EV)", "EV", 25.0, 25.0, 60, "ceiling_hour", 1),
            ("Default Universal Rate", "All", 30.0, 30.0, 60, "ceiling_hour", 1)
        ]
        conn.executemany("""
            INSERT INTO pricing_rules (rule_name, vehicle_type, hourly_rate, minimum_charge, billing_interval_minutes, rounding_policy, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, sample_pricing_rules)

    # Seamless Data Migration: link any pre-existing occupied slots with vehicle and booking records
    occupied_slots = conn.execute(
        "SELECT id, slot_number, vehicle_number, booked_by FROM parking_slots WHERE status = 'Occupied' AND booked_by IS NOT NULL"
    ).fetchall()

    for s in occupied_slots:
        if s["vehicle_number"] and s["booked_by"]:
            # Auto-ensure vehicle
            veh = conn.execute(
                "SELECT id, vehicle_type FROM vehicles WHERE user_id = ? AND vehicle_number = ?",
                (s["booked_by"], s["vehicle_number"])
            ).fetchone()
            if not veh:
                conn.execute(
                    "INSERT OR IGNORE INTO vehicles (user_id, vehicle_number, vehicle_type, model, color) VALUES (?, ?, ?, ?, ?)",
                    (s["booked_by"], s["vehicle_number"], "Car", "Vehicle", "Silver")
                )
                veh = conn.execute(
                    "SELECT id, vehicle_type FROM vehicles WHERE user_id = ? AND vehicle_number = ?",
                    (s["booked_by"], s["vehicle_number"])
                ).fetchone()
            veh_id = veh["id"] if veh else None
            veh_type = veh["vehicle_type"] if veh else "Car"

            # Auto-ensure active booking
            bk = conn.execute(
                "SELECT id, start_time FROM bookings WHERE slot_id = ? AND status = 'Active'",
                (s["id"],)
            ).fetchone()
            if not bk:
                code = f"SPK-{uuid.uuid4().hex[:6].upper()}"
                now = datetime.now()
                end_time = now + timedelta(hours=2)
                conn.execute(
                    """
                    INSERT INTO bookings (booking_code, user_id, slot_id, vehicle_id, vehicle_number, start_time, duration_hours, end_time, status)
                    VALUES (?, ?, ?, ?, ?, ?, 2, ?, 'Active')
                    """,
                    (code, s["booked_by"], s["id"], veh_id, s["vehicle_number"], now.strftime("%Y-%m-%d %H:%M:%S"), end_time.strftime("%Y-%m-%d %H:%M:%S"))
                )
                bk = conn.execute("SELECT id, start_time FROM bookings WHERE slot_id = ? AND status = 'Active'", (s["id"],)).fetchone()

            # Auto-ensure active parking session if none exists
            ses = conn.execute(
                "SELECT id FROM parking_sessions WHERE slot_id = ? AND status = 'Active'",
                (s["id"],)
            ).fetchone()
            if not ses:
                ses_code = f"SES-{uuid.uuid4().hex[:6].upper()}"
                entry_time = bk["start_time"] if bk else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT INTO parking_sessions (session_code, user_id, vehicle_id, vehicle_number, slot_id, booking_id, entry_time, hourly_rate, pricing_rule_id, pricing_rule_name, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 30.0, 1, 'Standard Four-Wheeler (Car)', 'Active')
                    """,
                    (ses_code, s["booked_by"], veh_id, s["vehicle_number"], s["id"], bk["id"] if bk else None, entry_time)
                )

    conn.commit()
    conn.close()


# Ensure tables are initialized
init_db()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        conn = get_db()
        user = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()
        conn.close()
        if user is None or not user["is_admin"]:
            flash("Admin access is required.")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped_view


@app.context_processor
def inject_global_data():
    is_admin = False
    active_count = 0
    vehicles_count = 0
    current_active_session = None
    if "user_id" in session:
        conn = get_db()
        user = conn.execute("SELECT is_admin FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if user:
            is_admin = bool(user["is_admin"])
            session["is_admin"] = is_admin
        vehicles_count = conn.execute(
            "SELECT COUNT(*) FROM vehicles WHERE user_id = ?", (session["user_id"],)
        ).fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status = 'Active'", (session["user_id"],)
        ).fetchone()[0]
        active_ses = conn.execute("""
            SELECT ps.*, s.slot_number
            FROM parking_sessions ps
            JOIN parking_slots s ON ps.slot_id = s.id
            WHERE ps.user_id = ? AND ps.status = 'Active'
            ORDER BY ps.id DESC LIMIT 1
        """, (session["user_id"],)).fetchone()
        if active_ses:
            current_active_session = dict(active_ses)
        conn.close()
    return {
        "is_admin": is_admin,
        "active_bookings_count": active_count,
        "registered_vehicles_count": vehicles_count,
        "current_active_session": current_active_session
    }


@app.template_filter("format_dt")
def format_dt(value):
    if not value:
        return "—"
    try:
        if isinstance(value, str):
            clean_str = value.split(".")[0].replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(clean_str, fmt)
                    return dt.strftime("%d %b %Y, %I:%M %p")
                except ValueError:
                    continue
        elif hasattr(value, "strftime"):
            return value.strftime("%d %b %Y, %I:%M %p")
        return str(value)
    except Exception:
        return str(value)


@app.template_filter("currency")
def currency_filter(amount):
    try:
        val = float(amount)
        return f"₹{val:.2f}"
    except (ValueError, TypeError):
        return f"₹{amount}"


@app.template_filter("format_duration")
def format_duration_filter(minutes):
    return format_duration(minutes)


def get_active_pricing_rule(vehicle_type: str = None) -> dict:
    """
    Task 6.2 & 6.5: Retrieve the appropriate active pricing rule for a vehicle type.
    Falls back to 'All' or system default.
    """
    conn = get_db()
    rule = None
    if vehicle_type:
        clean_vtype = vehicle_type.strip()
        rule = conn.execute("""
            SELECT * FROM pricing_rules
            WHERE is_active = 1 AND LOWER(vehicle_type) = LOWER(?)
            ORDER BY id ASC LIMIT 1
        """, (clean_vtype,)).fetchone()

        if not rule and any(w in clean_vtype.lower() for w in ["bike", "motorcycle", "scooter", "two"]):
            rule = conn.execute("""
                SELECT * FROM pricing_rules
                WHERE is_active = 1 AND (LOWER(vehicle_type) LIKE '%bike%' OR LOWER(vehicle_type) LIKE '%two%')
                ORDER BY id ASC LIMIT 1
            """).fetchone()

    if not rule:
        rule = conn.execute("""
            SELECT * FROM pricing_rules
            WHERE is_active = 1 AND LOWER(vehicle_type) = 'all'
            ORDER BY id ASC LIMIT 1
        """).fetchone()

    if not rule:
        rule = conn.execute("""
            SELECT * FROM pricing_rules
            WHERE is_active = 1
            ORDER BY id ASC LIMIT 1
        """).fetchone()

    conn.close()

    if rule:
        return dict(rule)

    return {
        "id": 1,
        "rule_name": "Standard Rate",
        "vehicle_type": "All",
        "hourly_rate": 30.0,
        "minimum_charge": 30.0,
        "billing_interval_minutes": 60,
        "rounding_policy": "ceiling_hour"
    }


def parse_datetime(dt_val) -> datetime:
    """Safely parse datetime from string or datetime object."""
    if isinstance(dt_val, datetime):
        return dt_val
    if not dt_val:
        return datetime.now()
    clean_str = str(dt_val).split(".")[0].replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean_str, fmt)
        except ValueError:
            continue
    return datetime.now()


def format_duration(total_minutes: int) -> str:
    """Task 5.6: Format duration in human readable hours and minutes."""
    if total_minutes is None:
        return "—"
    try:
        total_minutes = int(total_minutes)
    except (ValueError, TypeError):
        return "—"
    if total_minutes <= 0:
        return "Less than 1 min"
    days = total_minutes // 1440
    rem = total_minutes % 1440
    hours = rem // 60
    mins = rem % 60
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days > 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hr{'s' if hours > 1 else ''}")
    if mins > 0 or not parts:
        parts.append(f"{mins} min{'s' if mins > 1 else ''}")
    return " ".join(parts)


def calculate_parking_fee(entry_time, exit_time=None, pricing_rule=None, vehicle_type=None) -> dict:
    """
    Task 6.4: Reliable server-side fee calculation based on duration.
    Billing policy:
    - Default policy: 'ceiling_hour'
      Any fraction of an hour started counts as a billed hour.
    - Applies minimum charge if configured.
    - Floating-point safe rounding to 2 decimal places.
    """
    if pricing_rule is None:
        pricing_rule = get_active_pricing_rule(vehicle_type)

    entry_dt = parse_datetime(entry_time)
    exit_dt = parse_datetime(exit_time) if exit_time else datetime.now()

    total_seconds = max(0, int((exit_dt - entry_dt).total_seconds()))
    total_minutes = int(math.ceil(total_seconds / 60.0)) if total_seconds > 0 else 0

    rounding_policy = pricing_rule.get("rounding_policy", "ceiling_hour")
    hourly_rate = float(pricing_rule.get("hourly_rate", 30.0))
    min_charge = float(pricing_rule.get("minimum_charge", 0.0))

    if total_seconds <= 0:
        billed_hours = 1
    elif rounding_policy == "ceiling_hour":
        billed_hours = max(1, int(math.ceil(total_seconds / 3600.0)))
    elif rounding_policy == "half_hour":
        half_hours = max(1, int(math.ceil(total_seconds / 1800.0)))
        billed_hours = half_hours * 0.5
    else:
        billed_hours = max(0.25, round(total_seconds / 3600.0, 2))

    subtotal = round(billed_hours * hourly_rate, 2)
    final_amount = max(min_charge, subtotal)
    final_amount = round(final_amount, 2)

    return {
        "entry_dt": entry_dt,
        "exit_dt": exit_dt,
        "total_seconds": total_seconds,
        "duration_minutes": total_minutes,
        "billed_hours": billed_hours,
        "hourly_rate": hourly_rate,
        "minimum_charge": min_charge,
        "rounding_policy": rounding_policy,
        "subtotal": subtotal,
        "discount_amount": 0.0,
        "tax_amount": 0.0,
        "final_amount": final_amount,
        "pricing_rule_id": pricing_rule.get("id"),
        "pricing_rule_name": pricing_rule.get("rule_name", "Standard Rate")
    }


def validate_vehicle_number(v_num: str) -> tuple[bool, str, str]:
    """
    Task 4.3: Validate vehicle registration numbers.
    Accepts standard vehicle registration formats (e.g. KA 01 AB 1234, DL-04-1234, MH12DE4567).
    Returns (is_valid, sanitized_plate, error_message).
    """
    if not v_num:
        return False, "", "Vehicle registration number cannot be empty."

    cleaned = re.sub(r'[\s\-]+', ' ', v_num.strip()).upper()
    compact = re.sub(r'[\s\-]', '', cleaned)

    if len(compact) < 4 or len(compact) > 15:
        return False, "", "Vehicle registration number must be between 4 and 15 alphanumeric characters."

    if not re.fullmatch(r'^[A-Z0-9 ]+$', cleaned):
        return False, "", "Vehicle registration number can only contain letters, digits, spaces, and hyphens."

    has_alpha = any(c.isalpha() for c in compact)
    has_digit = any(c.isdigit() for c in compact)
    if not (has_alpha and has_digit):
        return False, "", "Vehicle registration number must contain both letters and digits (e.g. KA 01 AB 1234)."

    return True, cleaned, ""


def get_user_vehicles(user_id: int):
    """
    Task 4.5: Retrieve saved vehicle details for a user.
    """
    conn = get_db()
    vehicles = conn.execute("""
        SELECT v.*,
               ((SELECT COUNT(*) FROM bookings b WHERE b.vehicle_id = v.id AND b.status = 'Active') +
                (SELECT COUNT(*) FROM parking_sessions ps WHERE ps.vehicle_id = v.id AND ps.status = 'Active')) AS has_active_booking
        FROM vehicles v
        WHERE v.user_id = ?
        ORDER BY v.id DESC
    """, (user_id,)).fetchall()
    conn.close()
    return vehicles


# ----------------------------------------------------
# AUTHENTICATION ROUTES (Phase 1)
# ----------------------------------------------------

@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Please fill in every field.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
            return render_template("register.html")

        hashed_password = generate_password_hash(password)
        conn = get_db()

        try:
            conn.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                (name, email, hashed_password)
            )
            conn.commit()
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("An account with this email already exists.", "error")
        finally:
            conn.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ----------------------------------------------------
# DASHBOARD ROUTE
# ----------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    user = conn.execute(
        "SELECT name, is_admin FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()

    if user is None:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    # Slot overview stats
    stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'Available' THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN status = 'Occupied' THEN 1 ELSE 0 END) AS occupied
        FROM parking_slots
    """).fetchone()

    # User active bookings with slot & vehicle info
    active_bookings = conn.execute("""
        SELECT b.id, b.booking_code, b.start_time, b.duration_hours, b.end_time,
               s.slot_number,
               v.vehicle_number, v.vehicle_type, v.model
        FROM bookings b
        JOIN parking_slots s ON b.slot_id = s.id
        LEFT JOIN vehicles v ON b.vehicle_id = v.id
        WHERE b.user_id = ? AND b.status = 'Active'
        ORDER BY b.start_time DESC
    """, (session["user_id"],)).fetchall()

    # Phase 5: User active parking sessions
    active_sessions = conn.execute("""
        SELECT ps.*, s.slot_number, v.vehicle_type, v.model, v.color
        FROM parking_sessions ps
        JOIN parking_slots s ON ps.slot_id = s.id
        LEFT JOIN vehicles v ON ps.vehicle_id = v.id
        WHERE ps.user_id = ? AND ps.status = 'Active'
        ORDER BY ps.entry_time DESC
    """, (session["user_id"],)).fetchall()

    vehicles_count = conn.execute(
        "SELECT COUNT(*) FROM vehicles WHERE user_id = ?",
        (session["user_id"],)
    ).fetchone()[0]

    total_bookings_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE user_id = ?",
        (session["user_id"],)
    ).fetchone()[0]

    conn.close()

    return render_template(
        "dashboard.html",
        name=user["name"],
        is_admin=user["is_admin"],
        stats=stats,
        active_bookings=active_bookings,
        active_sessions=active_sessions,
        vehicles_count=vehicles_count,
        total_bookings_count=total_bookings_count
    )


# ----------------------------------------------------
# VEHICLE MANAGEMENT (Phase 4)
# ----------------------------------------------------

@app.route("/vehicles", methods=["GET", "POST"])
@login_required
def vehicles():
    """
    Phase 4: Vehicle Management
    Task 4.1: Design vehicle registration form
    Task 4.4: Store vehicle details
    Task 4.5: Retrieve saved vehicle details
    Task 4.6: Associate vehicles with users
    """
    if request.method == "POST":
        raw_number = request.form.get("vehicle_number", "")
        vehicle_type = request.form.get("vehicle_type", "Car").strip()
        model = request.form.get("model", "").strip()
        color = request.form.get("color", "").strip()

        # Task 4.3: Validate vehicle registration numbers
        is_valid, plate_number, err_msg = validate_vehicle_number(raw_number)
        if not is_valid:
            flash(err_msg, "error")
            return redirect(url_for("vehicles"))

        if not vehicle_type:
            vehicle_type = "Car"

        conn = get_db()
        # Task 4.6: Associate vehicles with users & prevent duplicate vehicle entry for same user
        existing = conn.execute(
            "SELECT id FROM vehicles WHERE user_id = ? AND vehicle_number = ?",
            (session["user_id"], plate_number)
        ).fetchone()

        if existing:
            conn.close()
            flash(f"Vehicle '{plate_number}' is already registered in your account.", "error")
            return redirect(url_for("vehicles"))

        # Task 4.4: Store vehicle details
        conn.execute(
            """
            INSERT INTO vehicles (user_id, vehicle_number, vehicle_type, model, color)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session["user_id"], plate_number, vehicle_type, model or None, color or None)
        )
        conn.commit()
        conn.close()

        flash(f"Vehicle '{plate_number}' successfully registered!", "success")

        # If user was redirected from slot booking page, send them back to complete booking
        if request.args.get("next") == "parking_slots":
            return redirect(url_for("parking_slots"))

        return redirect(url_for("vehicles"))

    user_vehicles = get_user_vehicles(session["user_id"])
    return render_template("vehicles.html", vehicles=user_vehicles)


@app.route("/vehicles/delete/<int:vehicle_id>", methods=["POST"])
@login_required
def delete_vehicle(vehicle_id):
    """
    Phase 4: Remove a registered vehicle safely
    """
    conn = get_db()
    veh = conn.execute(
        "SELECT * FROM vehicles WHERE id = ? AND user_id = ?",
        (vehicle_id, session["user_id"])
    ).fetchone()

    if not veh:
        conn.close()
        flash("Vehicle record not found.", "error")
        return redirect(url_for("vehicles"))

    # Prevent deleting if vehicle currently holds an active booking or session
    active_booking = conn.execute(
        "SELECT id FROM bookings WHERE vehicle_id = ? AND status = 'Active'",
        (vehicle_id,)
    ).fetchone()

    active_session = conn.execute(
        "SELECT id FROM parking_sessions WHERE vehicle_id = ? AND status = 'Active'",
        (vehicle_id,)
    ).fetchone()

    if active_booking or active_session:
        conn.close()
        flash(
            f"Cannot delete vehicle {veh['vehicle_number']} because it is currently parked in an active reservation or session. End the session or cancel the reservation first.",
            "error"
        )
        return redirect(url_for("vehicles"))

    conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    conn.commit()
    conn.close()

    flash(f"Vehicle {veh['vehicle_number']} removed successfully.", "success")
    return redirect(url_for("vehicles"))


# ----------------------------------------------------
# PARKING SLOTS & SESSIONS (Phases 2, 3, 5 & 6)
# ----------------------------------------------------

@app.route("/parking-slots")
@login_required
def parking_slots():
    """
    Phase 2, Phase 3 & Phase 5:
    Displays all slots, user's registered vehicles, active sessions, and reservation options.
    """
    conn = get_db()
    slots = conn.execute("""
        SELECT id, slot_number, status, vehicle_number, booked_by
        FROM parking_slots
        ORDER BY slot_number
    """).fetchall()

    stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'Available' THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN status = 'Occupied' THEN 1 ELSE 0 END) AS occupied
        FROM parking_slots
    """).fetchone()

    # Active bookings made by the current user
    user_active_rows = conn.execute("""
        SELECT b.id AS booking_id, b.booking_code, b.slot_id, b.vehicle_number
        FROM bookings b
        WHERE b.user_id = ? AND b.status = 'Active'
    """, (session["user_id"],)).fetchall()

    user_active_slots = {r["slot_id"]: dict(r) for r in user_active_rows}

    # Active parking sessions made by the current user (Phase 5)
    user_active_sessions_rows = conn.execute("""
        SELECT ps.id AS session_id, ps.session_code, ps.slot_id, ps.vehicle_number
        FROM parking_sessions ps
        WHERE ps.user_id = ? AND ps.status = 'Active'
    """, (session["user_id"],)).fetchall()

    user_active_sessions = {r["slot_id"]: dict(r) for r in user_active_sessions_rows}

    conn.close()

    # Retrieve user's registered vehicles for slot selection
    user_vehicles = get_user_vehicles(session["user_id"])

    return render_template(
        "parking_slots.html",
        slots=slots,
        stats=stats,
        user_vehicles=user_vehicles,
        user_active_slots=user_active_slots,
        user_active_sessions=user_active_sessions
    )


@app.route("/select-slot/<int:slot_id>", methods=["POST"])
@app.route("/book-slot/<int:slot_id>", methods=["POST"])
@app.route("/park-now/<int:slot_id>", methods=["POST"])
@login_required
def select_slot(slot_id):
    """
    Phase 3 & Phase 5:
    Task 3.2 - 3.6: Booking and Reservation creation
    Task 5.2 - 5.3: Immediate Parking Session start ("Park Now")
    """
    vehicle_id_raw = request.form.get("vehicle_id")
    duration_hours = request.form.get("duration", "2")
    action_mode = request.form.get("action_type")
    
    # If endpoint called is /park-now/<id>, force park_now mode
    if request.path.startswith("/park-now"):
        action_mode = "park_now"

    try:
        duration_hours = int(duration_hours)
        if duration_hours <= 0:
            duration_hours = 2
    except (ValueError, TypeError):
        duration_hours = 2

    # Verify user has selected a registered vehicle
    if not vehicle_id_raw:
        flash("You must select a registered vehicle to book or park in a slot. Please register a vehicle first if you haven't.", "error")
        return redirect(url_for("vehicles", next="parking_slots"))

    try:
        vehicle_id = int(vehicle_id_raw)
    except ValueError:
        flash("Invalid vehicle selected.", "error")
        return redirect(url_for("parking_slots"))

    conn = get_db()

    # Verify vehicle belongs to current user
    vehicle = conn.execute(
        "SELECT * FROM vehicles WHERE id = ? AND user_id = ?",
        (vehicle_id, session["user_id"])
    ).fetchone()

    if not vehicle:
        conn.close()
        flash("Selected vehicle was not found in your account.", "error")
        return redirect(url_for("parking_slots"))

    # Task 3.5 & Task 5.2: Prevent duplicate active occupancy/reservations
    active_vehicle_session = conn.execute("""
        SELECT ps.id, ps.session_code, s.slot_number
        FROM parking_sessions ps
        JOIN parking_slots s ON ps.slot_id = s.id
        WHERE ps.vehicle_id = ? AND ps.status = 'Active'
    """, (vehicle["id"],)).fetchone()

    if active_vehicle_session:
        conn.close()
        flash(
            f"Vehicle '{vehicle['vehicle_number']}' already has an active parking session at slot {active_vehicle_session['slot_number']} (Ref: {active_vehicle_session['session_code']}).",
            "error"
        )
        return redirect(url_for("active_session_view"))

    active_vehicle_booking = conn.execute("""
        SELECT b.id, b.booking_code, s.slot_number
        FROM bookings b
        JOIN parking_slots s ON b.slot_id = s.id
        WHERE b.vehicle_id = ? AND b.status = 'Active'
    """, (vehicle["id"],)).fetchone()

    if active_vehicle_booking and action_mode != "park_now":
        conn.close()
        flash(
            f"Vehicle '{vehicle['vehicle_number']}' already has an active reservation at slot {active_vehicle_booking['slot_number']} (Ref: {active_vehicle_booking['booking_code']}). Each vehicle can only hold one active reservation at a time.",
            "error"
        )
        return redirect(url_for("parking_slots"))

    # Task 3.4: Validate slot availability before booking / parking
    slot = conn.execute(
        "SELECT * FROM parking_slots WHERE id = ?",
        (slot_id,)
    ).fetchone()

    if not slot or slot["status"] != "Available":
        conn.close()
        flash("Sorry, this slot is no longer available. Please select another slot.", "error")
        return redirect(url_for("parking_slots"))

    # Task 3.6 / Task 5.7: Update slot status atomically
    cursor = conn.execute("""
        UPDATE parking_slots
        SET status = 'Occupied', vehicle_number = ?, booked_by = ?
        WHERE id = ? AND status = 'Available'
    """, (vehicle["vehicle_number"], session["user_id"], slot_id))

    if cursor.rowcount == 0:
        conn.close()
        flash("Sorry, this slot was just taken by another user. Please choose another slot.", "error")
        return redirect(url_for("parking_slots"))

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    booking_code = f"SPK-{uuid.uuid4().hex[:6].upper()}"
    end_time = now + timedelta(hours=duration_hours)
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    # Insert reservation record
    bk_cursor = conn.execute("""
        INSERT INTO bookings (
            booking_code, user_id, slot_id, vehicle_id, vehicle_number,
            booking_time, start_time, duration_hours, end_time, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active')
    """, (
        booking_code, session["user_id"], slot_id, vehicle["id"], vehicle["vehicle_number"],
        now_str, now_str, duration_hours, end_str
    ))
    booking_id = bk_cursor.lastrowid

    # If action_mode is park_now, immediately start live parking session (Task 5.2 & 5.3)
    if action_mode == "park_now":
        pricing_rule = get_active_pricing_rule(vehicle["vehicle_type"])
        session_code = f"SES-{uuid.uuid4().hex[:6].upper()}"

        conn.execute("""
            INSERT INTO parking_sessions (
                session_code, user_id, vehicle_id, vehicle_number, slot_id, booking_id,
                entry_time, hourly_rate, pricing_rule_id, pricing_rule_name, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active')
        """, (
            session_code, session["user_id"], vehicle["id"], vehicle["vehicle_number"], slot_id, booking_id,
            now_str, pricing_rule["hourly_rate"], pricing_rule.get("id"), pricing_rule.get("rule_name")
        ))
        conn.commit()
        conn.close()

        flash(
            f"Parking session started! Slot {slot['slot_number']} occupied by {vehicle['vehicle_number']}. Timer is now active.",
            "success"
        )
        return redirect(url_for("active_session_view"))

    conn.commit()
    conn.close()

    flash(
        f"Reservation successful! Slot {slot['slot_number']} reserved for {vehicle['vehicle_number']}. Ref: {booking_code}",
        "success"
    )
    return redirect(url_for("history"))


@app.route("/start-session/<int:booking_id>", methods=["POST"])
@login_required
def start_session_from_booking(booking_id):
    """
    Task 5.2 & 5.3: Check-in from an existing reservation to start the live parking session and timer.
    """
    conn = get_db()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()

    if not booking:
        conn.close()
        flash("Reservation not found.", "error")
        return redirect(url_for("history"))

    if booking["user_id"] != session["user_id"] and not session.get("is_admin"):
        conn.close()
        flash("You do not have permission to check in to this reservation.", "error")
        return redirect(url_for("history"))

    if booking["status"] != "Active":
        conn.close()
        flash(f"This reservation is already {booking['status'].lower()}.", "error")
        return redirect(url_for("history"))

    # Verify no active session already running for this booking
    existing_session = conn.execute(
        "SELECT id FROM parking_sessions WHERE booking_id = ? AND status = 'Active'",
        (booking_id,)
    ).fetchone()

    if existing_session:
        conn.close()
        flash("Parking session is already active for this reservation.", "info")
        return redirect(url_for("active_session_view"))

    vehicle = None
    if booking["vehicle_id"]:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id = ?", (booking["vehicle_id"],)).fetchone()

    vehicle_type = vehicle["vehicle_type"] if vehicle else "Car"
    pricing_rule = get_active_pricing_rule(vehicle_type)

    session_code = f"SES-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        INSERT INTO parking_sessions (
            session_code, user_id, vehicle_id, vehicle_number, slot_id, booking_id,
            entry_time, hourly_rate, pricing_rule_id, pricing_rule_name, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active')
    """, (
        session_code, booking["user_id"], booking["vehicle_id"], booking["vehicle_number"],
        booking["slot_id"], booking["id"], now_str, pricing_rule["hourly_rate"],
        pricing_rule.get("id"), pricing_rule.get("rule_name")
    ))

    conn.commit()
    conn.close()

    flash("Check-in confirmed! Live parking session has begun and timer is ticking.", "success")
    return redirect(url_for("active_session_view"))


@app.route("/active-session")
@login_required
def active_session_view():
    """
    Task 5.4: Display the live parking timer and active session status.
    """
    conn = get_db()
    session_row = conn.execute("""
        SELECT ps.*, s.slot_number, v.vehicle_type, v.model, v.color
        FROM parking_sessions ps
        JOIN parking_slots s ON ps.slot_id = s.id
        LEFT JOIN vehicles v ON ps.vehicle_id = v.id
        WHERE ps.user_id = ? AND ps.status = 'Active'
        ORDER BY ps.id DESC LIMIT 1
    """, (session["user_id"],)).fetchone()
    conn.close()

    if not session_row:
        return render_template("active_session.html", session_data=None)

    session_data = dict(session_row)
    entry_iso = str(session_data["entry_time"]).replace(" ", "T")
    current_estimate = calculate_parking_fee(
        session_data["entry_time"],
        exit_time=None,
        pricing_rule={"hourly_rate": session_data["hourly_rate"], "rounding_policy": "ceiling_hour", "minimum_charge": session_data["hourly_rate"]}
    )

    return render_template(
        "active_session.html",
        session_data=session_data,
        entry_iso=entry_iso,
        current_estimate=current_estimate
    )


@app.route("/api/session-status")
@login_required
def api_session_status():
    """Task 5.4 API: Provide real-time timer status and fee data."""
    conn = get_db()
    session_row = conn.execute("""
        SELECT ps.*, s.slot_number
        FROM parking_sessions ps
        JOIN parking_slots s ON ps.slot_id = s.id
        WHERE ps.user_id = ? AND ps.status = 'Active'
        ORDER BY ps.id DESC LIMIT 1
    """, (session["user_id"],)).fetchone()
    conn.close()

    if not session_row:
        return jsonify({"active": False})

    estimate = calculate_parking_fee(session_row["entry_time"])
    return jsonify({
        "active": True,
        "session_code": session_row["session_code"],
        "slot_number": session_row["slot_number"],
        "vehicle_number": session_row["vehicle_number"],
        "entry_time": session_row["entry_time"],
        "elapsed_seconds": estimate["total_seconds"],
        "duration_minutes": estimate["duration_minutes"],
        "duration_formatted": format_duration(estimate["duration_minutes"]),
        "billed_hours": estimate["billed_hours"],
        "hourly_rate": session_row["hourly_rate"],
        "estimated_fee": estimate["final_amount"]
    })


@app.route("/end-session/<int:session_id>", methods=["POST"])
@login_required
def end_session(session_id):
    """
    Task 5.5, 5.6, 5.7, 6.4, 6.6: End parking session, release slot, and generate bill.
    """
    conn = get_db()
    ses = conn.execute(
        "SELECT * FROM parking_sessions WHERE id = ?",
        (session_id,)
    ).fetchone()

    if not ses:
        conn.close()
        flash("Parking session record not found.", "error")
        return redirect(url_for("history"))

    if ses["user_id"] != session["user_id"] and not session.get("is_admin"):
        conn.close()
        flash("You do not have permission to check out this session.", "error")
        return redirect(url_for("history"))

    # Prevent duplicate checkout requests (Task 5.5)
    if ses["status"] != "Active":
        conn.close()
        flash(f"This parking session has already been {ses['status'].lower()}.", "info")
        return redirect(url_for("bill_summary", session_id=session_id))

    exit_time = datetime.now()
    exit_time_str = exit_time.strftime("%Y-%m-%d %H:%M:%S")

    # Fetch applicable pricing rule stored at creation
    pricing_rule = None
    if ses["pricing_rule_id"]:
        r = conn.execute("SELECT * FROM pricing_rules WHERE id = ?", (ses["pricing_rule_id"],)).fetchone()
        if r:
            pricing_rule = dict(r)

    if not pricing_rule:
        pricing_rule = {
            "hourly_rate": ses["hourly_rate"],
            "rule_name": ses["pricing_rule_name"] or "Standard Rate",
            "rounding_policy": "ceiling_hour",
            "minimum_charge": ses["hourly_rate"]
        }

    # Task 5.6 & 6.4: Calculate duration and final fee server-side
    calc = calculate_parking_fee(ses["entry_time"], exit_time, pricing_rule=pricing_rule)
    final_fee = calc["final_amount"]
    billed_hours = calc["billed_hours"]
    duration_minutes = calc["duration_minutes"]

    # Mark session Completed atomically
    cursor = conn.execute("""
        UPDATE parking_sessions
        SET status = 'Completed', exit_time = ?, duration_minutes = ?, billed_hours = ?, calculated_fee = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'Active'
    """, (exit_time_str, duration_minutes, billed_hours, final_fee, session_id))

    if cursor.rowcount == 0:
        conn.close()
        flash("Session was already ended by another request.", "info")
        return redirect(url_for("bill_summary", session_id=session_id))

    # Task 5.7: Release the parking slot
    conn.execute("""
        UPDATE parking_slots
        SET status = 'Available', vehicle_number = NULL, booked_by = NULL
        WHERE id = ?
    """, (ses["slot_id"],))

    # If linked to a booking, also mark booking Completed
    if ses["booking_id"]:
        conn.execute(
            "UPDATE bookings SET status = 'Completed' WHERE id = ?",
            (ses["booking_id"],)
        )

    # Task 6.6: Generate persistent billing summary
    bill_ref = f"BILL-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    conn.execute("""
        INSERT INTO billing_records (
            bill_reference, session_id, user_id, vehicle_number, slot_id,
            pricing_rule_name, hourly_rate, entry_time, exit_time,
            duration_minutes, billed_hours, rounding_policy, subtotal,
            discount_amount, tax_amount, final_amount, payment_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending Payment')
    """, (
        bill_ref, session_id, ses["user_id"], ses["vehicle_number"], ses["slot_id"],
        pricing_rule.get("rule_name", "Standard Rate"), calc["hourly_rate"], ses["entry_time"], exit_time_str,
        duration_minutes, billed_hours, calc["rounding_policy"], calc["subtotal"],
        0.0, 0.0, final_fee
    ))

    conn.commit()
    conn.close()

    flash("Parking session ended successfully and slot released. Here is your billing summary.", "success")
    return redirect(url_for("bill_summary", session_id=session_id))


@app.route("/bill/<int:session_id>")
@login_required
def bill_summary(session_id):
    """
    Task 6.1 & 6.7: Display billing summary invoice before payment.
    """
    conn = get_db()
    bill_row = conn.execute("""
        SELECT b.*,
               ps.session_code, ps.status AS session_status,
               s.slot_number,
               v.model, v.color, v.vehicle_type,
               u.name AS user_name, u.email AS user_email
        FROM billing_records b
        JOIN parking_sessions ps ON b.session_id = ps.id
        JOIN parking_slots s ON b.slot_id = s.id
        LEFT JOIN vehicles v ON ps.vehicle_id = v.id
        JOIN users u ON b.user_id = u.id
        WHERE b.session_id = ?
    """, (session_id,)).fetchone()

    if not bill_row:
        # Check if session exists and is completed but bill record missing
        ses = conn.execute("SELECT * FROM parking_sessions WHERE id = ?", (session_id,)).fetchone()
        if not ses:
            conn.close()
            flash("Billing record not found.", "error")
            return redirect(url_for("history"))

        # Create bill record if missing
        bill_ref = f"BILL-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        calc = calculate_parking_fee(ses["entry_time"], ses["exit_time"] or datetime.now())
        conn.execute("""
            INSERT OR IGNORE INTO billing_records (
                bill_reference, session_id, user_id, vehicle_number, slot_id,
                pricing_rule_name, hourly_rate, entry_time, exit_time,
                duration_minutes, billed_hours, rounding_policy, subtotal, final_amount, payment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending Payment')
        """, (
            bill_ref, ses["id"], ses["user_id"], ses["vehicle_number"], ses["slot_id"],
            ses["pricing_rule_name"] or "Standard Rate", ses["hourly_rate"], ses["entry_time"],
            ses["exit_time"] or ses["entry_time"], ses["duration_minutes"] or 0, ses["billed_hours"] or 1,
            "ceiling_hour", ses["calculated_fee"], ses["calculated_fee"]
        ))
        conn.commit()

        bill_row = conn.execute("""
            SELECT b.*,
                   ps.session_code, ps.status AS session_status,
                   s.slot_number,
                   v.model, v.color, v.vehicle_type,
                   u.name AS user_name, u.email AS user_email
            FROM billing_records b
            JOIN parking_sessions ps ON b.session_id = ps.id
            JOIN parking_slots s ON b.slot_id = s.id
            LEFT JOIN vehicles v ON ps.vehicle_id = v.id
            JOIN users u ON b.user_id = u.id
            WHERE b.session_id = ?
        """, (session_id,)).fetchone()

    conn.close()

    if not bill_row:
        flash("Billing invoice not available.", "error")
        return redirect(url_for("history"))

    if bill_row["user_id"] != session["user_id"] and not session.get("is_admin"):
        flash("Unauthorized access to billing invoice.", "error")
        return redirect(url_for("history"))

    return render_template("bill_summary.html", bill=dict(bill_row))


@app.route("/pay-bill/<int:bill_id>", methods=["POST"])
@login_required
def pay_bill(bill_id):
    """
    Task 6.7: Friendly simulation for Phase 7 payment transition.
    """
    conn = get_db()
    bill = conn.execute("SELECT * FROM billing_records WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        conn.close()
        flash("Bill not found.", "error")
        return redirect(url_for("history"))

    if bill["user_id"] != session["user_id"] and not session.get("is_admin"):
        conn.close()
        flash("Unauthorized.", "error")
        return redirect(url_for("history"))

    conn.execute("UPDATE billing_records SET payment_status = 'Paid' WHERE id = ?", (bill_id,))
    conn.commit()
    conn.close()

    flash("Payment simulated successfully! Invoice marked as Paid.", "success")
    return redirect(url_for("bill_summary", session_id=bill["session_id"]))


# ----------------------------------------------------
# BOOKING & SESSION HISTORY (Phase 3 & Phase 5)
# ----------------------------------------------------

@app.route("/history")
@login_required
def history():
    """
    Task 5.8: Parking History Page
    Displays completed sessions, active timers, advance reservations, and invoices.
    Supports filtering by tab, status, and search query.
    """
    active_tab = request.args.get("tab", "all").strip().lower()
    selected_status = request.args.get("status", "all").strip().lower()
    search_query = request.args.get("q", "").strip()
    is_admin_view = request.args.get("view") == "all" and session.get("is_admin")

    conn = get_db()

    # 1. Fetch Parking Sessions (Phase 5)
    ses_where = ["1=1"]
    ses_params = []
    if not is_admin_view:
        ses_where.append("ps.user_id = ?")
        ses_params.append(session["user_id"])

    sessions_sql = f"""
        SELECT ps.id, ps.session_code AS code, 'session' AS type,
               ps.slot_id, s.slot_number,
               ps.vehicle_number, v.vehicle_type, v.model, v.color,
               ps.entry_time AS start_time, ps.exit_time AS end_time,
               ps.duration_minutes, ps.calculated_fee AS fee,
               ps.status, ps.user_id,
               u.name AS user_name, u.email AS user_email,
               b.bill_reference, b.payment_status
        FROM parking_sessions ps
        JOIN parking_slots s ON ps.slot_id = s.id
        LEFT JOIN vehicles v ON ps.vehicle_id = v.id
        LEFT JOIN billing_records b ON ps.id = b.session_id
        LEFT JOIN users u ON ps.user_id = u.id
        WHERE {' AND '.join(ses_where)}
        ORDER BY ps.entry_time DESC
    """
    raw_sessions = [dict(r) for r in conn.execute(sessions_sql, ses_params).fetchall()]

    # 2. Fetch Bookings / Reservations (Phase 3)
    bk_where = ["1=1"]
    bk_params = []
    if not is_admin_view:
        bk_where.append("b.user_id = ?")
        bk_params.append(session["user_id"])

    bookings_sql = f"""
        SELECT b.id, b.booking_code AS code, 'booking' AS type,
               b.slot_id, s.slot_number,
               b.vehicle_number, v.vehicle_type, v.model, v.color,
               b.start_time, b.end_time,
               (b.duration_hours * 60) AS duration_minutes,
               0.0 AS fee,
               b.status, b.user_id,
               u.name AS user_name, u.email AS user_email,
               NULL AS bill_reference, NULL AS payment_status
        FROM bookings b
        LEFT JOIN parking_slots s ON b.slot_id = s.id
        LEFT JOIN vehicles v ON b.vehicle_id = v.id
        LEFT JOIN users u ON b.user_id = u.id
        WHERE {' AND '.join(bk_where)}
        ORDER BY b.booking_time DESC
    """
    raw_bookings = [dict(r) for r in conn.execute(bookings_sql, bk_params).fetchall()]

    # Format human durations for records
    for r in raw_sessions:
        if r["status"] == "Completed" and r["duration_minutes"] is not None:
            r["duration_text"] = format_duration(r["duration_minutes"])
        elif r["status"] == "Active":
            est = calculate_parking_fee(r["start_time"])
            r["duration_text"] = f"{format_duration(est['duration_minutes'])} (live)"
            r["fee"] = est["final_amount"]
        else:
            r["duration_text"] = "—"

    for r in raw_bookings:
        r["duration_text"] = f"{r['duration_minutes'] // 60} hrs reserved"

    # Overall Metrics Stats
    total_revenue = sum(r["fee"] or 0.0 for r in raw_sessions if r["status"] == "Completed")
    active_sessions_count = sum(1 for r in raw_sessions if r["status"] == "Active")
    completed_sessions_count = sum(1 for r in raw_sessions if r["status"] == "Completed")
    active_reservations_count = sum(1 for r in raw_bookings if r["status"] == "Active")

    # Combine or filter by tab
    combined = []
    if active_tab == "sessions":
        combined = raw_sessions
    elif active_tab == "reservations":
        combined = raw_bookings
    else:
        combined = sorted(raw_sessions + raw_bookings, key=lambda x: str(x["start_time"] or ""), reverse=True)

    # Status filter
    if selected_status and selected_status != "all":
        combined = [r for r in combined if r["status"].lower() == selected_status]

    # Search filter
    if search_query:
        q_lower = search_query.lower()
        combined = [
            r for r in combined
            if q_lower in (r["code"] or "").lower()
            or q_lower in (r["vehicle_number"] or "").lower()
            or q_lower in (r["slot_number"] or "").lower()
        ]

    conn.close()

    return render_template(
        "history.html",
        records=combined,
        active_tab=active_tab,
        selected_status=selected_status,
        search_query=search_query,
        is_admin_view=is_admin_view,
        sessions_count=len(raw_sessions),
        reservations_count=len(raw_bookings),
        total_filtered_count=len(combined),
        stats={
            "total": len(raw_sessions) + len(raw_bookings),
            "active": active_sessions_count + active_reservations_count,
            "cancelled": sum(1 for r in raw_bookings if r["status"] == "Cancelled"),
            "completed": completed_sessions_count,
            "active_sessions": active_sessions_count,
            "active_reservations": active_reservations_count,
            "completed_sessions": completed_sessions_count,
            "total_revenue": total_revenue
        }
    )


@app.route("/cancel-booking/<int:booking_id>", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    """
    Task 3.7: Implement booking cancellation
    Cancels reservation, frees the parking slot, and records status in history.
    """
    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ?",
        (booking_id,)
    ).fetchone()

    if not booking:
        conn.close()
        flash("Reservation not found.", "error")
        return redirect(url_for("history"))

    # Permission check: booking owner or system admin
    if booking["user_id"] != session["user_id"] and not session.get("is_admin"):
        conn.close()
        flash("You do not have permission to cancel this reservation.", "error")
        return redirect(url_for("history"))

    if booking["status"] != "Active":
        conn.close()
        flash(f"This reservation is already marked as {booking['status']}.", "error")
        return redirect(url_for("history"))

    # Update booking status to Cancelled
    conn.execute(
        "UPDATE bookings SET status = 'Cancelled' WHERE id = ?",
        (booking_id,)
    )

    # Release the parking slot back to Available
    slot = conn.execute(
        "SELECT slot_number FROM parking_slots WHERE id = ?",
        (booking["slot_id"],)
    ).fetchone()

    conn.execute("""
        UPDATE parking_slots
        SET status = 'Available', vehicle_number = NULL, booked_by = NULL
        WHERE id = ?
    """, (booking["slot_id"],))

    conn.commit()
    conn.close()

    slot_name = slot["slot_number"] if slot else f"Slot #{booking['slot_id']}"
    flash(
        f"Reservation {booking['booking_code']} for {slot_name} has been cancelled successfully. The slot is now Available.",
        "success"
    )
    return redirect(url_for("history"))


# ----------------------------------------------------
# ADMIN MANAGEMENT (Phases 2 & 6)
# ----------------------------------------------------

@app.route("/admin/slots", methods=["GET", "POST"])
@admin_required
def admin_slots():
    conn = get_db()
    if request.method == "POST":
        slot_number = request.form.get("slot_number", "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{2,10}", slot_number):
            flash("Use 2–10 letters, numbers, or hyphens for a slot ID.", "error")
        else:
            try:
                conn.execute(
                    "INSERT INTO parking_slots (slot_number) VALUES (?)",
                    (slot_number,)
                )
                conn.commit()
                flash(f"Parking slot '{slot_number}' added successfully.", "success")
            except sqlite3.IntegrityError:
                flash("That slot ID already exists.", "error")
        conn.close()
        return redirect(url_for("admin_slots"))

    slots = conn.execute("""
        SELECT s.id, s.slot_number, s.status, s.vehicle_number, s.booked_by,
               u.name AS booked_by_name, u.email AS booked_by_email
        FROM parking_slots s
        LEFT JOIN users u ON s.booked_by = u.id
        ORDER BY s.slot_number
    """).fetchall()
    conn.close()
    return render_template("adminslots.html", slots=slots)


@app.route("/admin/slot/<int:slot_id>/edit", methods=["POST"])
@admin_required
def edit_slot(slot_id):
    slot_number = request.form.get("slot_number", "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{2,10}", slot_number):
        flash("Use 2–10 letters, numbers, or hyphens for a slot ID.", "error")
    else:
        try:
            conn = get_db()
            conn.execute(
                "UPDATE parking_slots SET slot_number = ? WHERE id = ?",
                (slot_number, slot_id)
            )
            conn.commit()
            conn.close()
            flash("Slot updated successfully.", "success")
        except sqlite3.IntegrityError:
            flash("That slot ID already exists.", "error")
    return redirect(url_for("admin_slots"))


@app.route("/admin/slot/<int:slot_id>/release", methods=["POST"])
@admin_required
def release_slot(slot_id):
    conn = get_db()
    # Check if there is an active session on this slot
    active_ses = conn.execute(
        "SELECT id, entry_time, hourly_rate, pricing_rule_id, pricing_rule_name, user_id, vehicle_number FROM parking_sessions WHERE slot_id = ? AND status = 'Active'",
        (slot_id,)
    ).fetchone()

    if active_ses:
        exit_time = datetime.now()
        exit_str = exit_time.strftime("%Y-%m-%d %H:%M:%S")
        calc = calculate_parking_fee(active_ses["entry_time"], exit_time)
        conn.execute("""
            UPDATE parking_sessions
            SET status = 'Completed', exit_time = ?, duration_minutes = ?, billed_hours = ?, calculated_fee = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (exit_str, calc["duration_minutes"], calc["billed_hours"], calc["final_amount"], active_ses["id"]))

        bill_ref = f"BILL-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        conn.execute("""
            INSERT OR IGNORE INTO billing_records (
                bill_reference, session_id, user_id, vehicle_number, slot_id,
                pricing_rule_name, hourly_rate, entry_time, exit_time,
                duration_minutes, billed_hours, rounding_policy, subtotal, final_amount, payment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending Payment')
        """, (
            bill_ref, active_ses["id"], active_ses["user_id"], active_ses["vehicle_number"], slot_id,
            active_ses["pricing_rule_name"] or "Standard Rate", active_ses["hourly_rate"],
            active_ses["entry_time"], exit_str, calc["duration_minutes"], calc["billed_hours"],
            calc["rounding_policy"], calc["subtotal"], calc["final_amount"]
        ))

    # Also update any active booking record for this slot to Completed
    active_bk = conn.execute(
        "SELECT id, booking_code FROM bookings WHERE slot_id = ? AND status = 'Active'",
        (slot_id,)
    ).fetchone()

    if active_bk:
        conn.execute(
            "UPDATE bookings SET status = 'Completed' WHERE id = ?",
            (active_bk["id"],)
        )

    conn.execute(
        "UPDATE parking_slots SET status = 'Available', vehicle_number = NULL, booked_by = NULL WHERE id = ?",
        (slot_id,)
    )
    conn.commit()
    conn.close()
    flash("Slot has been released and any active session completed.", "success")
    return redirect(url_for("admin_slots"))


@app.route("/admin/slot/<int:slot_id>/delete", methods=["POST"])
@admin_required
def delete_slot(slot_id):
    conn = get_db()
    slot = conn.execute("SELECT status FROM parking_slots WHERE id = ?", (slot_id,)).fetchone()
    if slot and slot["status"] == "Occupied":
        flash("Cannot remove an occupied slot.", "error")
    else:
        conn.execute("DELETE FROM parking_slots WHERE id = ?", (slot_id,))
        conn.commit()
        flash("Slot removed successfully.", "success")
    conn.close()
    return redirect(url_for("admin_slots"))


# ----------------------------------------------------
# ADMIN PRICING CONFIGURATION (Phase 6)
# ----------------------------------------------------

@app.route("/pricing")
@app.route("/admin/pricing", methods=["GET", "POST"])
@login_required
def admin_pricing():
    """Task 6.2 & 6.5: View and configure pricing rules."""
    conn = get_db()
    if request.method == "POST":
        if not session.get("is_admin"):
            flash("Admin privileges required to modify pricing tariffs.", "error")
            conn.close()
            return redirect(url_for("admin_pricing"))

        rule_name = request.form.get("rule_name", "").strip()
        vehicle_type = request.form.get("vehicle_type", "All").strip()
        hourly_rate = request.form.get("hourly_rate", "30.0")
        minimum_charge = request.form.get("minimum_charge", "0.0")
        rounding_policy = request.form.get("rounding_policy", "ceiling_hour")

        if not rule_name:
            flash("Rule name cannot be empty.", "error")
            conn.close()
            return redirect(url_for("admin_pricing"))

        try:
            rate_val = float(hourly_rate)
            min_val = float(minimum_charge)
            if rate_val < 0 or min_val < 0:
                raise ValueError("Negative rates are not allowed.")
        except ValueError:
            flash("Please enter valid, non-negative numbers for rates.", "error")
            conn.close()
            return redirect(url_for("admin_pricing"))

        conn.execute("""
            INSERT INTO pricing_rules (rule_name, vehicle_type, hourly_rate, minimum_charge, billing_interval_minutes, rounding_policy, is_active)
            VALUES (?, ?, ?, ?, 60, ?, 1)
        """, (rule_name, vehicle_type, rate_val, min_val, rounding_policy))
        conn.commit()
        conn.close()
        flash(f"Pricing rule '{rule_name}' created successfully.", "success")
        return redirect(url_for("admin_pricing"))

    rules = conn.execute("SELECT * FROM pricing_rules ORDER BY is_active DESC, id ASC").fetchall()
    conn.close()
    return render_template("admin_pricing.html", rules=[dict(r) for r in rules])


@app.route("/admin/pricing/<int:rule_id>/edit", methods=["POST"])
@admin_required
def admin_pricing_edit(rule_id):
    """Task 6.5: Update hourly rate for a pricing rule."""
    hourly_rate = request.form.get("hourly_rate", "")
    try:
        rate_val = float(hourly_rate)
        if rate_val < 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Invalid rate provided.", "error")
        return redirect(url_for("admin_pricing"))

    conn = get_db()
    conn.execute(
        "UPDATE pricing_rules SET hourly_rate = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (rate_val, rule_id)
    )
    conn.commit()
    conn.close()
    flash("Pricing rule updated successfully.", "success")
    return redirect(url_for("admin_pricing"))


@app.route("/admin/pricing/<int:rule_id>/toggle", methods=["POST"])
@admin_required
def admin_pricing_toggle(rule_id):
    """Task 6.5: Toggle active/inactive status of a pricing rule."""
    conn = get_db()
    rule = conn.execute("SELECT is_active FROM pricing_rules WHERE id = ?", (rule_id,)).fetchone()
    if rule:
        new_status = 0 if rule["is_active"] else 1
        conn.execute(
            "UPDATE pricing_rules SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, rule_id)
        )
        conn.commit()
        flash("Rule status updated.", "success")
    conn.close()
    return redirect(url_for("admin_pricing"))




if __name__ == "__main__":
    init_db()
    app.run(debug=True)
