import unittest
import os
import sqlite3
import tempfile
from werkzeug.security import generate_password_hash

# Set a temporary database for test isolation
test_db_fd, test_db_path = tempfile.mkstemp(suffix=".db")
os.close(test_db_fd)

import app
app.DATABASE = test_db_path
app.app.config["TESTING"] = True
app.app.config["SECRET_KEY"] = "test-secret"
app.init_db()


class SMPSTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app.app.test_client()
        # Clean and re-init database
        conn = app.get_db()
        conn.execute("DELETE FROM payments")
        conn.execute("DELETE FROM billing_records")
        conn.execute("DELETE FROM parking_sessions")
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM vehicles")
        conn.execute("DELETE FROM parking_slots")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM pricing_rules")

        # Re-seed default pricing rules
        sample_pricing_rules = [
            (1, "Standard Four-Wheeler (Car)", "Car", 30.0, 30.0, 60, "ceiling_hour", 1),
            (2, "Two-Wheeler (Bike / Scooter)", "Two-Wheeler", 15.0, 15.0, 60, "ceiling_hour", 1),
            (3, "Heavy / SUV Parking", "SUV", 45.0, 45.0, 60, "ceiling_hour", 1),
            (4, "Electric Vehicle (EV)", "EV", 25.0, 25.0, 60, "ceiling_hour", 1),
            (5, "Default Universal Rate", "All", 30.0, 30.0, 60, "ceiling_hour", 1)
        ]
        conn.executemany("""
            INSERT INTO pricing_rules (id, rule_name, vehicle_type, hourly_rate, minimum_charge, billing_interval_minutes, rounding_policy, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, sample_pricing_rules)
        
        # Insert test users
        conn.execute(
            "INSERT INTO users (id, name, email, password, is_admin) VALUES (?, ?, ?, ?, ?)",
            (1, "Test User", "user@test.com", generate_password_hash("password123"), 0)
        )
        conn.execute(
            "INSERT INTO users (id, name, email, password, is_admin) VALUES (?, ?, ?, ?, ?)",
            (2, "Admin User", "admin@test.com", generate_password_hash("password123"), 1)
        )
        conn.execute(
            "INSERT INTO users (id, name, email, password, is_admin) VALUES (?, ?, ?, ?, ?)",
            (3, "Second User", "user2@test.com", generate_password_hash("password123"), 0)
        )
        
        # Insert sample slots
        conn.execute("INSERT INTO parking_slots (id, slot_number, status) VALUES (1, 'A01', 'Available')")
        conn.execute("INSERT INTO parking_slots (id, slot_number, status) VALUES (2, 'A02', 'Available')")
        conn.commit()
        conn.close()

    def login_user(self, email="user@test.com", password="password123"):
        return self.app.post("/login", data={"email": email, "password": password}, follow_redirects=True)

    def logout_user(self):
        return self.app.get("/logout", follow_redirects=True)

    # -------------------------------------------------------------------------
    # PHASE 4: VEHICLE MANAGEMENT TESTS (Tasks 4.1 - 4.7)
    # -------------------------------------------------------------------------

    def test_task_4_1_and_4_2_vehicle_table_and_interface(self):
        """Task 4.1 & 4.2: Verify vehicles table exists and registration interface renders"""
        conn = app.get_db()
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(vehicles)").fetchall()]
        conn.close()
        self.assertIn("user_id", columns)
        self.assertIn("vehicle_number", columns)
        self.assertIn("vehicle_type", columns)

        self.login_user()
        response = self.app.get("/vehicles")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Register New Vehicle", response.data)
        self.assertIn(b"Vehicle Registration Number", response.data)

    def test_task_4_3_validate_vehicle_number(self):
        """Task 4.3: Validate vehicle registration numbers with strict validation"""
        valid_plates = ["KA 01 AB 1234", "TN-01-EF-2345", "MH12DE4567", "DL 04 C 5678"]
        for plate in valid_plates:
            is_valid, sanitized, err = app.validate_vehicle_number(plate)
            self.assertTrue(is_valid, f"Expected {plate} to be valid, got: {err}")
            self.assertTrue(len(sanitized) > 0)

        invalid_plates = [
            "",                  # Empty
            "12345",             # Numbers only, no letters
            "ABCDEFG",           # Letters only, no numbers
            "AB",                # Too short (< 4 chars)
            "KA 01 @@@ 1234",    # Invalid special symbols
        ]
        for plate in invalid_plates:
            is_valid, _, err = app.validate_vehicle_number(plate)
            self.assertFalse(is_valid, f"Expected {plate} to be invalid")

    def test_task_4_4_to_4_6_store_and_retrieve_vehicle(self):
        """Task 4.4, 4.5, 4.6: Store vehicle details and retrieve saved vehicles associated with user"""
        self.login_user()
        # Register a valid vehicle
        response = self.app.post("/vehicles", data={
            "vehicle_number": "KA 01 AB 1234",
            "vehicle_type": "Car",
            "model": "Honda City",
            "color": "White"
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"KA 01 AB 1234", response.data)
        self.assertIn(b"Honda City", response.data)

        # Retrieve vehicles using get_user_vehicles
        user_vehicles = app.get_user_vehicles(1)
        self.assertEqual(len(user_vehicles), 1)
        self.assertEqual(user_vehicles[0]["vehicle_number"], "KA 01 AB 1234")
        self.assertEqual(user_vehicles[0]["model"], "Honda City")

        # Test duplicate registration for the same user is rejected
        dup_resp = self.app.post("/vehicles", data={
            "vehicle_number": "KA 01 AB 1234",
            "vehicle_type": "Car"
        }, follow_redirects=True)
        self.assertIn(b"already registered in your account", dup_resp.data)

    def test_vehicle_deletion_protection(self):
        """Verify vehicle deletion works and prevents deletion when vehicle is actively parked"""
        self.login_user()
        self.app.post("/vehicles", data={
            "vehicle_number": "MH 02 BB 9999",
            "vehicle_type": "SUV"
        }, follow_redirects=True)

        user_vehicles = app.get_user_vehicles(1)
        veh_id = user_vehicles[0]["id"]

        # Book slot 1 with this vehicle
        self.app.post(f"/select-slot/1", data={"vehicle_id": veh_id, "duration": "2"}, follow_redirects=True)

        # Attempt to delete while actively booked -> must fail
        del_resp = self.app.post(f"/vehicles/delete/{veh_id}", follow_redirects=True)
        self.assertIn(b"Cannot delete vehicle", del_resp.data)

        # Cancel the booking
        conn = app.get_db()
        booking = conn.execute("SELECT id FROM bookings WHERE vehicle_id = ? AND status = 'Active'", (veh_id,)).fetchone()
        conn.close()
        self.app.post(f"/cancel-booking/{booking['id']}", follow_redirects=True)

        # Now deletion must succeed
        del_resp2 = self.app.post(f"/vehicles/delete/{veh_id}", follow_redirects=True)
        self.assertIn(b"removed successfully", del_resp2.data)
        self.assertEqual(len(app.get_user_vehicles(1)), 0)

    # -------------------------------------------------------------------------
    # PHASE 3: BOOKING AND RESERVATION TESTS (Tasks 3.1 - 3.8)
    # -------------------------------------------------------------------------

    def test_task_3_1_booking_interface_requires_vehicle(self):
        """Task 3.1: Interface prompts user to register vehicle before booking"""
        self.login_user()
        # When no vehicle registered, parking-slots page shows requirement banner
        response = self.app.get("/parking-slots")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Vehicle Registration Required", response.data)

    def test_task_3_2_and_3_3_create_reservation_and_associate(self):
        """Task 3.2 & 3.3: Implement reservation creation and associate with user, slot, vehicle"""
        self.login_user()
        # 1. Register vehicle first
        self.app.post("/vehicles", data={
            "vehicle_number": "DL 01 AA 1111",
            "vehicle_type": "Car",
            "model": "Hyundai i20"
        }, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        # 2. Book slot 1
        response = self.app.post("/select-slot/1", data={
            "vehicle_id": veh["id"],
            "duration": "4"
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reservation successful", response.data)

        # Verify booking in database
        conn = app.get_db()
        booking = conn.execute("SELECT * FROM bookings WHERE slot_id = 1").fetchone()
        self.assertIsNotNone(booking)
        self.assertEqual(booking["user_id"], 1)
        self.assertEqual(booking["vehicle_id"], veh["id"])
        self.assertEqual(booking["vehicle_number"], "DL 01 AA 1111")
        self.assertEqual(booking["duration_hours"], 4)
        self.assertEqual(booking["status"], "Active")
        self.assertTrue(booking["booking_code"].startswith("SPK-"))
        conn.close()

    def test_task_3_4_validate_slot_availability(self):
        """Task 3.4: Validate slot availability before booking"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KA 02 C 2222", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        # First reservation on slot 1
        self.app.post("/select-slot/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        # Attempt to book slot 1 again (slot is already Occupied)
        # Register a second vehicle so duplicate vehicle check doesn't trigger first
        self.app.post("/vehicles", data={"vehicle_number": "KA 03 D 3333", "vehicle_type": "Car"}, follow_redirects=True)
        veh2 = app.get_user_vehicles(1)[0]

        response = self.app.post("/select-slot/1", data={"vehicle_id": veh2["id"]}, follow_redirects=True)
        self.assertIn(b"no longer available", response.data)

    def test_task_3_5_prevent_duplicate_reservations(self):
        """Task 3.5: Prevent duplicate reservations for the same vehicle across different slots"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "TN 09 XY 5555", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        # Book slot 1 with vehicle
        self.app.post("/select-slot/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        # Attempt to book slot 2 with the SAME vehicle -> Must be prevented
        response = self.app.post("/select-slot/2", data={"vehicle_id": veh["id"]}, follow_redirects=True)
        self.assertIn(b"already has an active reservation", response.data)

    def test_task_3_6_update_slot_status_after_reservation(self):
        """Task 3.6: Update slot status to Occupied with vehicle number and user id"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "HR 26 DK 8337", "vehicle_type": "SUV"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        self.app.post("/select-slot/2", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        slot = conn.execute("SELECT * FROM parking_slots WHERE id = 2").fetchone()
        conn.close()
        self.assertEqual(slot["status"], "Occupied")
        self.assertEqual(slot["vehicle_number"], "HR 26 DK 8337")
        self.assertEqual(slot["booked_by"], 1)

    def test_task_3_7_booking_cancellation(self):
        """Task 3.7: Implement booking cancellation - updates status and releases slot"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KL 07 CA 9000", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        self.app.post("/select-slot/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        booking = conn.execute("SELECT * FROM bookings WHERE slot_id = 1 AND status = 'Active'").fetchone()
        conn.close()

        # Cancel the booking
        cancel_resp = self.app.post(f"/cancel-booking/{booking['id']}", follow_redirects=True)
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertIn(b"has been cancelled successfully", cancel_resp.data)

        # Verify booking status in database is Cancelled
        conn = app.get_db()
        updated_booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking["id"],)).fetchone()
        self.assertEqual(updated_booking["status"], "Cancelled")

        # Verify parking slot is back to Available
        slot = conn.execute("SELECT * FROM parking_slots WHERE id = 1").fetchone()
        self.assertEqual(slot["status"], "Available")
        self.assertIsNone(slot["vehicle_number"])
        self.assertIsNone(slot["booked_by"])
        conn.close()

    def test_history_page_display_and_admin_view(self):
        """Verify history page displays reservations and admin can view all system reservations"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "MH 04 ER 7777", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/select-slot/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        # Normal user visits history
        hist_resp = self.app.get("/history")
        self.assertEqual(hist_resp.status_code, 200)
        self.assertIn(b"Reservation History", hist_resp.data)
        self.assertIn(b"MH 04 ER 7777", hist_resp.data)
        self.assertIn(b"Cancel Reservation", hist_resp.data)

        # Admin user visits history with all view
        self.logout_user()
        self.login_user(email="admin@test.com")
        admin_hist_resp = self.app.get("/history?view=all")
        self.assertEqual(admin_hist_resp.status_code, 200)
        self.assertIn(b"All System Reservations", admin_hist_resp.data)
        self.assertIn(b"MH 04 ER 7777", admin_hist_resp.data)

    # -------------------------------------------------------------------------
    # PHASE 5: PARKING SESSION MANAGEMENT TESTS (Tasks 5.1 - 5.9)
    # -------------------------------------------------------------------------

    def test_task_5_1_parking_sessions_table_structure(self):
        """Task 5.1: Verify parking_sessions table exists with all required columns"""
        conn = app.get_db()
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(parking_sessions)").fetchall()]
        conn.close()
        required_cols = [
            "id", "session_code", "user_id", "vehicle_id", "vehicle_number",
            "slot_id", "entry_time", "exit_time", "duration_minutes",
            "billed_hours", "hourly_rate", "calculated_fee", "status"
        ]
        for col in required_cols:
            self.assertIn(col, columns, f"Missing column {col} in parking_sessions")

    def test_task_5_2_and_5_3_start_parking_session_and_record_entry(self):
        """Task 5.2 & 5.3: Associate registered vehicle with slot, start session, and record server entry timestamp"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KA 05 MJ 2026", "vehicle_type": "Car", "model": "Civic"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        # Park Now on slot 1
        resp = self.app.post("/select-slot/1", data={
            "vehicle_id": veh["id"],
            "action_type": "park_now"
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Active Parking", resp.data)
        self.assertIn(b"KA 05 MJ 2026", resp.data)

        conn = app.get_db()
        ses = conn.execute("SELECT * FROM parking_sessions WHERE slot_id = 1 AND status = 'Active'").fetchone()
        slot = conn.execute("SELECT * FROM parking_slots WHERE id = 1").fetchone()
        conn.close()

        self.assertIsNotNone(ses)
        self.assertEqual(ses["user_id"], 1)
        self.assertEqual(ses["vehicle_id"], veh["id"])
        self.assertEqual(ses["vehicle_number"], "KA 05 MJ 2026")
        self.assertIsNotNone(ses["entry_time"])
        self.assertIsNone(ses["exit_time"])
        self.assertEqual(ses["status"], "Active")
        self.assertEqual(slot["status"], "Occupied")

    def test_task_5_2_check_in_from_existing_reservation(self):
        """Task 5.2 & 5.3: Distinguish advance reservation from actual parking session start"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "TN 10 AB 4444", "vehicle_type": "SUV"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]

        # 1. Create advance reservation (not immediate park_now)
        self.app.post("/select-slot/1", data={"vehicle_id": veh["id"], "action_type": "reserve"}, follow_redirects=True)

        conn = app.get_db()
        # No active parking session should exist yet
        ses_before = conn.execute("SELECT * FROM parking_sessions WHERE slot_id = 1").fetchone()
        booking = conn.execute("SELECT * FROM bookings WHERE slot_id = 1 AND status = 'Active'").fetchone()
        conn.close()
        self.assertIsNone(ses_before)
        self.assertIsNotNone(booking)

        # 2. Check in to start actual parking session
        checkin_resp = self.app.post(f"/start-session/{booking['id']}", follow_redirects=True)
        self.assertEqual(checkin_resp.status_code, 200)
        self.assertIn(b"Check-in confirmed", checkin_resp.data)

        conn = app.get_db()
        ses_after = conn.execute("SELECT * FROM parking_sessions WHERE booking_id = ? AND status = 'Active'", (booking["id"],)).fetchone()
        conn.close()
        self.assertIsNotNone(ses_after)
        self.assertEqual(ses_after["hourly_rate"], 45.0)  # SUV rate

    def test_task_5_4_live_timer_and_session_status_api(self):
        """Task 5.4: Verify live timer page renders with entry timestamp and status API works"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "DL 08 CC 1010", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        # Check active-session page
        page_resp = self.app.get("/active-session")
        self.assertEqual(page_resp.status_code, 200)
        self.assertIn(b'id="live-timer"', page_resp.data)
        self.assertIn(b'data-entry="', page_resp.data)

        # Check JSON status API
        api_resp = self.app.get("/api/session-status")
        self.assertEqual(api_resp.status_code, 200)
        data = api_resp.get_json()
        self.assertTrue(data["active"])
        self.assertEqual(data["slot_number"], "A01")
        self.assertEqual(data["vehicle_number"], "DL 08 CC 1010")
        self.assertEqual(data["hourly_rate"], 30.0)
        self.assertGreaterEqual(data["elapsed_seconds"], 0)

    def test_task_5_5_5_6_5_7_checkout_duration_and_release_slot(self):
        """Task 5.5, 5.6, 5.7: End session, record exit time, calculate duration, release slot, and prevent duplicate checkout"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KA 01 ZZ 8888", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        # Simulate that entry was 90 minutes ago (1 hr 30 mins -> 2 billed hours)
        ninety_mins_ago = (app.datetime.now() - app.timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE parking_sessions SET entry_time = ? WHERE slot_id = 1", (ninety_mins_ago,))
        conn.commit()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1 AND status = 'Active'").fetchone()
        conn.close()

        # Check out
        checkout_resp = self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)
        self.assertEqual(checkout_resp.status_code, 200)
        self.assertIn(b"Billing Summary", checkout_resp.data)
        self.assertIn(b"60.00", checkout_resp.data)  # 2 hours * 30.00 = 60.00

        conn = app.get_db()
        updated_ses = conn.execute("SELECT * FROM parking_sessions WHERE id = ?", (ses["id"],)).fetchone()
        slot = conn.execute("SELECT * FROM parking_slots WHERE id = 1").fetchone()
        bill = conn.execute("SELECT * FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()

        self.assertEqual(updated_ses["status"], "Completed")
        self.assertIsNotNone(updated_ses["exit_time"])
        self.assertGreaterEqual(updated_ses["duration_minutes"], 90)
        self.assertEqual(updated_ses["billed_hours"], 2)
        self.assertEqual(updated_ses["calculated_fee"], 60.0)

        # Slot must be released to Available
        self.assertEqual(slot["status"], "Available")
        self.assertIsNone(slot["vehicle_number"])
        self.assertIsNone(slot["booked_by"])

        # Bill must be persisted
        self.assertIsNotNone(bill)
        self.assertEqual(bill["final_amount"], 60.0)
        self.assertEqual(bill["payment_status"], "Pending Payment")

        # Duplicate checkout request must not create duplicate bill or error
        dup_resp = self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)
        self.assertEqual(dup_resp.status_code, 200)
        self.assertIn(b"already been completed", dup_resp.data)

    def test_task_5_9_unauthorized_access_protection(self):
        """Task 5.9: Prevent another user from ending session or viewing bill of User 1"""
        self.login_user(email="user@test.com")
        self.app.post("/vehicles", data={"vehicle_number": "MH 01 AB 1234", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()

        # Log in as Second User (id=3)
        self.logout_user()
        self.login_user(email="user2@test.com")

        # Attempt to end User 1's session -> must be denied
        unauth_end = self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)
        self.assertIn(b"do not have permission", unauth_end.data)

        # Verify session is still Active
        conn = app.get_db()
        still_active = conn.execute("SELECT status FROM parking_sessions WHERE id = ?", (ses["id"],)).fetchone()
        conn.close()
        self.assertEqual(still_active["status"], "Active")

        # User 1 ends session
        self.logout_user()
        self.login_user(email="user@test.com")
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        # User 2 attempts to view User 1's bill -> must be denied
        self.logout_user()
        self.login_user(email="user2@test.com")
        unauth_bill = self.app.get(f"/bill/{ses['id']}", follow_redirects=True)
        self.assertIn(b"Unauthorized access", unauth_bill.data)

    # -------------------------------------------------------------------------
    # PHASE 6: BILLING AND PRICING TESTS (Tasks 6.1 - 6.8)
    # -------------------------------------------------------------------------

    def test_task_6_2_and_6_5_pricing_configuration_and_admin_management(self):
        """Task 6.2 & 6.5: Verify pricing rules table and admin pricing management"""
        self.login_user(email="admin@test.com")
        resp = self.app.get("/admin/pricing")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Manage Pricing", resp.data)
        self.assertIn(b"Standard Four-Wheeler (Car)", resp.data)

        # Add a new custom rule
        add_resp = self.app.post("/admin/pricing", data={
            "rule_name": "VIP Van Tariff",
            "vehicle_type": "Van",
            "hourly_rate": "50.0",
            "minimum_charge": "50.0",
            "rounding_policy": "ceiling_hour"
        }, follow_redirects=True)
        self.assertIn(b"created successfully", add_resp.data)

        rule = app.get_active_pricing_rule("Van")
        self.assertEqual(rule["hourly_rate"], 50.0)

        # Edit existing rule rate (Car rule id=1 from 30 to 40)
        edit_resp = self.app.post("/admin/pricing/1/edit", data={"hourly_rate": "40.0"}, follow_redirects=True)
        self.assertIn(b"updated successfully", edit_resp.data)
        car_rule = app.get_active_pricing_rule("Car")
        self.assertEqual(car_rule["hourly_rate"], 40.0)

        # Deactivate rule 1 -> should fall back to 'All' (30.0)
        self.app.post("/admin/pricing/1/toggle", follow_redirects=True)
        fallback_rule = app.get_active_pricing_rule("Car")
        self.assertEqual(fallback_rule["hourly_rate"], 30.0)

    def test_task_6_4_and_6_8_billing_calculations_all_scenarios(self):
        """Task 6.4 & 6.8: Test billing calculations across <1hr, 1hr, multi-hour, midnight, multi-day, and categories"""
        car_rule = {"id": 1, "rule_name": "Car Rate", "hourly_rate": 30.0, "minimum_charge": 30.0, "rounding_policy": "ceiling_hour"}

        # 1. Less than 1 hour (25 minutes) -> 1 billed hour = ₹30.00
        c1 = app.calculate_parking_fee("2026-10-12 10:00:00", "2026-10-12 10:25:00", pricing_rule=car_rule)
        self.assertEqual(c1["duration_minutes"], 25)
        self.assertEqual(c1["billed_hours"], 1)
        self.assertEqual(c1["final_amount"], 30.0)

        # 2. Exactly 1 hour (60 minutes) -> 1 billed hour = ₹30.00
        c2 = app.calculate_parking_fee("2026-10-12 10:00:00", "2026-10-12 11:00:00", pricing_rule=car_rule)
        self.assertEqual(c2["duration_minutes"], 60)
        self.assertEqual(c2["billed_hours"], 1)
        self.assertEqual(c2["final_amount"], 30.0)

        # 3. Partial hour over 1 hour (1 hr 10 mins = 70 mins) -> 2 billed hours = ₹60.00
        c3 = app.calculate_parking_fee("2026-10-12 10:00:00", "2026-10-12 11:10:00", pricing_rule=car_rule)
        self.assertEqual(c3["duration_minutes"], 70)
        self.assertEqual(c3["billed_hours"], 2)
        self.assertEqual(c3["final_amount"], 60.0)

        # 4. Multiple exact hours (3 hours = 180 mins) -> 3 billed hours = ₹90.00
        c4 = app.calculate_parking_fee("2026-10-12 10:00:00", "2026-10-12 13:00:00", pricing_rule=car_rule)
        self.assertEqual(c4["duration_minutes"], 180)
        self.assertEqual(c4["billed_hours"], 3)
        self.assertEqual(c4["final_amount"], 90.0)

        # 5. Session spanning midnight (23:00 to 01:15 next day = 135 mins) -> 3 billed hours = ₹90.00
        c5 = app.calculate_parking_fee("2026-10-12 23:00:00", "2026-10-13 01:15:00", pricing_rule=car_rule)
        self.assertEqual(c5["duration_minutes"], 135)
        self.assertEqual(c5["billed_hours"], 3)
        self.assertEqual(c5["final_amount"], 90.0)

        # 6. Session spanning multiple days (48 hours = 2880 mins) -> 48 billed hours = ₹1440.00
        c6 = app.calculate_parking_fee("2026-10-10 09:00:00", "2026-10-12 09:00:00", pricing_rule=car_rule)
        self.assertEqual(c6["duration_minutes"], 2880)
        self.assertEqual(c6["billed_hours"], 48)
        self.assertEqual(c6["final_amount"], 1440.0)

        # 7. Two-wheeler category (₹15/hr for 2 hours = ₹30.00)
        c7 = app.calculate_parking_fee("2026-10-12 10:00:00", "2026-10-12 12:00:00", vehicle_type="Two-Wheeler")
        self.assertEqual(c7["hourly_rate"], 15.0)
        self.assertEqual(c7["final_amount"], 30.0)

    def test_task_6_5_6_6_6_7_historical_bill_immutability_and_payment_simulation(self):
        """Task 6.5, 6.6, 6.7: Ensure rate changes do not alter completed bills and verify bill display before payment"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KA 09 EE 9090", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()

        # End session at ₹30/hr
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        # Admin changes Car rate to ₹100/hr
        self.logout_user()
        self.login_user(email="admin@test.com")
        self.app.post("/admin/pricing/1/edit", data={"hourly_rate": "100.0"}, follow_redirects=True)

        # User revisits bill -> must still be ₹30.00!
        self.logout_user()
        self.login_user()
        bill_page = self.app.get(f"/bill/{ses['id']}")
        self.assertEqual(bill_page.status_code, 200)
        self.assertIn(b"30.00", bill_page.data)
        self.assertIn(b"Pending Payment", bill_page.data)

        # Simulate payment
        conn = app.get_db()
        bill = conn.execute("SELECT id FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()

        pay_resp = self.app.post(f"/pay-bill/{bill['id']}", follow_redirects=True)
        self.assertEqual(pay_resp.status_code, 200)
        self.assertIn(b"Paid", pay_resp.data)

    # -------------------------------------------------------------------------
    # PHASE 7: PAYMENT MANAGEMENT TESTS (Tasks 7.1 - 7.8)
    # -------------------------------------------------------------------------

    def test_task_7_1_and_7_2_payments_table_and_interface(self):
        """Task 7.1 & 7.2: Verify payments table schema and payment interface rendering"""
        conn = app.get_db()
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(payments)").fetchall()]
        conn.close()
        required_cols = [
            "id", "payment_reference", "bill_id", "user_id", "amount",
            "payment_method", "transaction_id", "payment_status", "failure_reason", "payment_date"
        ]
        for col in required_cols:
            self.assertIn(col, columns, f"Missing column {col} in payments table")

        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KA 01 PM 7001", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        conn = app.get_db()
        bill = conn.execute("SELECT id FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()

        # Visit payment interface (Task 7.1)
        resp = self.app.get(f"/pay/{bill['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Payment", resp.data)
        self.assertIn(b"Choose Payment Method", resp.data)
        self.assertIn(b"Credit / Debit Card", resp.data)
        self.assertIn(b"UPI", resp.data)
        self.assertIn(b"SIMULATION SANDBOX", resp.data)

    def test_task_7_3_to_7_6_payment_success_processing_and_status_update(self):
        """Task 7.3, 7.4, 7.5, 7.6: Process successful payment, store transaction, and update bill status to Paid"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "MH 12 PAY 7777", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        conn = app.get_db()
        bill = conn.execute("SELECT * FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()
        self.assertEqual(bill["payment_status"], "Pending Payment")

        # Process successful card payment
        pay_resp = self.app.post(f"/pay/{bill['id']}", data={
            "payment_method": "Credit / Debit Card",
            "simulation_mode": "success",
            "card_number": "4111 2222 3333 4444"
        }, follow_redirects=True)

        self.assertEqual(pay_resp.status_code, 200)
        self.assertIn(b"Payment Successful", pay_resp.data)
        self.assertIn(b"Payment Receipt", pay_resp.data)

        # Check transaction details stored in database (Task 7.5)
        conn = app.get_db()
        payment = conn.execute("SELECT * FROM payments WHERE bill_id = ?", (bill["id"],)).fetchone()
        updated_bill = conn.execute("SELECT payment_status FROM billing_records WHERE id = ?", (bill["id"],)).fetchone()
        conn.close()

        self.assertIsNotNone(payment)
        self.assertEqual(payment["user_id"], 1)
        self.assertEqual(payment["amount"], bill["final_amount"])
        self.assertEqual(payment["payment_method"], "Credit / Debit Card")
        self.assertEqual(payment["payment_status"], "Success")
        self.assertTrue(payment["transaction_id"].startswith("TXN-"))
        self.assertTrue(payment["payment_reference"].startswith("PAY-"))

        # Bill status must be updated to Paid (Task 7.6)
        self.assertEqual(updated_bill["payment_status"], "Paid")

    def test_task_7_4_payment_failure_simulation_and_retry(self):
        """Task 7.4: Process simulated payment failure state, store failure reason, and allow retry"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "DL 03 FL 1234", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        conn = app.get_db()
        bill = conn.execute("SELECT id FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()

        # Submit simulated failure
        fail_resp = self.app.post(f"/pay/{bill['id']}", data={
            "payment_method": "UPI / QR Code",
            "simulation_mode": "failure",
            "failure_reason": "Insufficient account balance"
        }, follow_redirects=True)

        self.assertEqual(fail_resp.status_code, 200)
        self.assertIn(b"Payment Failed", fail_resp.data)
        self.assertIn(b"Insufficient account balance", fail_resp.data)

        # Verify failure recorded in payments table
        conn = app.get_db()
        failed_payment = conn.execute("SELECT * FROM payments WHERE bill_id = ?", (bill["id"],)).fetchone()
        bill_check = conn.execute("SELECT payment_status FROM billing_records WHERE id = ?", (bill["id"],)).fetchone()
        conn.close()

        self.assertIsNotNone(failed_payment)
        self.assertEqual(failed_payment["payment_status"], "Failed")
        self.assertEqual(failed_payment["failure_reason"], "Insufficient account balance")
        # Bill remains Pending Payment
        self.assertEqual(bill_check["payment_status"], "Pending Payment")

        # Retry and succeed
        success_resp = self.app.post(f"/pay/{bill['id']}", data={
            "payment_method": "UPI / QR Code",
            "simulation_mode": "success"
        }, follow_redirects=True)
        self.assertEqual(success_resp.status_code, 200)
        self.assertIn(b"Payment Successful", success_resp.data)

        conn = app.get_db()
        bill_final = conn.execute("SELECT payment_status FROM billing_records WHERE id = ?", (bill["id"],)).fetchone()
        conn.close()
        self.assertEqual(bill_final["payment_status"], "Paid")

    def test_task_7_6_prevent_double_payment(self):
        """Task 7.6: Prevent paying a bill that is already paid"""
        self.login_user()
        self.app.post("/vehicles", data={"vehicle_number": "KA 04 DP 9999", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        conn = app.get_db()
        bill = conn.execute("SELECT id FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()

        # First payment succeeds
        self.app.post(f"/pay/{bill['id']}", data={"simulation_mode": "success"}, follow_redirects=True)

        # Second attempt to pay same bill
        dup_pay = self.app.post(f"/pay/{bill['id']}", data={"simulation_mode": "success"}, follow_redirects=True)
        self.assertIn(b"already been paid", dup_pay.data)

        conn = app.get_db()
        pay_count = conn.execute("SELECT COUNT(*) FROM payments WHERE bill_id = ? AND payment_status = 'Success'", (bill["id"],)).fetchone()[0]
        conn.close()
        self.assertEqual(pay_count, 1)

    def test_task_7_7_payment_receipt_and_unauthorized_protection(self):
        """Task 7.7: Payment confirmation receipt display and unauthorized access protection"""
        self.login_user(email="user@test.com")
        self.app.post("/vehicles", data={"vehicle_number": "TN 01 RC 5555", "vehicle_type": "Car"}, follow_redirects=True)
        veh = app.get_user_vehicles(1)[0]
        self.app.post("/park-now/1", data={"vehicle_id": veh["id"]}, follow_redirects=True)

        conn = app.get_db()
        ses = conn.execute("SELECT id FROM parking_sessions WHERE slot_id = 1").fetchone()
        conn.close()
        self.app.post(f"/end-session/{ses['id']}", follow_redirects=True)

        conn = app.get_db()
        bill = conn.execute("SELECT id FROM billing_records WHERE session_id = ?", (ses["id"],)).fetchone()
        conn.close()

        pay_resp = self.app.post(f"/pay/{bill['id']}", data={"payment_method": "Digital Wallet", "simulation_mode": "success"}, follow_redirects=True)
        self.assertEqual(pay_resp.status_code, 200)

        conn = app.get_db()
        payment = conn.execute("SELECT id FROM payments WHERE bill_id = ?", (bill["id"],)).fetchone()
        conn.close()

        # View receipt as owner (User 1)
        receipt_resp = self.app.get(f"/payment/receipt/{payment['id']}")
        self.assertEqual(receipt_resp.status_code, 200)
        self.assertIn(b"Payment Receipt", receipt_resp.data)
        self.assertIn(b"TN 01 RC 5555", receipt_resp.data)
        self.assertIn(b"Digital Wallet", receipt_resp.data)

        # Switch to User 2 (unauthorized) -> must be denied
        self.logout_user()
        self.login_user(email="user2@test.com")
        unauth_resp = self.app.get(f"/payment/receipt/{payment['id']}", follow_redirects=True)
        self.assertIn(b"Unauthorized access", unauth_resp.data)

        # Switch to Admin -> must be allowed
        self.logout_user()
        self.login_user(email="admin@test.com")
        admin_resp = self.app.get(f"/payment/receipt/{payment['id']}")
        self.assertEqual(admin_resp.status_code, 200)
        self.assertIn(b"Payment Receipt", admin_resp.data)

    # -------------------------------------------------------------------------
    # PHASE 8: ADMIN DASHBOARD AND REPORTS TESTS (Tasks 8.1 - 8.8)
    # -------------------------------------------------------------------------

    def test_task_8_1_and_8_2_admin_dashboard_metrics_and_access(self):
        """Task 8.1 & 8.2: Verify Admin Dashboard UI, access control, and slot metrics (total, available, occupied)"""
        # Normal user cannot access Admin Dashboard
        self.login_user(email="user@test.com")
        denied_resp = self.app.get("/admin/dashboard", follow_redirects=True)
        self.assertIn(b"Admin access is required", denied_resp.data)

        # Admin user access
        self.logout_user()
        self.login_user(email="admin@test.com")
        dash_resp = self.app.get("/admin/dashboard")
        self.assertEqual(dash_resp.status_code, 200)
        self.assertIn(b"Admin", dash_resp.data)
        self.assertIn(b"Total Parking Slots", dash_resp.data)
        self.assertIn(b"Available Slots", dash_resp.data)
        self.assertIn(b"Occupied Slots", dash_resp.data)
        self.assertIn(b"Occupancy Rate", dash_resp.data)

        # Also verify /admin redirect/route works
        admin_root = self.app.get("/admin")
        self.assertEqual(admin_root.status_code, 200)

    def test_task_8_3_8_4_8_5_admin_slot_management_crud(self):
        """Task 8.3, 8.4, 8.5: Admin add, edit, and remove parking slots"""
        self.login_user(email="admin@test.com")

        # Task 8.3: Add a new slot
        add_resp = self.app.post("/admin/slots", data={"slot_number": "D01"}, follow_redirects=True)
        self.assertIn(b"added successfully", add_resp.data)

        conn = app.get_db()
        d01 = conn.execute("SELECT * FROM parking_slots WHERE slot_number = 'D01'").fetchone()
        conn.close()
        self.assertIsNotNone(d01)
        self.assertEqual(d01["status"], "Available")

        # Task 8.4: Edit slot details (rename D01 to D02)
        edit_resp = self.app.post(f"/admin/slot/{d01['id']}/edit", data={"slot_number": "D02"}, follow_redirects=True)
        self.assertIn(b"Slot updated successfully", edit_resp.data)

        conn = app.get_db()
        d02 = conn.execute("SELECT * FROM parking_slots WHERE id = ?", (d01["id"],)).fetchone()
        conn.close()
        self.assertEqual(d02["slot_number"], "D02")

        # Task 8.5: Remove parking slot
        del_resp = self.app.post(f"/admin/slot/{d01['id']}/delete", follow_redirects=True)
        self.assertIn(b"Slot removed successfully", del_resp.data)

        conn = app.get_db()
        deleted = conn.execute("SELECT * FROM parking_slots WHERE id = ?", (d01["id"],)).fetchone()
        conn.close()
        self.assertIsNone(deleted)

    def test_task_8_6_admin_user_management(self):
        """Task 8.6: Display registered users, search users, and toggle admin roles safely"""
        self.login_user(email="admin@test.com")

        # Display users
        users_resp = self.app.get("/admin/users")
        self.assertEqual(users_resp.status_code, 200)
        self.assertIn(b"Users & Permissions", users_resp.data)
        self.assertIn(b"All Registered Users", users_resp.data)
        self.assertIn(b"Test User", users_resp.data)
        self.assertIn(b"user@test.com", users_resp.data)

        # Search query filter
        search_resp = self.app.get("/admin/users?q=Second")
        self.assertIn(b"Second User", search_resp.data)

        # Promote User 1 (id=1) to Admin
        toggle_resp = self.app.post("/admin/user/1/toggle-admin", follow_redirects=True)
        self.assertIn(b"promoted to Administrator", toggle_resp.data)

        conn = app.get_db()
        u1 = conn.execute("SELECT is_admin FROM users WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(u1["is_admin"], 1)

        # Demote User 1 back to standard user
        demote_resp = self.app.post("/admin/user/1/toggle-admin", follow_redirects=True)
        self.assertIn(b"demoted to Standard User", demote_resp.data)

        conn = app.get_db()
        u1_demoted = conn.execute("SELECT is_admin FROM users WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(u1_demoted["is_admin"], 0)

        # Self-demotion safety check: Admin (id=2) cannot demote themselves
        self_demote = self.app.post("/admin/user/2/toggle-admin", follow_redirects=True)
        self.assertIn(b"cannot modify your own", self_demote.data)

    def test_task_8_7_and_8_8_admin_occupancy_and_revenue_reports(self):
        """Task 8.7 & 8.8: Generate parking occupancy reports and revenue payment reports with CSV exports"""
        self.login_user(email="admin@test.com")

        # Task 8.7: Parking Occupancy Report
        occ_resp = self.app.get("/admin/reports/occupancy")
        self.assertEqual(occ_resp.status_code, 200)
        self.assertIn(b"Occupancy", occ_resp.data)
        self.assertIn(b"Slot Utilization Breakdown", occ_resp.data)

        # Task 8.7: CSV Export of Occupancy Report
        occ_csv = self.app.get("/admin/reports/occupancy/export")
        self.assertEqual(occ_csv.status_code, 200)
        self.assertEqual(occ_csv.content_type, "text/csv; charset=utf-8")
        self.assertIn(b"Slot Number", occ_csv.data)
        self.assertIn(b"A01", occ_csv.data)

        # Task 8.8: Revenue & Payment Report
        rev_resp = self.app.get("/admin/reports/revenue")
        self.assertEqual(rev_resp.status_code, 200)
        self.assertIn(b"Revenue", rev_resp.data)
        self.assertIn(b"Itemized Payment Ledger", rev_resp.data)

        # Task 8.8: CSV Export of Revenue Report
        rev_csv = self.app.get("/admin/reports/revenue/export")
        self.assertEqual(rev_csv.status_code, 200)
        self.assertEqual(rev_csv.content_type, "text/csv; charset=utf-8")
        self.assertIn(b"Transaction ID", rev_csv.data)
        self.assertIn(b"Amount (INR)", rev_csv.data)


if __name__ == "__main__":
    unittest.main()

