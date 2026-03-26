FROM python:3.11-slim

# Install Node.js 20 for Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (uses host ~/.claude mount for auth — no API key needed)
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend source
COPY backend/ backend/

EXPOSE 8000

CMD ["uvicorn", "backend.server:socket_app", "--host", "0.0.0.0", "--port", "8000"]
