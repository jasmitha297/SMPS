
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-to-a-random-secret-before-deployment"

DATABASE = "parking.db"


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
    """)

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

    # Add an admin flag to existing user databases.
    columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    ]

    if "is_admin" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
        )

    # Add sample slots only when the table is empty.
    slot_count = conn.execute(
        "SELECT COUNT(*) FROM parking_slots"
    ).fetchone()[0]

    if slot_count == 0:
        sample_slots = [
            ("A01",), ("A02",), ("A03",), ("A04",),
            ("B01",), ("B02",), ("B03",), ("B04",),
            ("C01",), ("C02",), ("C03",), ("C04",)
        ]
        conn.executemany(
            "INSERT INTO parking_slots (slot_number) VALUES (?)",
            sample_slots
        )

    conn.commit()


    conn.commit()
    conn.close()


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
            flash("Please fill in every field.")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return render_template("register.html")

        hashed_password = generate_password_hash(password)
        conn = get_db()

        try:
            conn.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                (name, email, hashed_password)
            )
            conn.commit()
            flash("Registration successful! Please log in.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("An account with this email already exists.")
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
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.")

    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    user = conn.execute(
        "SELECT name, is_admin FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()
    conn.close()
    if user is None:
        session.clear()
        return redirect(url_for("login"))
    return render_template(
        "dashboard.html",
        name=user["name"],
        is_admin=user["is_admin"]
    )

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))

from flask import abort
import re

@app.route("/parking-slots")
@login_required
def parking_slots():
    conn = get_db()
    slots = conn.execute("""
        SELECT id, slot_number, status, vehicle_number
        FROM parking_slots
        ORDER BY slot_number
    """).fetchall()
    stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'Available' THEN 1 ELSE 0 END)
                AS available,
            SUM(CASE WHEN status = 'Occupied' THEN 1 ELSE 0 END)
                AS occupied
        FROM parking_slots
    """).fetchone()
    conn.close()
    return render_template(
        "parking_slots.html",
        slots=slots,
        stats=stats
    )

@app.route("/select-slot/<int:slot_id>", methods=["POST"])
@login_required
def select_slot(slot_id):
    vehicle_number = request.form.get("vehicle_number", "").strip().upper()
    if not vehicle_number:
        flash("Please enter a valid vehicle number.", "error")
        return redirect(url_for("parking_slots"))
    
    conn = get_db()
    # Updates status, vehicle number, and associates it with the current user's ID
    cursor = conn.execute(
        """
        UPDATE parking_slots
        SET status = 'Occupied', vehicle_number = ?, booked_by = ?
        WHERE id = ? AND status = 'Available'
        """,
        (vehicle_number, session["user_id"], slot_id)
    )
    conn.commit()
    conn.close()

    if cursor.rowcount == 0:
        flash("Sorry, that slot was just taken. Please choose another.", "error")
    else:
        flash("Parking slot successfully reserved!", "success")
    
    return redirect(url_for("parking_slots"))

@app.route("/admin/slots", methods=["GET", "POST"])
@admin_required
def admin_slots():
    conn = get_db()
    if request.method == "POST":
        slot_number = request.form.get(
            "slot_number", ""
        ).strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{2,10}", slot_number):
            flash("Use 2–10 letters, numbers, or hyphens for a slot ID.")
        else:
            try:
                conn.execute(
                    "INSERT INTO parking_slots (slot_number) VALUES (?)",
                    (slot_number,)
                )
                conn.commit()
                flash("Parking slot added successfully.")
            except sqlite3.IntegrityError:
                flash("That slot ID already exists.")
        conn.close()
        return redirect(url_for("admin_slots"))
    slots = conn.execute("""
        SELECT id, slot_number, status, vehicle_number
        FROM parking_slots
        ORDER BY slot_number
    """).fetchall()
    conn.close()
    return render_template("admin_slots.html", slots=slots)

@app.route("/admin/slot/<int:slot_id>/edit", methods=["POST"])
@admin_required
def edit_slot(slot_id):
    slot_number = request.form.get("slot_number", "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{2,10}", slot_number):
        flash("Use 2–10 letters, numbers, or hyphens for a slot ID.")
    else:
        try:
            conn = get_db()
            conn.execute(
                "UPDATE parking_slots SET slot_number = ? WHERE id = ?",
                (slot_number, slot_id)
            )
            conn.commit()
            conn.close()
            flash("Slot updated successfully.")
        except sqlite3.IntegrityError:
            flash("That slot ID already exists.")
    return redirect(url_for("admin_slots"))

@app.route("/admin/slot/<int:slot_id>/release", methods=["POST"])
@admin_required
def release_slot(slot_id):
    conn = get_db()
    conn.execute(
        "UPDATE parking_slots SET status = 'Available', vehicle_number = NULL, booked_by = NULL WHERE id = ?",
        (slot_id,)
    )
    conn.commit()
    conn.close()
    flash("Slot has been released.")
    return redirect(url_for("admin_slots"))

@app.route("/admin/slot/<int:slot_id>/delete", methods=["POST"])
@admin_required
def delete_slot(slot_id):
    conn = get_db()
    # Prevent deleting an occupied slot
    slot = conn.execute("SELECT status FROM parking_slots WHERE id = ?", (slot_id,)).fetchone()
    if slot and slot["status"] == "Occupied":
        flash("Cannot remove an occupied slot.")
    else:
        conn.execute("DELETE FROM parking_slots WHERE id = ?", (slot_id,))
        conn.commit()
        flash("Slot removed.")
    conn.close()
    return redirect(url_for("admin_slots"))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
