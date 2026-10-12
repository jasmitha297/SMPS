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
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM vehicles")
        conn.execute("DELETE FROM parking_slots")
        conn.execute("DELETE FROM users")
        
        # Insert test users
        conn.execute(
            "INSERT INTO users (id, name, email, password, is_admin) VALUES (?, ?, ?, ?, ?)",
            (1, "Test User", "user@test.com", generate_password_hash("password123"), 0)
        )
        conn.execute(
            "INSERT INTO users (id, name, email, password, is_admin) VALUES (?, ?, ?, ?, ?)",
            (2, "Admin User", "admin@test.com", generate_password_hash("password123"), 1)
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


if __name__ == "__main__":
    unittest.main()
