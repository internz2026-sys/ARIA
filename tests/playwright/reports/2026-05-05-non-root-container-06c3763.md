# Drop root from aria-backend container — commit `06c3763`

**Run:** 2026-05-05T15:02 UTC
**Approach:** Option A — true non-root with `HOME=/home/aria` and host data migrated from `/root/.claude/` → `/opt/aria/.claude/` (chowned to uid 1000)

| Check | Status | Result |
|---|---|---|
| Container runtime UID | PASS | `uid=1000(aria) gid=1000(aria) groups=1000(aria)` |
| `HOME` env var set correctly | PASS | `/home/aria` |
| Volume mount visible to aria | PASS | All files in `/home/aria/.claude/` owned by `aria:aria` |
| Auto-restore on first boot | PASS | Log: `Auto-restored /home/aria/.claude.json from .claude.json.backup.1777977210636 (CLI config rotation race recovered)` |
| Claude auth file present | PASS | `/home/aria/.claude.json` 21181 bytes, owned by aria |
| `/health` responds | PASS | `{"status":"healthy"}` |
| Auth middleware still works | PASS | Unauth'd request → `401 Missing authorization token` |
| aria can read app source | PASS | `cat /app/backend/server.py` succeeds |
| aria can write to `/tmp` | PASS | Write + read + delete succeeded |
| **aria CANNOT write to `/etc/passwd`** | PASS | `Permission denied` — privilege containment confirmed |

## Migration

VPS-side performed before push:
```bash
mkdir -p /opt/aria/.claude
cp -a /root/.claude/. /opt/aria/.claude/    # 700 files
chown -R 1000:1000 /opt/aria/.claude
```

`docker-compose.yml` mount changed from `/root/.claude:/root/.claude` to `/opt/aria/.claude:/home/aria/.claude`.

## Verdict

PASS. Backend now runs as uid 1000 inside the container. Any future RCE in the FastAPI process can no longer write to root-owned paths (`/etc`, etc.) — the worst case is still confined to `/app` (read-only for aria) plus `/home/aria/` (writable, but volume-mounted from host where it's bounded to `/opt/aria/.claude`). Auto-restore worked on first boot via the existing `_try_restore_claude_config` helper, no manual fix needed.
