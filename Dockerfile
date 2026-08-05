FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire repository (backend, frontend, data, detection modules)
COPY . /app

# Ensure entrypoint script is executable
RUN chmod +x /app/backend/entrypoint.sh

ENV DB_PATH=/app/data/data.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["/app/backend/entrypoint.sh"]
