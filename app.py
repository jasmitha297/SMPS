import sqlite3
import re
import uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
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

    # Seamless Data Migration: link any pre-existing occupied slots with vehicle and booking records
    occupied_slots = conn.execute(
        "SELECT id, slot_number, vehicle_number, booked_by FROM parking_slots WHERE status = 'Occupied' AND booked_by IS NOT NULL"
    ).fetchall()

    for s in occupied_slots:
        if s["vehicle_number"] and s["booked_by"]:
            # Auto-ensure vehicle
            veh = conn.execute(
                "SELECT id FROM vehicles WHERE user_id = ? AND vehicle_number = ?",
                (s["booked_by"], s["vehicle_number"])
            ).fetchone()
            if not veh:
                conn.execute(
                    "INSERT OR IGNORE INTO vehicles (user_id, vehicle_number, vehicle_type, model, color) VALUES (?, ?, ?, ?, ?)",
                    (s["booked_by"], s["vehicle_number"], "Car", "Vehicle", "Silver")
                )
                veh = conn.execute(
                    "SELECT id FROM vehicles WHERE user_id = ? AND vehicle_number = ?",
                    (s["booked_by"], s["vehicle_number"])
                ).fetchone()
            veh_id = veh["id"] if veh else None

            # Auto-ensure active booking
            bk = conn.execute(
                "SELECT id FROM bookings WHERE slot_id = ? AND status = 'Active'",
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
        conn.close()
    return {
        "is_admin": is_admin,
        "active_bookings_count": active_count,
        "registered_vehicles_count": vehicles_count
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
               (SELECT COUNT(*) FROM bookings b WHERE b.vehicle_id = v.id AND b.status = 'Active') AS has_active_booking
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

    # Prevent deleting if vehicle currently holds an active booking
    active_booking = conn.execute(
        "SELECT id FROM bookings WHERE vehicle_id = ? AND status = 'Active'",
        (vehicle_id,)
    ).fetchone()

    if active_booking:
        conn.close()
        flash(
            f"Cannot delete vehicle {veh['vehicle_number']} because it is currently parked in an active reservation. Cancel the reservation first.",
            "error"
        )
        return redirect(url_for("vehicles"))

    conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    conn.commit()
    conn.close()

    flash(f"Vehicle {veh['vehicle_number']} removed successfully.", "success")
    return redirect(url_for("vehicles"))


# ----------------------------------------------------
# PARKING SLOTS & RESERVATION (Phase 2 & Phase 3)
# ----------------------------------------------------

@app.route("/parking-slots")
@login_required
def parking_slots():
    """
    Phase 2 & Phase 3:
    Task 3.1: Design the booking interface
    Displays all slots, user's registered vehicles, and slot reservation options.
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

    conn.close()

    # Retrieve user's registered vehicles for slot selection
    user_vehicles = get_user_vehicles(session["user_id"])

    return render_template(
        "parking_slots.html",
        slots=slots,
        stats=stats,
        user_vehicles=user_vehicles,
        user_active_slots=user_active_slots
    )


@app.route("/select-slot/<int:slot_id>", methods=["POST"])
@app.route("/book-slot/<int:slot_id>", methods=["POST"])
@login_required
def select_slot(slot_id):
    """
    Phase 3: Booking and Reservation
    Task 3.2: Implement reservation creation
    Task 3.3: Associate bookings with users, slots, and registered vehicles
    Task 3.4: Validate slot availability before booking
    Task 3.5: Prevent duplicate reservations
    Task 3.6: Update slot status after reservation
    """
    vehicle_id_raw = request.form.get("vehicle_id")
    duration_hours = request.form.get("duration", "2")

    try:
        duration_hours = int(duration_hours)
        if duration_hours <= 0:
            duration_hours = 2
    except ValueError:
        duration_hours = 2

    # Verify user has selected a registered vehicle
    if not vehicle_id_raw:
        flash("You must select a registered vehicle to book a slot. Please register a vehicle first if you haven't.", "error")
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

    # Task 3.5: Prevent duplicate reservations
    # A single vehicle cannot hold multiple active reservations simultaneously
    active_vehicle_booking = conn.execute("""
        SELECT b.id, b.booking_code, s.slot_number
        FROM bookings b
        JOIN parking_slots s ON b.slot_id = s.id
        WHERE b.vehicle_id = ? AND b.status = 'Active'
    """, (vehicle["id"],)).fetchone()

    if active_vehicle_booking:
        conn.close()
        flash(
            f"Vehicle '{vehicle['vehicle_number']}' already has an active reservation at slot {active_vehicle_booking['slot_number']} (Ref: {active_vehicle_booking['booking_code']}). Each vehicle can only hold one active reservation at a time.",
            "error"
        )
        return redirect(url_for("parking_slots"))

    # Task 3.4: Validate slot availability before booking
    slot = conn.execute(
        "SELECT * FROM parking_slots WHERE id = ?",
        (slot_id,)
    ).fetchone()

    if not slot or slot["status"] != "Available":
        conn.close()
        flash("Sorry, this slot is no longer available. Please select another slot.", "error")
        return redirect(url_for("parking_slots"))

    # Task 3.6: Update slot status atomically after reservation
    cursor = conn.execute("""
        UPDATE parking_slots
        SET status = 'Occupied', vehicle_number = ?, booked_by = ?
        WHERE id = ? AND status = 'Available'
    """, (vehicle["vehicle_number"], session["user_id"], slot_id))

    if cursor.rowcount == 0:
        conn.close()
        flash("Sorry, this slot was just taken by another user. Please choose another slot.", "error")
        return redirect(url_for("parking_slots"))

    # Task 3.2 & 3.3: Implement reservation creation and association
    booking_code = f"SPK-{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now()
    end_time = now + timedelta(hours=duration_hours)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        INSERT INTO bookings (
            booking_code, user_id, slot_id, vehicle_id, vehicle_number,
            booking_time, start_time, duration_hours, end_time, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active')
    """, (
        booking_code, session["user_id"], slot_id, vehicle["id"], vehicle["vehicle_number"],
        now_str, now_str, duration_hours, end_str
    ))

    conn.commit()
    conn.close()

    flash(
        f"Reservation successful! Slot {slot['slot_number']} reserved for {vehicle['vehicle_number']}. Ref: {booking_code}",
        "success"
    )
    return redirect(url_for("history"))


# ----------------------------------------------------
# BOOKING HISTORY & CANCELLATION (Phase 3)
# ----------------------------------------------------

@app.route("/history")
@login_required
def history():
    """
    Phase 3: Booking & Reservation History
    Displays all past and active reservations, metrics, and cancellation controls.
    """
    view_all = request.args.get("view") == "all" and session.get("is_admin")
    conn = get_db()

    if view_all:
        bookings_query = """
            SELECT b.*,
                   s.slot_number,
                   v.vehicle_type, v.model, v.color,
                   u.name AS user_name, u.email AS user_email
            FROM bookings b
            LEFT JOIN parking_slots s ON b.slot_id = s.id
            LEFT JOIN vehicles v ON b.vehicle_id = v.id
            LEFT JOIN users u ON b.user_id = u.id
            ORDER BY b.booking_time DESC
        """
        all_bookings = conn.execute(bookings_query).fetchall()
    else:
        bookings_query = """
            SELECT b.*,
                   s.slot_number,
                   v.vehicle_type, v.model, v.color,
                   u.name AS user_name, u.email AS user_email
            FROM bookings b
            LEFT JOIN parking_slots s ON b.slot_id = s.id
            LEFT JOIN vehicles v ON b.vehicle_id = v.id
            LEFT JOIN users u ON b.user_id = u.id
            WHERE b.user_id = ?
            ORDER BY b.booking_time DESC
        """
        all_bookings = conn.execute(bookings_query, (session["user_id"],)).fetchall()

    # Calculate statistics
    total_count = len(all_bookings)
    active_count = sum(1 for b in all_bookings if b["status"] == "Active")
    cancelled_count = sum(1 for b in all_bookings if b["status"] == "Cancelled")
    completed_count = sum(1 for b in all_bookings if b["status"] == "Completed")

    conn.close()

    return render_template(
        "history.html",
        bookings=all_bookings,
        is_admin_view=view_all,
        stats={
            "total": total_count,
            "active": active_count,
            "cancelled": cancelled_count,
            "completed": completed_count
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
# ADMIN MANAGEMENT (Phase 2 & Admin)
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
    flash("Slot has been released and active reservation marked completed.", "success")
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
