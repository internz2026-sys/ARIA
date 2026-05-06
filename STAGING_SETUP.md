# Staging Environment — One-Time VPS Setup

This is the VPS-side runbook for bringing up `staging.aria.hoversight.agency` alongside production. The repo files (`docker-compose.staging.yml`, `nginx.conf` updates, `deploy-staging.sh`, Traefik labels) are already in place; the steps below are the ops glue.

**Sharing model the operator chose on 2026-05-06:**
- Supabase: shared with prod (staging actions touch real data)
- Email outbound: shared via Hostinger SMTP (staging CAN send real email)
- Email inbound (IMAP): disabled on staging so prod and staging don't race
- Redis / Qdrant / Paperclip: shared (per-tenant keys, no collision)

---

## 1. DNS — point staging at the same VPS

In Hostinger DNS Zone for `hoversight.agency`:

| Type | Name | Points to | TTL |
|---|---|---|---|
| A | `staging.aria` | `72.61.126.188` | 3600 |

Wait ~5 min for propagation. Verify with:

```bash
nslookup staging.aria.hoversight.agency 8.8.8.8
```

Should resolve to the VPS IP.

---

## 2. Create the staging clone

```bash
ssh root@72.61.126.188

# Clone a SEPARATE working tree for staging — do NOT switch branches
# inside /opt/aria, or prod auto-deploys would fight the staging branch
# checkout.
git clone https://github.com/internz2026-sys/ARIA.git /opt/aria-staging
cd /opt/aria-staging
```

If the `staging` branch doesn't exist yet:

```bash
# Create it from main, push to remote so the webhook can find it.
git checkout -b staging
git push -u origin staging
```

If it does exist:

```bash
git checkout staging
git pull origin staging
```

---

## 3. Wire the staging deploy script

`deploy-staging.sh` ships in the repo. Make it executable:

```bash
chmod +x /opt/aria-staging/deploy-staging.sh
```

Test it manually first (confirms env_file resolution + network attach + build):

```bash
/opt/aria-staging/deploy-staging.sh
```

Expected output:

```
[deploy-staging] git pull origin staging
[deploy-staging] rebuilding staging containers
... docker build output ...
 Container aria-backend-staging  Started
 Container aria-frontend-staging Started
[deploy-staging] done at 2026-05-06T...Z
```

Verify both containers are running:

```bash
docker compose -f /opt/aria-staging/docker-compose.staging.yml ps
```

---

## 4. Reload nginx so the new server blocks take effect

The repo's `nginx.conf` already has both server blocks (prod + staging). Reload nginx in the prod stack to pick up the new config:

```bash
cd /opt/aria
git pull origin main
docker compose up -d nginx
```

The Traefik labels for the staging router were added to `docker-compose.yml` in the same commit — Traefik picks them up automatically when the nginx container restarts.

---

## 5. First-request cert provisioning

Hit the staging URL once from any browser:

```
https://staging.aria.hoversight.agency
```

Traefik's Let's Encrypt resolver provisions the cert on first request. May take ~30 seconds; if it shows a self-signed warning, refresh after a minute. Subsequent requests serve the real cert.

---

## 6. Add the staging webhook

### 6a. On the VPS — extend `/etc/webhook.conf`

Append a second hook entry (keep the existing `deploy-aria` block intact):

```bash
sudo nano /etc/webhook.conf
```

Add (assuming the file is a JSON array of hooks):

```json
{
  "id": "deploy-aria-staging",
  "execute-command": "/opt/aria-staging/deploy-staging.sh",
  "command-working-directory": "/opt/aria-staging",
  "trigger-rule": {
    "and": [
      {
        "match": {
          "type": "payload-hmac-sha256",
          "secret": "REPLACE_WITH_NEW_SECRET",
          "parameter": { "source": "header", "name": "X-Hub-Signature-256" }
        }
      },
      {
        "match": {
          "type": "value",
          "value": "refs/heads/staging",
          "parameter": { "source": "payload", "name": "ref" }
        }
      }
    ]
  }
}
```

Pick a NEW secret (different from prod's `absolutemadness`) — generate one with:

```bash
openssl rand -hex 24
```

Save the file, then reload the webhook service:

```bash
systemctl restart webhook
journalctl -u webhook -f
```

You should see `loaded 2 hook(s) from file`.

### 6b. On GitHub — add the webhook

Repo → Settings → Webhooks → Add webhook:

- Payload URL: `http://72.61.126.188:9000/hooks/deploy-aria-staging`
- Content type: `application/json`
- Secret: (the secret you generated above)
- Which events: Just the `push` event

Save. GitHub fires a ping; check `journalctl -u webhook -f` — should see a successful match.

---

## 7. End-to-end smoke test

```bash
# From your laptop, on the staging branch:
git checkout staging
git commit --allow-empty -m "test: staging webhook deploy"
git push origin staging
```

Tail webhook logs on the VPS:

```bash
ssh root@72.61.126.188
journalctl -u webhook -f
```

Expected sequence:
```
incoming HTTP POST /hooks/deploy-aria-staging
deploy-aria-staging got matched
200 OK
executing /opt/aria-staging/deploy-staging.sh
[deploy-staging] git pull origin staging
[deploy-staging] rebuilding staging containers
[deploy-staging] done at ...
```

Refresh `https://staging.aria.hoversight.agency` — should serve the latest staging code.

---

## Daily workflow

```bash
# 1. Branch off staging (or main — both fine for new feature)
git checkout staging
git pull origin staging
git checkout -b feat/something
# ... write code ...

# 2. Promote to staging
git checkout staging
git merge feat/something
git push origin staging
# → auto-deploys to staging.aria.hoversight.agency

# 3. Test on staging URL

# 4. When happy, promote to prod
git checkout main
git merge staging
git push origin main
# → auto-deploys to aria.hoversight.agency
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Staging URL returns "Bad Gateway" | Containers may not have started — `docker compose -f /opt/aria-staging/docker-compose.staging.yml ps` to confirm. If they crashed, `docker compose -f docker-compose.staging.yml logs --tail 50 backend-staging`. |
| Staging deploy succeeds but nginx still 502s | Nginx is using stale upstream IPs. `docker compose -f /opt/aria/docker-compose.yml restart nginx` forces a re-resolve. |
| Webhook shows "Hook rules were not satisfied" on staging push | Branch ref or HMAC secret mismatch. Verify the GitHub webhook secret matches `/etc/webhook.conf` AND that the push was to the `staging` branch (not a feature branch). |
| `aria_default network not found` during staging compose up | The prod stack hasn't created its default network yet, OR the project name differs. Check `docker network ls` — if you see `opt-aria_default` instead, update the `name:` field in `docker-compose.staging.yml`. |
| Cert never provisions on staging | Hit the URL from a real browser (not curl) — Let's Encrypt's Traefik resolver fires on the TLS handshake, not on the HTTP request. Check `docker compose logs traefik` if more than 60s pass. |
| Staging tests showing up in prod data | Expected — Supabase is shared. Use a clearly-named test tenant and clean up afterwards. |
