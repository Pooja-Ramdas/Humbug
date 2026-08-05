import sys, os
sys.path.insert(0, os.path.abspath('.'))
from backend.app import app
print('FastAPI app imported OK')
print('Routes:')
for r in app.routes:
    if hasattr(r, 'methods'):
        print(f"  {list(r.methods)} {r.path}")
