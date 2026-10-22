# Deploying Fakhri HRMS

Two small services, both CPU-only:

```
   phone at the gate ─┐
   HR laptop ─────────┼──►  HRMS (FastAPI + SQLite)  ──►  Face API (YuNet + SFace)
                      │        port 8100                      port 8000 (internal)
   accountant ────────┘
```

| Resource | Minimum | Comfortable |
|---|---|---|
| CPU | 2 vCPU (1 for each service) | 4 vCPU |
| RAM | 1.5 GB | 3 GB |
| Disk | 5 GB (models + databases + backups) | 20 GB |
| Traffic | a scan is ~60–120 KB up and a few KB down — 1,000 scans/day is tiny | — |

Both fit on one **€6–8/month VPS**. SQLite means the HRMS is a *single-instance*
service: scale up, not out.

---

## Option A — Docker Compose (recommended)

`hrms/docker-compose.yml` builds and runs **both** services, with the Face API
reachable only on the internal network.

```bash
# on the server, as a normal user with docker access
git clone <your-repo> hrms-deploy && cd hrms-deploy/hrms
cp .env.example .env
```

Edit `.env` — the minimum:

```ini
HRMS_SECRET_KEY=<openssl rand -base64 48>
HRMS_DEFAULT_HR_PASSWORD=<a strong first password>
HRMS_DEFAULT_KIOSK_PASSWORD=<a strong gate password>
HRMS_PUBLIC_BASE_URL=https://hrms.yourdomain.com
HRMS_SECURE_COOKIES=true
HRMS_FACEAPI_URL=http://faceapi:8000
```

Then:

```bash
docker compose up -d --build
docker compose logs -f api        # first run prints the created logins
curl -s localhost:8100/healthz
```

### HTTPS — required for the gate camera

Browsers refuse `getUserMedia` on plain HTTP (except `localhost`), so the kiosk
**must** be on HTTPS. Caddy gets a certificate automatically:

```bash
nano deploy/Caddyfile          # your domain + email; DNS A record must already point here
docker compose --profile tls up -d
curl -s https://hrms.yourdomain.com/api/health
```

Once Caddy is in front, stop publishing the app port directly
(`HOST_PORT=127.0.0.1:8100:8100`) or remove the `ports:` entry from `api:` so only
Caddy can reach it.

### Firewall

```bash
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

---

## Option B — no Docker (venv + systemd + nginx)

### Face API

```bash
sudo useradd --system --create-home --home-dir /opt/faceapi faceapi
sudo -u faceapi git clone <your-repo>/faceapi /opt/faceapi
cd /opt/faceapi
sudo -u faceapi python3 -m venv .venv
sudo -u faceapi ./.venv/bin/pip install -r requirements.txt
sudo -u faceapi ./.venv/bin/python scripts/download_models.py
```

Run it **without** auth on localhost only — the HRMS is its only client:

```bash
# /etc/systemd/system/faceapi.service
[Service]
User=faceapi
WorkingDirectory=/opt/faceapi
Environment=FACEAPI_AUTH_ENABLED=false
Environment=FACEAPI_DATA_DIR=/var/lib/faceapi
ExecStart=/opt/faceapi/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
```

Follow `../faceapi/docs/DEPLOY.md` for the full unit if you prefer the hardened
version.

### HRMS

```bash
sudo useradd --system --create-home --home-dir /opt/hrms hrms
sudo -u hrms git clone <your-repo>/hrms /opt/hrms
cd /opt/hrms
sudo -u hrms python3 -m venv .venv
sudo -u hrms ./.venv/bin/pip install -r requirements.txt
sudo -u hrms ./.venv/bin/python cli.py make-icons

sudo mkdir -p /var/lib/hrms /etc/hrms
sudo chown hrms:hrms /var/lib/hrms
sudo install -m 600 /dev/null /etc/hrms/hrms.env
sudo tee /etc/hrms/hrms.env >/dev/null <<'EOF'
HRMS_DATA_DIR=/var/lib/hrms
HRMS_FACEAPI_URL=http://127.0.0.1:8000
HRMS_PUBLIC_BASE_URL=https://hrms.yourdomain.com
HRMS_SECURE_COOKIES=true
HRMS_TZ=Asia/Kolkata
HRMS_SECRET_KEY=CHANGE_ME
HRMS_DEFAULT_HR_PASSWORD=CHANGE_ME
HRMS_DEFAULT_KIOSK_PASSWORD=CHANGE_ME
EOF

sudo cp deploy/hrms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now faceapi hrms
sudo systemctl status hrms
sudo -u hrms /opt/hrms/.venv/bin/python cli.py doctor
```

Reverse proxy with TLS — copy `deploy/nginx.conf` from the *faceapi* project (it
is the same shape) or use Caddy with the Caddyfile in this repo, swapping
`api:8100` for `127.0.0.1:8100`.

---

## First-run checklist

1. `curl -s https://hrms.yourdomain.com/api/health` → `"status":"ok"` and
   `face_service.ok: true`.
