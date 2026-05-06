#!/bin/bash
#
# Staging deploy script — invoked by the webhook listener when a push
# lands on the `staging` branch. Lives at /opt/aria-staging/deploy-staging.sh
# on the VPS (see STAGING_SETUP.md for the one-time install).
#
# Pulls the staging branch into /opt/aria-staging, then rebuilds the
# -staging containers via docker-compose.staging.yml. Prod containers
# are not touched.
#
# nginx is owned by the prod stack at /opt/aria, so we do NOT restart
# it from here — it routes by Host header and re-resolves the
# backend-staging / frontend-staging upstreams via Docker DNS within
# 10s, so nothing else is required.

set -euo pipefail
cd /opt/aria-staging

echo "[deploy-staging] git pull origin staging"
git pull origin staging

echo "[deploy-staging] rebuilding staging containers"
docker compose -f docker-compose.staging.yml --env-file /opt/aria/.env up -d --build backend-staging frontend-staging

echo "[deploy-staging] done at $(date -u +%FT%TZ)"
