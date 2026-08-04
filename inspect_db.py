import sqlite3

con = sqlite3.connect('data/data.db')
cur = con.cursor()

tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('TABLES:', [t[0] for t in tables])

meta = cur.execute('SELECT * FROM meta').fetchall()
print('META:', meta)

count = cur.execute('SELECT COUNT(*) FROM pole_registry').fetchone()
print('Pole count:', count[0])

sample = cur.execute('SELECT * FROM pole_registry LIMIT 3').fetchall()
cols = [d[0] for d in cur.description]
print('Pole cols:', cols)
for r in sample:
    print(dict(zip(cols, r)))

# Check transformers
count_dt = cur.execute('SELECT COUNT(*) FROM transformer_registry').fetchone()
print('DT count:', count_dt[0])
sample_dt = cur.execute('SELECT * FROM transformer_registry LIMIT 2').fetchall()
cols_dt = [d[0] for d in cur.description]
print('DT cols:', cols_dt)

# Check feeders/substations
feeders = cur.execute('SELECT * FROM feeders').fetchall()
print('Feeders:', len(feeders))

subs = cur.execute('SELECT * FROM substations').fetchall()
print('Substations:', len(subs))

# Topology info - how many poles have parent_pole_id set vs not
with_topo = cur.execute("SELECT COUNT(*) FROM pole_registry WHERE parent_pole_id IS NOT NULL").fetchone()
without_topo = cur.execute("SELECT COUNT(*) FROM pole_registry WHERE parent_pole_id IS NULL").fetchone()
print(f'Poles with topology: {with_topo[0]}, without: {without_topo[0]}')

# Check for tickets table
for tname in ['tickets', 'telemetry', 'device_last_seen']:
    exists = cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{tname}'").fetchone()
    print(f'Table {tname} exists:', exists is not None)

con.close()