2. Sign in as `hr`, go to **Settings → Users**, and **change the HR password**.
3. Keep the `gate` kiosk login (or create one per gate with a device label like
   `Back Gate` — the label is recorded on every scan).
4. Delete `data/initial_credentials.txt` from the server once the passwords are
   changed.
5. Set the real rules in **Settings**: shift start, grace minutes, half-day
   threshold, payroll basis (calendar or 26), leave accrual, weekly off day,
   late-mark penalty.
6. Add holidays for the year (Settings or the Leave page).
7. Add employees, then enrol each face on their profile page.
8. Install the kiosk PWA on the gate device (see README → *Putting the scanner at
   the gate*) and do one test scan with a real person.
9. Run one **payroll month in draft**, compare a payslip against the previous
   month's manual calculation, then finalise.

---

## Backups

Two files matter: `hrms.db` (people, salaries, attendance, leave, payroll) and
the Face API's `faceapi.db` (the biometric templates). Back both up, encrypted.

```bash
# consistent SQLite backup without stopping the service
docker compose exec api python - <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/hrms.db"); dst = sqlite3.connect("/app/data/backup.db")
src.backup(dst); print("backup written")
PY
docker compose cp api:/app/data/backup.db ./hrms-$(date +%F).db

# the face templates too
docker compose exec faceapi python - <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/faceapi.db"); dst = sqlite3.connect("/app/data/backup.db")
src.backup(dst); print("backup written")
PY
docker compose cp faceapi:/app/data/backup.db ./faceapi-$(date +%F).db
```

Nightly cron (keeps 30 days, ships off-box):

```cron
30 2 * * * cd /home/deploy/hrms-deploy/hrms && docker compose exec -T api python -c "import sqlite3;sqlite3.connect('/app/data/backup.db').close()" >/dev/null 2>&1; docker compose cp api:/app/data/hrms.db /backups/hrms-$(date +\%F).db && find /backups -name 'hrms-*.db' -mtime +30 -delete
```

Restore: stop the service, put the file back at `data/hrms.db`, start again —
there are no migrations to run.

**Treat backups as sensitive personal data**: they contain salaries and
biometric links. Encrypt at rest and set a retention period.

---

## Monitoring

```yaml
# prometheus.yml — the Face API exposes metrics; the HRMS exposes health
scrape_configs:
  - job_name: faceapi
    metrics_path: /metrics
    static_configs: [{ targets: ["faceapi.internal:8000"] }]
```

Alert on:

* `GET /healthz` failing for 2 minutes (HRMS down)
* `GET /api/health` reporting `face_service.ok == false` (the gate becomes blind)
* disk free < 20 %
* a jump in 5xx in the nginx/Caddy access log
* (optional) `faceapi_requests_total{status="503"}` — the face service running out
  of workers during the morning rush

Watch the morning rush yourself on the first week: the kiosk page shows
*inside now*, *late* and *not marked* counters that HR can glance at from the
dashboard.

---

## Scaling and tuning

| Symptom | Fix |
|---|---|
| Scans queue in the morning | the Face API is the bottleneck: raise its `--workers` (≈ vCPU) and check `FACEAPI_MAX_IMAGE_SIDE` (the kiosk sends ~720 px frames already) |
| HR pages slow | raise HRMS `--workers`; SQLite writes are serialised but tiny |
| Register/payroll page slow with 300+ employees | it renders a full month matrix; use the CSV export and the department filter |
| More than one HRMS instance | you must move state to Postgres — `app/db.py` is the only file that talks SQL; sessions are already stateless |
| Face service on another host | set `HRMS_FACEAPI_URL` and, if it is public, `FACEAPI_AUTH_ENABLED=true` plus `HRMS_FACEAPI_KEY` (create one with `faceapi/cli.py keys create --plan personal`) |

---

## Hardening checklist

- [ ] `HRMS_SECRET_KEY` set from a secret manager (not the generated file)
- [ ] `HRMS_SECURE_COOKIES=true` and TLS everywhere; HSTS on
- [ ] first-run passwords changed; `data/initial_credentials.txt` deleted
- [ ] kiosk login per gate with a device label; disable any unused logins
- [ ] the face service **not** reachable from the internet
- [ ] `/api/health` visible only to your monitoring (proxy rule) if you prefer
- [ ] uploads capped at the proxy (`client_max_body_size` ≥ 20 MB)
- [ ] nightly encrypted backups + a restore you have actually tested
- [ ] audit log reviewed monthly (`/audit`) — it captures logins and every change
- [ ] written consent + retention policy for employees, and the delete actions
      used when someone leaves (`Remove face enrolment`, then deactivate)
- [ ] a documented fallback for when cameras or the face service fail: HR marks
      attendance manually from `/attendance`

## Updating

```bash
git pull
docker compose up -d --build          # or: systemctl restart hrms
```

No migrations: `app/db.py` creates missing tables and seeds new settings on
startup. Take a backup before jumping many versions.
