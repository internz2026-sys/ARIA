FROM python:3.11-slim

# Create a non-root user with a stable UID 1000. The host's
# /opt/aria/.claude is chowned to 1000:1000 so the volume mount at
# /home/aria/.claude is read+writable by this user without breaking
# the auto-restore flow in backend/tools/claude_cli.py.
RUN groupadd -g 1000 aria && useradd -u 1000 -g 1000 -m -s /bin/bash aria

WORKDIR /app

# Root-required steps: apt + nodejs + global npm install. Done before
# the USER switch so subsequent runs can drop privilege.
RUN apt-get update && \
    apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Python deps installed system-wide as root; readable by everyone via
# default /usr/local site-packages permissions.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App source with explicit aria ownership so the runtime user can
# read the .py / .md files without falling back to "world-readable"
# defaults.
COPY --chown=aria:aria backend/ backend/
COPY --chown=aria:aria docs/ docs/

# Pre-create /home/aria/.claude (volume mount target). The directory
# permissions here are masked at runtime by the host bind-mount, but
# having the path exist with aria ownership keeps the layer
# consistent for any local-dev usage that mounts a different path.
# /app gets chowned so any future runtime write (e.g. matplotlib's
# font cache, pip --user installs during dev) lands in a writable
# location.
RUN chown -R aria:aria /app /home/aria && \
    mkdir -p /home/aria/.claude && \
    chown -R aria:aria /home/aria/.claude

# HOME must be set explicitly — Docker's USER directive changes the
# runtime UID/GID but does NOT update the HOME env var. Without this,
# `os.path.expanduser("~/.claude.json")` resolves to the previous
# HOME (or empty), which would break the auto-restore + claude CLI
# auth lookup.
ENV HOME=/home/aria

EXPOSE 8000

USER aria

CMD ["sh", "-c", "uvicorn backend.server:socket_app --host 0.0.0.0 --port ${PORT:-8000}"]
