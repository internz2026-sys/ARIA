#!/usr/bin/env bash
# ─────────────────────────────────────────────
# ARIA — Start backend + frontend in one command
# Usage:  bash start.sh
# ─────────────────────────────────────────────
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
  echo -e "\n${YELLOW}Shutting down ARIA...${NC}"
  kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
  wait $BACKEND_PID $FRONTEND_PID 2>/dev/null
  echo -e "${GREEN}All services stopped.${NC}"
}
trap cleanup EXIT INT TERM

# ── Check .env ──────────────────────────────
if [ ! -f "$ROOT_DIR/.env" ]; then
  echo -e "${RED}.env file not found!${NC}"
  echo "Copy .env.example and fill in your keys:"
  echo "  cp .env.example .env"
  exit 1
fi

# ── Backend setup ───────────────────────────
echo -e "${GREEN}[1/3] Setting up Python venv...${NC}"
if [ ! -d "$BACKEND_DIR/venv" ]; then
  python -m venv "$BACKEND_DIR/venv"
fi

# Activate venv (Windows Git Bash or Linux/Mac)
if [ -f "$BACKEND_DIR/venv/Scripts/activate" ]; then
  source "$BACKEND_DIR/venv/Scripts/activate"
else
  source "$BACKEND_DIR/venv/bin/activate"
fi

echo -e "${GREEN}[2/3] Installing Python dependencies...${NC}"
pip install -q -r "$BACKEND_DIR/requirements.txt"

# ── Frontend setup ──────────────────────────
echo -e "${GREEN}[3/3] Installing Node dependencies...${NC}"
cd "$FRONTEND_DIR"
npm install --silent 2>/dev/null

# ── Launch all services ─────────────────────
echo ""
echo -e "${GREEN}Starting ARIA...${NC}"
echo ""

# Load .env for backend
set -a
source "$ROOT_DIR/.env"
set +a

# Start backend
cd "$ROOT_DIR"
echo -e "  ${GREEN}Backend${NC}  → http://localhost:8000"
uvicorn backend.server:socket_app --reload --port 8000 &
BACKEND_PID=$!

# Start frontend
cd "$FRONTEND_DIR"
echo -e "  ${GREEN}Frontend${NC} → http://localhost:3000"
npm run dev &
FRONTEND_PID=$!

echo ""
echo -e "${GREEN}ARIA is running! Press Ctrl+C to stop all services.${NC}"
echo ""

# Wait for any process to exit
wait -n $BACKEND_PID $FRONTEND_PID 2>/dev/null
