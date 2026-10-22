# HR operating guide

Everything the HR team needs, in the order you will actually do it.
Written for the person using the system, not for a developer.

> **रोज़ का काम (quick version):**
> सुबह — Dashboard खोलिए, "Present today" और "Not marked" देखिए।
> नया employee — Employees → Add employee → profile पर face capture।
> छुट्टी — Leave → employee चुनिए → date → Apply।
> महीने के अंत में — Attendance register check → Payroll → Generate → Finalise → Mark as paid।

---

## 1. Signing in

| Who | Where | What they can do |
|---|---|---|
| **HR** | `https://hrms.yourdomain.com/dashboard` | everything |
| **Gate** (kiosk login) | `https://hrms.yourdomain.com/kiosk` | mark attendance by face only |

On a phone you can install both as apps: open the site in Chrome (Android) →
menu → **Install app**; on iPhone use Safari → Share → **Add to Home Screen**.
The gate device installs as *Scanner* and opens straight into the camera.

Change the initial passwords immediately: **Settings → Logins → Manage → Set
password**. Do not share the HR login with the gate staff.

---

## 2. Adding an employee

**Employees → ➕ Add employee**

1. **Full name** and **Employee code** (it suggests `FITS004`; the code is the
   identity used by the face system, so keep it stable once set).
2. Department, designation, email, phone, **date of joining**, employment type.
3. **Monthly gross (CTC)** and, if you track them, basic / HRA / PF / ESI /
   professional tax. These drive payroll deductions.
4. **Save** — you land on the employee page with a warning that no face is
   enrolled yet.

### Enrolling their face

On the employee page, **Face enrolment** card:

1. Tap **Start camera**, allow camera access (the browser asks once).
2. Ask the person to look straight at the camera, no cap or mask, decent light.
3. Tap **Capture & enrol face**. You will see *Face enrolled (detection score
   0.91)* — anything below ~0.85 means retake.
4. No camera on your laptop? Use **Enrol from file** and pick a photo.

Their first scan at the gate is then recognised. **Ask the person first** — you
need their consent, and you should be able to tell them what the template is used
for and that it is deleted when they leave.

To replace a bad enrolment, capture again (the old template is removed
automatically). To stop someone scanning without deleting their records, use
**Remove face enrolment**, or **Mark inactive** if they have left.

---

## 3. The daily attendance rhythm

### HR (2 minutes each morning)

Open the **Dashboard**:

* **Present today / attendance %** — how many are in.
* **Absent** and **not marked** — chase these. "Not marked" at 11 am usually
  means a forgotten scan or someone on leave that was never recorded.
* **Face enrolment pending** — anyone without a face cannot scan; enrol them.
* **Today's scans** — the last few people through the gate, with gate label.

Use **Daily board** to see everyone for a date, with in/out times, worked hours,
late minutes and the source (`face`, `manual`, `leave`). Change the date at the
top to look at any day.

### Correcting a mistake

**Daily board → ✏️ Manual attendance entry**:

* *Forgot to scan* → employee + date + Present + check-in/out times.
* *Half day* → status **Half day**.
* *Absent* → status **Absent** (payroll will deduct a day).
* *Came in but it shows leave* → set Present; the note will show it overrode the
  earlier entry.
* To wipe a row entirely, use **Clear** on that row.

Every manual change is stamped `manual` and written to the audit log, so you can
always see who changed what.

### The gate device

The gate login shows **only the camera**: no names, no counters, no list of who
scanned before — deliberately, so one person cannot see another's attendance.
Training is one line: *look at the camera, wait for the green tick.*

* Green **✓ IN** with a time — they are marked in. Nothing to tap.
* Green **✓ OUT** with a time — they are marked out.
* Amber **! Already scanned** — they are standing in front of the camera too
  long; ask them to step aside for a moment.
* Amber **! No face found** — step into the oval, better light, remove the mask.
* Red **✕ Not recognised** — either they are not enrolled, or the enrolment photo
  was poor. Re-enrol them from the HR console.
* Red **✕ Scanner offline** — call the administrator; HR can still mark
  attendance manually meanwhile.
* The small dot in the top bar shows the scanner state (green = healthy,
  amber = face service unreachable).

The two round buttons are all the controls: **Flip** (front / rear camera) and
**Sound** (beep on / muted). **⏏** in the top bar signs the gate device out.

> The gate screen never shows whose attendance was marked — only a tick and the
> time. If your gate staff need the name to double-check a match, ask the
> administrator to switch on `HRMS_KIOSK_SHOW_NAME`.

---

## 4. Leave and half days

**Leave → Apply leave for an employee**

