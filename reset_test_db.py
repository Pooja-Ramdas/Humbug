"""Reset runtime tables so we start clean for the re-test."""
import sqlite3
con = sqlite3.connect('data/data.db')
con.execute("DELETE FROM telemetry")
con.execute("DELETE FROM tickets")
con.execute("DELETE FROM device_last_seen")
con.execute("DELETE FROM active_faults")
con.commit()
print("Runtime tables cleared.")
