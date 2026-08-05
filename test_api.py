import urllib.request, urllib.error, json, time

BASE = 'http://localhost:8000'

def get(path):
    r = urllib.request.urlopen(BASE + path)
    return json.loads(r.read())

def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
          headers={'Content-Type': 'application/json'}, method='POST')
    r = urllib.request.urlopen(req)
    return json.loads(r.read())

# Health
h = get('/health')
print('Health:', h)

# Stats
s = get('/stats')
print('Stats:', s)

# Poles (first 3)
poles = get('/poles')
print(f'Poles: {len(poles)} total, first 3:')
for p in poles[:3]:
    print(f"  {p['pole_id']} dt={p['dt_id']} status={p['status']}")

# Tickets (should be empty initially)
tickets = get('/tickets')
print(f'Tickets: {len(tickets)}')

# Inject a DT fault
dts = get('/transformers')
target_dt = dts[0]['dt_id']
print(f'\nInjecting DT fault on {target_dt}...')
result = post('/simulate/fault', {'type': 'dt', 'target_id': target_dt})
print('Inject result:', result)

# Check tickets
time.sleep(0.5)
tickets = get('/tickets')
print(f'\nTickets after inject: {len(tickets)}')
for t in tickets:
    print(f"  {t['id']} target={t['target_id']} status={t['status']} poles={t['affected_pole_count']} conf={t.get('confidence')}")

# Restore
print(f'\nRestoring {target_dt}...')
res = post('/simulate/restore', {'type': 'dt', 'target_id': target_dt})
print('Restore result:', res)

# Check tickets again
time.sleep(0.5)
tickets = get('/tickets')
print(f'\nTickets after restore: {len(tickets)}')
for t in tickets:
    print(f"  {t['id']} status={t['status']} verified_at={t.get('verified_at')}")
