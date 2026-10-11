
import sqlite3

conn = sqlite3.connect("parking.db")

email = input("Enter the registered admin email: ").strip().lower()

cursor = conn.execute(
    "UPDATE users SET is_admin = 1 WHERE email = ?",
    (email,)
)

conn.commit()

if cursor.rowcount:
    print("Admin role enabled successfully.")
else:
    print("No user found with that email.")

conn.close()
