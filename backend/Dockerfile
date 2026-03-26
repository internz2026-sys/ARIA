FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend source + agent docs (CEO chat reads .md files)
COPY backend/ backend/
COPY docs/ docs/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.server:socket_app --host 0.0.0.0 --port ${PORT:-8000}"]
