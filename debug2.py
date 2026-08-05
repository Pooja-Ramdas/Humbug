import sqlite3
con = sqlite3.connect('data/data.db')
con.row_factory = sqlite3.Row

# Check the ticket status in DB
t = con.execute("SELECT id, status, verified_at, closed_at, confidence FROM tickets").fetchall()
for row in t:
    print(dict(row))

# Manually trigger detection pass
import sys
sys.path.insert(0, '.')
from build_graph import build_graph
from detection import run_detection_pass

g = build_graph('data/data.db')
statuses = run_detection_pass(con, g)

# Check again
t = con.execute("SELECT id, status, verified_at, closed_at FROM tickets").fetchall()
print("\nAfter detection pass:")
for row in t:
    print(dict(row))