| Field | Notes |
|---|---|
| Employee | pick from the list |
| Leave type | **Paid / Sick / Casual** — paid, and they reduce the leave balance. **Unpaid** — reduces that month's salary (loss of pay). |
| From / To | the same date for one day; a range for many |
| **Half day** | tick for a single 0.5-day (bank work, doctor in the morning) |
| Reason | short note, appears in the record |

Click **Apply leave**. The attendance calendar is updated immediately, and
weekly offs or holidays inside the range are **skipped** — the confirmation tells
you exactly which dates were skipped and which were charged.

* **Leave balance** (bottom table): accrued = months of service × 1.5 (adjustable
  in Settings) − paid leave taken. Unpaid leave is shown separately and does not
  touch the balance.
* To undo: **Cancel** on the leave row. The attendance days it created are
  removed; a day that already had a real check-in is left alone.
* **Holidays**: add them here (or in Settings) so they are not counted as working
  days anywhere.

---

## 4b. Approving leave that employees request themselves

Employees can request leave from their own login (`/me/leave`). Those arrive as
**pending** and change nothing until you decide:

1. Open **Leave** — a highlighted card at the top lists everything waiting,
   with the person, type, dates and reason.
2. **Approve** → the days are written into the attendance calendar immediately
   (weekly offs and holidays inside the range are skipped, and the confirmation
   tells you which dates were skipped). Payroll will treat them as paid leave.
3. **Reject** → nothing is written; the employee sees "rejected" on their page.

An employee can withdraw their own request while it is pending. Once approved,
only HR can cancel it — that also restores the attendance days.

### Giving someone their portal login

**Employees → (person) → Employee portal → Create portal login.** The username is
their employee code in lower case (`FITS001` → `fits001`). The form shows a
suggested password — copy it before you click, because it is never shown again.
Share it privately; they can change it later, and you can reset it any time from
the same card (or disable the login when someone leaves).

Bulk option for a whole team, from the server:
`./.venv/bin/python3 cli.py ess --missing-only`

### What employees can see

Only their own record: today's status, their month, their payslips and their
leave balance. They cannot see anybody else's attendance, the payroll screen, or
the gate scanner.

## 4c. Noticeboard, emails and payslip dispatch

**Post a notice** — *Noticeboard* in the sidebar. Write the title and message, pick a tone
(info / success / warning / urgent), choose who sees it (everyone / employees / HR), and
tick **Email everyone** if you want it in their inbox too. It appears immediately on the
HR dashboard and on every employee's portal. Pin important ones; set *Hide after* to make
it expire by itself.

**See what was sent** — *Notifications*. Every message is listed with its state:
*waiting* (queued), *sent*, or *failed* with the reason. Three useful buttons:
**Send queued now**, **Retry failed**, and **Send a test** to check your SMTP details.

**Email payslips** — *Payroll → pick the month*. A payslip can only be emailed once the
run is **finalised** (draft figures can still change). Then press **✉️ Email payslips**:
one email per employee with the **PDF attached**, skipping anyone with no email address.
Each row also has a **PDF** button for a single download.

> Delivery note: until your SMTP details are entered, every "email" is written to
> `data/outbox/` on the server instead of being sent — handy for testing. Ask your
> administrator to configure `HRMS_SMTP_*` for real delivery.

## 4d. Approving profile and bank-detail changes

Employees can request changes to their phone, email, address, emergency contact and
**bank details** from their own portal. These arrive in **Profile changes**:

1. Review the *current* value against the *requested* value.
2. **Approve** → the employee record is updated on the spot, and the employee is emailed.
3. **Reject** → nothing changes; the employee sees the rejection.

Bank details never change silently — every approval records who did it and when, which is
exactly what you want if a salary transfer is ever disputed.

## 4e. Documents, assets and expense claims

**Document vault** — *Documents*. Pick the employee, the document type (Aadhaar, PAN, offer
letter, contract, resume, other), attach the file and upload. Press **Verify** once you have
checked it. Employees can upload their own from *My documents*; you see everything.
Only PDF/JPG/PNG/WebP up to 8 MB are accepted — the server checks the file's contents, not
its name, so a renamed `.exe` is refused. **Delete** removes the file from disk as well as
the record; do that when someone leaves.

**Assets** — *Assets*. Add each item with a tag (`FITS-LAP-001`), name, category, serial
and cost. **Assign** it to a person; a second assignment is refused until it is returned.
Use the tag link to see the full assignment history. Employees see their own at *My assets*.

