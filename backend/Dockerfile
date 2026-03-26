FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend source
COPY backend/ backend/

EXPOSE 8000

CMD ["uvicorn", "backend.server:socket_app", "--host", "0.0.0.0", "--port", "8000"]
