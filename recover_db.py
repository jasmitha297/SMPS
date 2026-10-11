
import sqlite3
import os

SOURCE = "parking_backup.db"
OUTPUT = "parking_recovered.db"

if not os.path.exists(SOURCE):
    print("Backup file not found:", SOURCE)
    raise SystemExit

if os.path.exists(OUTPUT):
    print("Recovery output already exists.")
    print("Rename or inspect it before trying again.")
    raise SystemExit

source = sqlite3.connect(SOURCE)
destination = sqlite3.connect(OUTPUT)

try:
    source.backup(destination)
    print("Database backup completed.")
except sqlite3.DatabaseError as error:
    print("Standard recovery failed:", error)
finally:
    destination.close()
    source.close()

if os.path.exists(OUTPUT):
    check = sqlite3.connect(OUTPUT)
    try:
        print("Integrity check:",
              check.execute("PRAGMA integrity_check").fetchall())
        print("Tables:",
              check.execute(
                  "SELECT name FROM sqlite_master WHERE type='table'"
              ).fetchall())
    except sqlite3.DatabaseError as error:
        print("Recovered file needs further repair:", error)
    finally:
        check.close()
