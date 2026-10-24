# Fakhri HRMS

**Attendance, people and payroll for Fakhri IT Services** — a mobile-first PWA
that marks attendance by **face recognition**, keeps salary and leave records,
and runs monthly payroll. Three logins: an **HR console**, a locked-down **gate
kiosk** that can only scan, and an **employee portal** where each person sees
their own attendance, payslips and leave.

Themed with the company's own palette, pulled from
[fakhriitservices.com](https://www.fakhriitservices.com): primary **#870808**,
warm surface **#f6f4f4**, ink **#141414**, hairlines **#e8e5e3**, and their two
font families (Poppins headings, Inter body).

---

## The seven things it does

| # | Requirement | Where it lives |
|---|---|---|
| 1 | **Mark attendance by face detection + recognition** | `/kiosk` scanner → `POST /api/kiosk/scan` → Face API `/v1/identify` → check-in/check-out state machine in `app/services/attendance.py` |
| 2 | **Add a new employee and enrol their face** | Employees → Add employee (`/employees/new`), then the face-enrolment card on the profile page (camera or file upload) |
| 3 | **Add and edit everyone's monthly salary** | Salary card on the employee profile; every change is a dated revision, so past payroll months keep their old structure |
| 4 | **Apply paid leave and half days** | `/leave` — full day, multi-day, or a single half day; weekly offs and holidays are never charged; balances accrue 1.5/month from the joining date |
| 5 | **Mobile-first responsive PWA** | Sidebar on desktop, bottom tab bar on phones, installable manifests (`/manifest.webmanifest` for HR, `/kiosk.webmanifest` for the gate), service worker + offline page, camera capture via `getUserMedia` |
| 6 | **HR login** | `hr` role — everything: people, attendance, leave, payroll, settings, audit log |
| 7 | **Public/gate login that only marks attendance** | `kiosk` role — opening any HR page bounces straight back to the scanner; no salaries, no employee edits |

### Notifications, noticeboard and self-service

* **Email + WhatsApp/SMS outbox.** Every message is queued in SQLite and sent by a
  background thread, so a kiosk scan never waits for SMTP. Three drivers ship:
  `console` (writes real `.eml` files to `data/outbox/` — the default, so the whole
  thing works with no credentials), `smtp`, and `twilio`/`meta` for WhatsApp.
* **Automatic messages:** punch-in/check-out confirmation, a late alert, "your
  leave was requested" to HR, "your leave was approved/rejected" to the employee,
  "your profile change was decided", and **payslips with the PDF attached**.
* **Noticeboard** (`/announcements`) — post a notice once; it appears on the HR
  dashboard and every employee portal, and can be mailed/WhatsApped to everyone in
  the same action. Pin it, expire it, hide it.
* **Notification log** (`/notifications`) — what was sent to whom, when, why it
  failed, with "send queued now", "retry failed" and a test-message button.
* **Forgot password** (`/forgot-password`) — email OTP, 10-minute expiry, 5
  attempts, 60-second cooldown, stored hashed, and the same response whether or
  not the account exists (no user enumeration).
* **Payslip PDFs** — a real A4 PDF per payslip (fpdf2, core fonts, no font files
  to ship), downloadable by HR and by the employee from their own portal.
* **Profile & bank change workflow** (`/me/profile` → `/profile-requests`) —
  employees request changes; **nothing updates until HR approves**, and every
  decision is in the audit log.

### Employee records: documents, assets, expenses, field attendance

* **Document vault** (`/documents`) — Aadhaar, PAN, offer letters, contracts and resumes
  per employee. Uploads are validated by **magic number, not file extension**, capped at
  8 MB, and stored under random names in `data/uploads/`; the original filename is only a
  label. HR verifies each file; the employee sees only their own. PDF/JPEG/PNG/WebP.
* **Asset register** (`/assets`) — laptops, phones, SIMs, ID cards and peripherals with
  tag, serial and cost. Assign to an employee, return to stock, and the full assignment
  history is kept per asset. Employees see their own holdings at `/me/assets`.
* **Expense claims** (`/me/expenses`) — employee submits an amount, date, description and
  an optional **receipt photo**; HR approves or rejects in `/expenses`. Receipts are
  private to the claimant and HR. Approved totals per status are shown on both pages.
* **Field / remote attendance** (`/me/field`, `/locations`) — HR defines allowed places
  with a radius; staff check in from a phone using browser geolocation (plus an optional
  selfie). The server measures the distance with the haversine formula and **only marks
  attendance inside the fence**; an outside attempt is recorded with its distance but does
  not mark anything. Attendance written this way is tagged `source='field'` and carries the
  location in `device_label`.

  > **Honest limit:** phone GPS can be mocked. Geo-fencing raises the effort and leaves
  > evidence (coordinates, accuracy, distance, selfie) for HR to review — it does not prove
  > presence. Do not use it as the only control for anything security-critical.

### Employee portal (`/me`) — self-service

Each employee gets their own login (username = employee code, e.g. `fits001`,
password set by HR on the employee page or in bulk with `cli.py ess`). They see
**only their own** data — every query is filtered by the employee id inside the
signed session, and there is no employee-id parameter in the URLs to tamper with:

* **`/me`** — today's status (checked in / out / on leave / not marked), this
  month's present/half/leave/absent counts, leave balance, latest payslip.
* **`/me/attendance`** — day-by-day history with in/out times, hours and notes.
* **`/me/leave`** — balance and their own requests; **applying creates a
  `pending` request** that changes nothing until HR approves it.
* **`/me/payslips`** — their own payslips, printable to PDF. Someone else's
  payslip id returns "not found", not a permission error that confirms it exists.

HR approving a request stamps it onto the attendance calendar in the same action,
so payroll stays correct without a second reconciliation step. Rejecting it
leaves attendance untouched. Employees can withdraw a pending request themselves;
an approved one needs HR (cancelling restores the days).

### …plus what a real HRMS needs

Dashboard (present / absent / late / on-leave / not-marked, 7-day trend, recent
scans, payroll estimate) · monthly **attendance register** with per-day codes and
**CSV export** (full + summary) · manual attendance corrections · half-day and
late rules · **loss-of-pay payroll** with per-employee adjustments (bonus,
salary advance, other deductions) · printable **payslips** · bank-transfer CSV ·
holiday calendar · audit log of every action · face-service health panel ·
in-app user management.

---

## Quickstart

### 1. Personal / evaluation run (2 terminals)

The HRMS deliberately owns **no face model** — it calls the Face API project.
Start both:

```bash
# terminal 1 — the face service (models + recognition)
cd ../faceapi
FACEAPI_AUTH_ENABLED=false ./.venv/bin/python3 cli.py serve      # http://127.0.0.1:8000

# terminal 2 — the HRMS
cd ../hrms
bash scripts/setup_local.sh            # first time only: venv + deps + icons
./.venv/bin/python3 cli.py init        # creates the hr and gate logins
./.venv/bin/python3 cli.py serve       # http://127.0.0.1:8100
```

`cli.py init` prints the generated passwords (they are also written to
`data/initial_credentials.txt`, mode 600). Then open:

* **HR console** → <http://127.0.0.1:8100/dashboard>
* **Gate kiosk** → <http://127.0.0.1:8100/kiosk>

Want data to click through immediately?

```bash
./.venv/bin/python3 cli.py seed-demo --employees 12
./.venv/bin/python3 cli.py seed-demo --employees 12 --enrol   # needs the Face API running

# give everyone an employee-portal login (prints each password once)
./.venv/bin/python3 cli.py ess --missing-only
```

### 2. Docker (both services, one command)

```bash
cp .env.example .env        # set HRMS_SECRET_KEY and HRMS_DEFAULT_HR_PASSWORD
docker compose up -d --build
docker compose logs api | grep -i login      # first-run credentials
```

`docker-compose.yml` starts the Face API on the internal network **and** the
HRMS, with volumes for both databases. Add `--profile tls` for automatic HTTPS
via Caddy (required before the gate phone's camera will work — see below).

### 3. Verify the whole chain

With both services running:

```bash
./.venv/bin/python3 scripts/verify_e2e.py \
    --hr-password '<from data/initial_credentials.txt>' \
    --kiosk-password '<from data/initial_credentials.txt>'
```

It creates two employees, enrols two real faces, recognises one of them from a
**different photo**, checks in, checks out, refuses a faceless photo, exercises
the debounce, exports CSVs and runs a payroll month — over real HTTP against the
real models.

---

## Putting the scanner at the gate

This is the part that decides whether the system is actually usable, so here is
the whole recipe:

1. **Serve over HTTPS.** Browsers only hand over the camera on a secure origin.
   Either put the HRMS behind a domain with TLS (`deploy/Caddyfile` does it
   automatically) or use `http://localhost` *on the device itself* for testing.
2. On the gate phone/tablet (Android or iOS), sign in with the **kiosk** login.
3. Open the browser menu → **Add to Home screen**. It installs as *Scanner* with
   `start_url=/kiosk`, so it opens straight into the camera screen, full screen.
4. Tap **Tap to start**, allow the camera when the browser asks, and prop the
   device up — the oval guide shows where the face should be.
5. Scanning is automatic: it captures every ~1.4 s, pauses ~2.6 s after each
   result and beeps. Tapping the camera scans immediately.
6. The whole screen is just the camera plus **two** controls — **Flip** (front /
   rear camera) and **Sound** (on / muted) — and **⏏ sign out** in the top bar.
   The screen is kept awake with the Wake Lock API where supported.
7. A small dot in the bar shows scanner state: green = healthy, amber = the face
   service is unreachable (the result flash then says *Scanner offline*).

**The gate screen is deliberately anonymous.** It shows no names, no counters and
no list of who scanned before — a shared device must not let one person see
another's attendance. The result is just colour + IN/OUT + time:

| Result | Screen |
|---|---|
| Recognised, first scan of the day | green **✓ IN** 09:32 |
| Recognised, second scan | green **✓ OUT** 18:07 |
| Scanned twice in a row | amber **! Already scanned** |
| Already left for the day | amber **! Already done today** |
| On approved leave | amber **! On leave today** |
| Not enrolled / unknown face | red **✕ Not recognised** — ask HR to enrol |
| Nobody in frame | amber **! No face found** — step into the oval |
| Face API down | red **✕ Scanner offline** (HR can mark attendance meanwhile) |

By default (Settings → *Gate confirmation card* = full) a successful scan shows a
3.5 s card with the person's initials, name, code, designation, IN/OUT, time and
any late fine. Set it to *anonymous* to show only IN/OUT and the time — the scan
response then carries no employee data at all. Either way the scan is recorded
server-side (attendance row + audit log). *Gate scanner mode* picks tap-to-scan
(camera off until a tap; default) or continuous scanning.

---

## Roles and access

| | `hr` | `kiosk` | `employee` |
|---|---|---|---|
| Dashboard, employees, salaries | ✅ | ❌ (redirected to the scanner) | ❌ |
| Leave, payroll, settings, audit | ✅ | ❌ | ❌ |
| Open `/kiosk` and scan | ✅ (for testing) | ✅ | ❌ (redirected to `/me`) |
| Own attendance, payslips, leave | ✅ | ❌ | ✅ **only their own** |
| Device label on scans | “HR console” | the device label you set (`Main Gate`, `Back Gate`, …) |
| Other people's attendance on screen | full dashboard | **nothing** — the scanner shows no names, counts or history |

Sessions are signed, HTTP-only cookies: **12 hours** for HR and employees, **30
days** for the kiosk so the gate device does not need a daily sign-in.

Every state-changing request needs the session's CSRF token. Passwords are stored as salted
**scrypt** hashes; the plaintext is never written anywhere.

---

## Attendance rules (the contract payroll relies on)

* One record per employee per day.
* **First scan of the day = check-in.** After the shift start + grace
  (`shift_start` 09:30, `late_grace_minutes` 15 by default) the delay is stored
  as `late_minutes`.
* **Next scan = check-out.** Worked minutes are computed; working less than
  `half_day_min_minutes` (240) turns the day into a **half day** (0.5 payable).
* **Further scans the same day are refused** — a queue at the gate cannot rewrite
  anybody's hours. A scan within `HRMS_KIOSK_DEBOUNCE` seconds (45) of the last
  one is ignored entirely.
* **Leave and holidays are never overwritten** by a scan. A scan *on a weekly
  off* **is** recorded (that is comp-off evidence).
* **Any unmarked working day in the past counts as absent** — which is exactly
  what payroll charges as loss of pay. HR must close exceptions before running
  the month.
* HR can correct anything manually, and the row records `source = manual` plus
  the note, so the audit trail shows what was changed.

## Payroll logic

```
per_day      = monthly_gross / basis_days      # calendar days, or a fixed 26 (setting)
lop_days     = absent + unpaid_leave + (late_marks // marks_per_half_lop) * 0.5   # count policies only
earned_gross = monthly_gross - lop_days × per_day            (never below zero)
net_pay      = earned_gross + bonus − PF − ESI − professional tax − late fines − advance − other
```

Salary components come from the revision **in force for that month**, so a raise
applied in April does not retroactively change March. A run goes
`draft → finalized → paid`; only a draft can be recalculated, and a paid run is
frozen for good. Adjustments (bonus, advance, deduction, note) are per payslip
and are printed on the payslip.

---

## Configuration

Everything is an environment variable (or `.env`) — see [.env.example](.env.example).
The ones you will actually touch:

| Variable | Default | Meaning |
|---|---|---|
| `HRMS_FACEAPI_URL` | `http://127.0.0.1:8000` | where the Face API lives |
| `HRMS_FACEAPI_KEY` | *(empty)* | set when the Face API runs with auth enabled |
| `HRMS_FACE_COLLECTION` | `employees` | the Face API collection used for staff |
| `HRMS_FACE_REQUIRED` | `false` | `true` = the kiosk refuses to scan when the face service is down |
| `HRMS_SECRET_KEY` | auto → `data/secret_key.txt` | session signing key; set it explicitly in production |
| `HRMS_DEFAULT_HR_PASSWORD` | auto-generated | first HR password (drops into `data/initial_credentials.txt` otherwise) |
| `HRMS_DEFAULT_KIOSK_PASSWORD` | auto-generated | first gate password |
| `HRMS_TIMEZONE` → `HRMS_TZ` | `Asia/Kolkata` | the working day is a *local* concept |
| `HRMS_SECURE_COOKIES` | `false` | set `true` behind HTTPS |
| `HRMS_KIOSK_DEBOUNCE` | `45` | seconds before the same face can scan again |
| `HRMS_KIOSK_SHOW_NAME` | `false` | force the full confirmation card even if Settings says anonymous |

Operational settings (shift start, grace, half-day threshold, payroll basis,
leave accrual, weekly off day, late-mark penalty, currency) live in the database
and are edited at **Settings** in the UI — no restart needed.

---

## Project layout

```
hrms/
├── app/
│   ├── main.py            FastAPI factory, static mount, 404 page
│   ├── config.py          settings + the brand palette from the website
│   ├── db.py              SQLite schema, settings store, audit helper
│   ├── auth.py            scrypt passwords, signed sessions, CSRF, roles
│   ├── face.py            HTTP client for the Face API (health/enrol/identify)
│   ├── deps.py            Jinja setup, filters, render helpers
│   ├── services/
│   │   ├── orgtime.py     local-time + calendar helpers (week offs, holidays)
│   │   ├── employees.py   employee CRUD, salary revisions, face enrolment
│   │   ├── attendance.py  scan state machine, registers, CSV, dashboards
│   │   ├── leave.py       paid/unpaid/half-day leave, balances
│   │   └── payroll.py     runs, LOP maths, payslips, exports
│   ├── routes/            auth, dashboard, employees, attendance, leave,
│   │                      payroll, admin/settings, kiosk, **me (ESS)**, meta
│   ├── templates/         37 Jinja templates (branded, print-friendly payslip)
│   └── static/            css/app.css · js/app.js · js/kiosk.js · sw.js · icons
├── scripts/               make_icons.py, seed_demo.py, verify_e2e.py, setup_local.sh
├── tests/                 206 tests (services, web, ESS, notifications, records)
├── deploy/                Caddyfile, hrms.service (systemd)
├── cli.py                 doctor · init · serve · users · ess · stats · seed-demo
├── app/notify.py          outbox queue + email / WhatsApp / SMS drivers
├── app/pdf.py             payslip PDF renderer
├── app/uploads.py         validated file storage (magic-number sniffing, 8 MB cap)
├── app/services/records.py  documents · assets · expense claims
├── app/services/field.py  geo-fenced field attendance
└── Dockerfile · docker-compose.yml · .env.example
```

---

## Tests

```bash
./.venv/bin/python3 -m pytest            # 206 tests, ~27 s
./.venv/bin/python3 cli.py doctor        # environment + face service check
./.venv/bin/python3 scripts/verify_e2e.py --help
```

The suite covers password/session/CSRF behaviour, role restrictions, every page
rendering, employee validation, salary history, the full scan state machine
(check-in, late, half day, debounce, already-done, leave-blocked, week-off work,
inactive), leave rules (skip week offs, half day, cancel), payroll maths
(hand-computed month), run locking, adjustments, CSV exports, kiosk responses
(unknown face, no face, service down, orphan template) and settings changes.

---

## Brand notes

* Colours come from the live site's Tailwind tokens: `--sidebar-primary: #870808`
  and its dark variant `#a90a0a`, warm background `#f6f4f4`, ink `#141414`,
  muted `#737373`, hairlines `#e8e5e3`, plus their navy accents `#0a192f` /
  `#1c3f60` (used for the kiosk screen). Functional status colours (success /
  warning / danger) were chosen to sit beside the maroon.
* The app icon is a monogram **F** in the brand maroon, generated by
  `scripts/make_icons.py`. It is deliberately **not** a copy of the company logo.
  To use the real logo: drop a square PNG at `app/static/img/logo.png`, then
  replace the `<span class="brand-mark">F</span>` in `templates/base.html` and
  `templates/login.html` with `<img src="/static/img/logo.png" alt="">`, and
  overwrite `icon-192.png` / `icon-512.png` / `maskable-512.png`.

---

## Troubleshooting

**“Camera blocked / getUserMedia is not a function”.** The page is not on a
secure origin. Use HTTPS (or `localhost`) — this is a browser rule, not a bug.

**“Face service offline” on the kiosk.** Start the Face API and confirm
`HRMS_FACEAPI_URL` matches. Check `/settings` → Face service, or
`curl $HRMS_FACEAPI_URL/readyz`. Attendance can still be marked manually from the
HR console meanwhile.

**A kiosk scan says “Face not recognised” for someone who is enrolled.** Common
causes: enrolment photo was blurry or side-on; the gate lighting is very
different; the person changed appearance (beard, glasses). Re-enrol from a
straight-on photo taken in the same lighting as the gate. The similarity score
appears in the audit log; the threshold is 0.363 and can be tuned on the Face API
side.

**Nothing is marked for today but the scan said success.** Check the employee is
`active` (inactive employees refuse scans) and that the date is right — the
working day uses `HRMS_TZ`.

**The install prompt does not appear.** iOS Safari does not show one; use
Share → *Add to Home Screen*. Chrome/Android shows *Install app* in the menu.

**Nobody can sign in.** `./.venv/bin/python3 cli.py users list`, then
`cli.py users password hr` to set a known password.

---

## Deploy

See [docs/DEPLOY.md](docs/DEPLOY.md) for the VPS walkthrough (both services,
systemd, HTTPS, backups, monitoring, hardening) and
[docs/HR_GUIDE.md](docs/HR_GUIDE.md) for the day-to-day operating guide to hand
to the HR team.

## Compliance — read before going live

Face templates are **biometric data**: special-category personal data under GDPR
Art. 9, regulated by Illinois BIPA, India's DPDP Act and similar laws.

* Get each employee's **consent** before enrolling, tell them what it is used for,
  and delete the enrolment when they leave (`Remove face enrolment`).
* HRMS stores a **numeric template keyed to the employee code**, not a photo
  gallery — but the Face API holds the embeddings, so protect that database too.
  `../faceapi/docs/SELLING.md` §6 has the regime-by-regime checklist.
* This is **not** liveness detection and **not** identity verification: a printed
  photo of an enrolled face would be accepted. Use it for attendance convenience,
  and keep a human/HR fallback. Do not use it as the sole control for a secure
  door.
* Keep the security settings short and honest: your customers' obligation is
  notice + consent + retention limits, and the HRMS gives you the delete actions
  to honour them.