**Expense claims** — employees submit from *My expenses* with an optional receipt photo.
They arrive under *Expense claims*: **Approve** or **Reject**. Approved totals are shown at
the top so you know what to reimburse. Pay approved claims with the next payroll run.

## 4f. Field staff attendance (GPS)

Field staff cannot scan a gate. Instead:

1. **Set up the places** — *Field locations*. Add each site with a radius (200–300 m is
   sensible; too tight and GPS drift blocks honest people). "Use my current position" fills
   the coordinates from your own phone or laptop.
2. **The employee** opens *Field attendance* on their phone, allows location access, picks
   check-in or check-out, optionally adds a selfie, and presses **Mark attendance**.
3. **The server** measures the distance. Inside the radius → attendance is marked and tagged
   as a field check. Outside → nothing is marked, and the attempt is logged with the distance.
4. **Review** — the *Field locations* page lists today's checks with distance and result.

> Geo-fencing is a convenience control, not proof of presence: a phone's location can be
> faked. Every check stores coordinates, accuracy, distance and selfie so you can review
> anything suspicious.

## 5. Payroll

**Payroll → pick the month → Generate payroll**

Before you generate, close the month's exceptions: every unmarked working day in
the past becomes **absent**, which is charged as loss of pay. Check the
**Monthly register** first — that grid is the whole month at a glance
(P present · ½ half day · L leave · A absent · W week off · H holiday).

* **Generate payroll** computes one payslip per active employee:
  `per day = monthly gross ÷ basis days` (calendar days, or a fixed 26 — set in
  Settings), then `earned gross = gross − LOP days × per day`.
* **Adjust** any payslip for **bonus / incentive**, **salary advance recovered**
  and **other deduction**, plus a note that prints on the payslip. Net pay
  updates immediately.
* **Finalise** locks the numbers (no more recalculation) — do this once HR and
  the accountant agree.
* **Mark as paid** freezes the run for good. If you find a mistake after
  finalising but before paying, use **Reopen**.
* **⬇️ Bank CSV** exports one row per employee — the file an accountant or the
  bank upload needs.
* Each row has a **Payslip** link: a clean printable page (print or *Save as
  PDF*) with earnings, deductions, net pay and signature lines.

Repeat every month. Past runs stay in the list with their status, so you always
know which months are done.

---

## 6. Settings you will want to change in week one

**Settings → Attendance & payroll rules** (each row saves independently):

| Setting | What it does |
|---|---|
| Organisation name | shown in the header and on payslips |
| Shift start | scans after start + grace are marked **late** |
| Late grace (minutes) | how late is still "on time" (default 15) |
| Half-day threshold (minutes) | worked less than this on a full day → half day (default 240 = 4 h) |
| Payroll basis | calendar days in the month, or a fixed **26** |
| Paid leave accrual / month | 1.5 days by default; drives the leave balance |
| Weekly off day | 0 = Monday … 6 = Sunday (default Sunday) |
| Late marks per half-day LOP | e.g. 3 → every 3 late marks costs half a day. **0 disables it.** |
| Currency symbol | used on payroll screens and payslips |

Also on this page: **Logins** (add one per gate, change passwords, disable),
**Holidays**, and the **Face service** panel that tells you whether face
recognition is reachable.

---

## 7. Everyone leaves eventually

1. **Employees → (person) → Remove face enrolment** — deletes the biometric
   template.
2. **Mark inactive** — they can no longer scan and drop out of payroll runs.
3. Keep their attendance, leave and payslip history (needed for the final
   settlement and for statutory records).
4. Only use **Delete employee permanently** when you are sure: it removes all of
   their attendance, leave, salary revisions and payslips.

---

## 8. When something looks wrong

| Symptom | What to check |
|---|---|
| Someone's scan does not register | Are they **active**? Is a face enrolled (green badge)? Is today marked as leave? |
| A scan shows a different name | Two enrolments may hold the same face — re-enrol both people from fresh photos, and check the audit log for the similarity score |
| Times look wrong (04:30 instead of 10:00) | the server timezone (`HRMS_TZ`, default Asia/Kolkata) |
| Payroll shows more LOP days than expected | unmarked working days in the past count as absent — fix them on the Daily board and **Recalculate draft** |
| The gate says "Scanner offline" | the Face API service is down; HR can mark attendance manually meanwhile. Then call the administrator |
| Nothing at all loads | the server is down or the URL changed — try `/healthz` and call the administrator |
| "Page not found" | you may be signed in as **gate** on that device; the kiosk login only reaches the scanner |

Keep this rule in mind: **the gate is convenience, not proof.** If a scan is
disputed, the audit log and the manual-correction trail are the record of truth,
and HR has the final word.
