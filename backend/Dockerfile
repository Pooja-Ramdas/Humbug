FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached unless requirements.txt changes)
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source (app.py + __init__.py for package resolution)
COPY backend/app.py      /app/backend/app.py
COPY backend/__init__.py /app/backend/__init__.py

# Copy repo-root Python modules that backend/app.py imports
COPY detection.py      /app/detection.py
COPY build_graph.py    /app/build_graph.py
COPY telemetry_sim.py  /app/telemetry_sim.py
COPY generate_data.py  /app/generate_data.py

# Copy data directory (seed DB + CSVs)
COPY data/ /app/data/

# Startup script: generates fresh DB if not present, then starts uvicorn
COPY backend/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV DB_PATH=/app/data/data.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
