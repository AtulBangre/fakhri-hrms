# Fakhri HRMS — Bug Resolution Plan (v3, full application audit)

> **Scope:** bugs and defects in features that already exist. New features and change
> requests are kept separately in the Appendix (from plan v2).
> **Method (2026-10-23):** read every service and route module, then reproduced each
> suspected bug with a script against a fresh database (the repro steps are written
> out under each item; turn them into the failing tests). Items marked **Reproduced** were demonstrated
> that way; items marked **Code review** were found by reading the code and still
> need a failing test written first.
> **Baseline:** `./.venv/bin/pytest -q` → 226 passed, 11 skipped.

> **Status (2026-10-24): all 45 bug IDs are fixed** on branch `fix/plan-v3-bugs`, one commit per step
> (§6). Every ID has a regression test in `tests/test_plan_v3.py` (or the existing suite where the
> behaviour changed). Full suite: 289 passed, 0 failed; site crawl (HR + employee): 0 broken pages.
> The Appendix change requests are **not** part of this work and remain open.

**Priority** — **P0** wrong money or a security hole · **P1** wrong data or a broken workflow · **P2** robustness, UX or hardening.

---

## 0. Before starting

- [x] Commit the current working tree as one checkpoint ("tax/TDS, regularization, exits, appraisals, UI overhaul").
- [x] Rule for every fix below: **first write a failing test that reproduces it, then fix it.** The repro steps are given per item.
- [x] One branch or PR per section (§1 Payroll, §2 Leave and attendance, §3 Security, …), so reviews stay small.

---

## Summary

| ID | Bug | Area | Priority | Evidence |
|---|---|---|---|---|
| PAY-01 | Half-day attendance (no leave) costs nothing — full salary paid | Payroll | **P0** | Reproduced |
| PAY-02 | Unpaid **half-day** leave costs nothing | Payroll | **P0** | Reproduced |
| PAY-03 | Mid-month joiners are paid for weekly offs/holidays before joining (₹13,000 instead of ₹11,000) | Payroll | **P0** | Reproduced |
| PAY-04 | Payroll can be generated for a **future** month at full salary | Payroll | **P0** | Reproduced |
| PAY-05 | CA TDS import reads `1,50,000` as **₹1** and reports success | Tax | **P0** | Reproduced |
| PAY-06 | Recalculating a draft run silently wipes bonus, advance, TDS override and notes | Payroll | **P0** | Reproduced |
| PAY-07 | A **paid** payroll run can be deleted | Payroll | **P0** | Reproduced |
| PAY-08 | PF/ESI are deducted in full in LOP months | Payroll | P1 | Code review |
| PAY-09 | Net pay can go negative | Payroll | P1 | Code review |
| PAY-10 | Attendance and leave can be changed inside a finalised or paid month — payslips go stale silently | Payroll / attendance | P1 | Reproduced |
| PAY-11 | Employees see and download **draft** payslips | ESS | P1 | Code review |
| PAY-12 | Mid-month salary revision pays the new salary for the whole month | Payroll | P2 | Code review |
| PAY-13 | Changing week-off or holiday settings rewrites past months retroactively | Payroll | P2 | Code review |
| PAY-14 | Approved expense claims are never reimbursed (no payroll line, no "mark paid") | Expenses | P1 | Code review |
| LV-01 | No leave balance check — balance reached −24 days | Leave | **P0** | Reproduced |
| LV-02 | Overlapping or duplicate leave accepted (12 approved leaves on one date) | Leave | P1 | Reproduced |
| LV-03 | Cancelling leave #N deletes attendance stamped by leave #N0–#N9 (`LIKE` prefix) | Leave | P1 | Reproduced |
| LV-04 | Leave on an already-worked day is skipped in attendance but still charged to the balance | Leave | P1 | Reproduced |
| LV-05 | Half-day leave and gate scans overwrite each other (a scan turns half-day leave into full "present") | Leave / attendance | P1 | Reproduced |
| LV-06 | Half-day leave on a weekly off or holiday is still charged 0.5 | Leave | P2 | Code review |
| LV-07 | Regularization or HR "mark present" on a leave day leaves the leave approved and charged | Leave / attendance | P2 | Code review |
| SEC-01 | Disabled or deleted logins keep working until the cookie expires (12 h HR, **30 days** kiosk) | Auth | **P0** | Reproduced |
| SEC-02 | Changing or resetting a password doesn't end existing sessions | Auth | **P0** | Reproduced |
| SEC-03 | Completing a password reset **re-enables a disabled account** | Auth | **P0** | Reproduced |
| SEC-04 | No login throttling or lockout (30 wrong passwords → all plain 401) | Auth | P1 | Reproduced |
| SEC-05 | Marking an employee inactive leaves their portal login active | Auth / employees | P1 | Reproduced |
| SEC-06 | Uploads are read fully into memory before the size check; no request-size limit | Uploads | P2 | Code review |
| SEC-07 | No security headers (CSP, X-Frame-Options, Referrer-Policy); `/api/docs` is public | App | P2 | Code review |
| EMP-01 | Changing an employee **code** breaks the gate scanner for that person (face template keyed by code) | Employees / kiosk | P1 | Reproduced |
| EMP-02 | Deleting a portal login that raised any request crashes with an FK error (500) | Settings | P1 | Reproduced |
| EMP-03 | Deleting an employee leaves their documents, receipts, selfies and **face template** behind | Employees | P1 | Reproduced (files) |
| EMP-04 | HR notification emails never go out — HR users have no email, and no screen sets one | Notifications | P1 | Code review |
| ATT-01 | Register CSV links break for departments containing `&` | Register | P1 | Reproduced |
| ATT-02 | Invalid month silently resets the register to the current month and drops the department | Register | P1 | Code review |
| ATT-03 | "Today" shows `A`/absent before the day has ended (register, dashboard, ESS) | Attendance | P1 | Code review |
| ATT-04 | Department spelling variants (`Sales` / `sales `) become separate departments | Employees | P2 | Code review |
| ATT-05 | Field "Check out" with no check-in silently records a check-in; GPS accuracy is ignored | Field | P2 | Code review |
| ATT-06 | Scans keep working after an exiting employee's last working day | Kiosk / exits | P2 | Code review |
| EXIT-01 | F&F pays only the LWD month — salary for months between the last payroll and the LWD is lost | Exits | P1 | Code review |
| EXIT-02 | A settled (inactive) employee can be paid twice if a later run uses "include inactive" | Exits | P2 | Code review |
| EXIT-03 | Settled employees can't see their own F&F (login disabled at settlement) | Exits / ESS | P2 | Code review |
| APR-01 | One exited employee's unfinished review blocks the cycle from ever closing | Appraisal | P2 | Code review |
| NTF-01 | The worker thread and manual "send now" can send the same message twice; no retry backoff | Notifications | P2 | Code review |
| NTF-02 | "Email payslips → only missing" matches any earlier email that mentions the month | Payroll email | P2 | Code review |
| MISC-01 | PDF footer and the `today_iso` template global use server-local `date.today()`, not the org timezone | Misc | P2 | Code review |

---

## 1. Payroll and money (P0 first)

### PAY-01 · Half-day attendance costs nothing — **P0**
- **Where:** `services/payroll.py::compute_slip`: `lop_days = absent + unpaid_leave + late_lop`. `half_days` only raises `present_days`; it never adds 0.5 LOP.
- **Repro:** mark a 2.5-hour day as `half_day`, everything else present → `lop_days = 0`, earned = full ₹30,000.
- **Fix:** in `employee_range_summary`, split half days into `half_worked_paid` (half day + paid leave type) and `half_unpaid` (no leave or unpaid leave). LOP += 0.5 × `half_unpaid`. The register totals (`payable_days`, `lop_days`) must use the same rule (see ATT-03 consistency).
- **Test:** 1 half day without leave → LOP 0.5; with paid half-day leave → LOP 0; with unpaid half-day leave → LOP 0.5.

### PAY-02 · Unpaid half-day leave costs nothing — **P0**
- **Where:** same summary: `unpaid_leave` is only counted when `status == 'leave'`, so a `half_day` row with `leave_type='unpaid'` is ignored.
- **Fix:** covered by the PAY-01 split. Add a dedicated test.

### PAY-03 · Joiners are paid for days before joining — **P0**
- **Where:** pre-joining days are counted as "absent" only on *working* days, so pre-joining weekly offs and holidays stay paid.
- **Repro:** join 20 Sep, present every working day after → earned ₹13,000; correct pro-rata is 11/30 × 30,000 = ₹11,000 (₹10,385 shown on the 26-day basis, also wrong).
- **Fix:** compute an *employed window* per slip: `max(month_start, joined_on) … min(month_end, last_working_day or month_end)`. Pay `per_day × payable days inside the window`. Days outside it are **not employed**: neither paid nor counted as absent. Apply the same window in the register (ATT-03) and F&F (EXIT-01).
- **Test:** joiner on the 20th of a 30-day month → 11/30 of gross; leaver on the 10th → 10/30.

### PAY-04 · Future-month payroll — **P0**
- **Where:** `generate_run` has no period check. Future days return zero counts, so LOP = 0.
- **Fix:** refuse `period > current period` in `generate_run`, `set_run_status`, the route and the CLI. The current month may be drafted but not finalised or paid before its last day (make that configurable).
- **Test:** generating next month raises `ValidationError`; finalising the current month before month-end raises.

### PAY-05 · CA TDS import mangles Indian number formatting — **P0**
- **Where:** `tax.import_tds_schedule` splits each line on `,` *before* parsing, so `BL1,1,50,000` becomes `tds="1"` (and taxable "50").
- **Fix:**
  - parse with `csv.reader` (handles quoted `"1,50,000"`);
  - reject lines with more than 3 columns ("Write amounts without commas or wrap them in quotes");
  - accept a tab-separated paste;
  - show a **preview with per-row errors before applying**, and apply nothing if any row is invalid.
- **Test:** `BL1,"1,50,000"` → 150000; `BL1,1,50,000` → rejected with a line error, nothing applied.

### PAY-06 · Recalculation wipes HR adjustments — **P0**
- **Where:** `generate_run` deletes every payslip in the draft run and re-inserts it, so bonus, advance, other deduction, TDS override and note are lost.
- **Fix:**
  - before deleting, read the adjustment columns per employee and re-apply them to the recomputed slip;
  - show "N payslips kept their adjustments" in the flash message;
  - add a "Reset adjustments" option for when HR really wants a clean slate.
- **Test:** add a ₹5,000 bonus, recalculate → bonus still ₹5,000 and net pay still includes it.

### PAY-07 · A paid run can be deleted — **P0**
- **Where:** `payroll.delete_run` has no status check; the template only hides the button.
- **Fix:** service-level guard — only `draft` runs can be deleted; finalised runs must be reopened first; paid runs never.
- **Test:** `delete_run` on a paid run raises; `POST /payroll/delete` returns an error flash.

### PAY-08 · PF/ESI ignore LOP — P1
- **Where:** `compute_slip` takes PF, ESI and PT as fixed amounts from `salary_revisions`.
- **Fix:** add `statutory_mode` to salary revisions:
  - `auto` (new hires): PF 12% of earned basic with a ₹15,000 ceiling; ESI 0.75% of earned gross while gross ≤ ₹21,000;
  - `fixed` (existing rows): today's amounts, pro-rated by `earned_gross / monthly_gross`.
  PT stays as the fixed slab.
- **Test:** 10 LOP days out of 30 → PF and ESI are ⅔ of normal.

### PAY-09 · Negative net pay — P1
- **Fix:** `update_slip` rejects adjustments that push net below 0 ("Deductions exceed earnings by ₹X"). `compute_slip` caps statutory deductions at earned gross. Round net to the rupee and store the rounding difference.

### PAY-10 · Locked months can still change — P1
- **Where:** `attendance.mark_manual`, `clear_day`, `leave.apply_leave`, `approve_leave`, `cancel_leave`, holiday add/delete and face/field marking never check the payroll run status. (Regularization already does.)
- **Fix:** one helper, `payroll.assert_period_open(storage, work_date)`, called by all of the above. Error message: "{Month} payroll is finalised — reopen it first." Paid months are read-only.
- **Test:** each entry point raises for a finalised month.

### PAY-11 · Draft payslips visible to employees — P1
- **Where:** `/me/payslips`, `/me/payslips/{id}`, its PDF, and the latest-payslip card on `/me` don't filter on run status.
- **Fix:** show employees only `finalized` and `paid` runs. Opening a draft slip by id returns "not found".

### PAY-12 · Mid-month salary revision — P2
- **Where:** `salary_for_period` takes the revision effective by month-end and applies it to the whole month.
- **Fix:** split the month at `effective_from`: per-day rates of the old and new salary for their own days. Document the rule in the HR guide.

### PAY-13 · Settings rewrite history — P2
- **Where:** week-off days, alternate Saturdays and holiday deletions are read live, so past months' "absent" days change after the fact.
- **Fix:** freeze calendar results into finalised payslips (they already store the numbers). Show a warning on the settings page when changing week-offs: "Affects draft and future months only; finalised months keep their figures." Block deleting a holiday inside a finalised month (PAY-10).

### PAY-14 · Approved expenses never paid — P1
- **Where:** claims stop at `approved`; `paid` is only set by F&F settlement.
- **Fix:**
  - add a `reimbursement` column on payslips;
  - `generate_run` adds approved, unpaid claims dated up to month-end, shows them as an earnings line (not taxable) and lists them on the payslip;
  - finalising marks those claims `paid` with the run id;
  - also add a manual "Mark paid" action for claims settled outside payroll.

---

## 2. Leave and attendance

### LV-01 · No balance check — **P0**
- **Repro:** one month of service plus a whole month of paid leave → balance −24; ESS can request more while negative.
- **Fix:**
  - `apply_leave` and `approve_leave` reject paid, sick and casual leave beyond `balance + pending` (pending ESS requests count against the available balance);
  - offer HR an explicit "allow negative balance" checkbox (audited);
  - the ESS form shows the available balance next to the type.

### LV-02 · Overlapping leave — P1
- **Fix:** reject a request if any `pending` or `approved` leave of the same employee overlaps the dates. Error: "You already have leave on {dates}."

### LV-03 · Cancel deletes other leaves' days — P1
- **Where:** `unstamp_attendance` uses `note LIKE 'leave #14%'`, which also matches `leave #140`.
- **Fix:** add a `leave_id` column to `attendance` (migration: backfill from the note) and delete by `leave_id`. Never match on the note text again.

### LV-04 · Worked days still charged — P1
- **Fix:** after stamping, set `leave_requests.days = len(applied)` (0.5 for half days). If every day was skipped, refuse the approval and tell HR why.

### LV-05 · Half-day leave vs gate scans — P1
- **Where:**
  - `mark_by_face` on a row with no check-in forces `status='present'` but keeps `leave_type`;
  - stamping half-day leave skips days that already have a check-in.
- **Fix:** model half-day leave explicitly. A half-day leave row keeps `status='half_day'` + `leave_type` even when a scan adds check-in/out times. A scan on such a row only records the times and never changes the status. Stamping onto a day with a check-in sets `half_day` + `leave_type` instead of skipping.

### LV-06 · Half-day leave on a weekly off — P2
- `_chargeable_days` returns 0.5 for any date. It should reject non-working dates ("{date} is a weekly off/holiday").

### LV-07 · Overwriting a leave day — P2
- When regularization or `mark_manual` replaces a row with `source='leave'`, shorten or cancel the linked leave (via `leave_id`, see LV-03) so the balance gets the day back. Ask HR to confirm in the UI.

### ATT-01 / ATT-02 · Register filters — P1
- Build CSV links with `urlencode`; keep the department on an invalid month; show an error flash.
- Add one `orgtime.parse_period()` helper (accepts `YYYY-MM`, `YYYY-M`, `MM/YYYY`) and use it on the register, payroll, ESS attendance and every CSV route.

### ATT-03 · "Today" counted as absent — P1
- **Where:** the register, `employee_range_summary` (`limit = min(end, today)`) and `effective_status` treat an unmarked *today* as absent.
- **Fix:** a day becomes absent only after it has passed (or after `shift_end` + grace on the day itself). Before that it is `·`, "not marked yet". Apply the PAY-03 employed window so pre-joining and post-LWD days show `–`. Add a test that the register's *Payable* and *LOP* equal the payslip's for every employee.

### ATT-04 · Department variants — P2
- Normalise on save (strip, collapse spaces, reuse the existing spelling of a case-insensitive match). Add a one-off migration to merge existing variants, and use `COLLATE NOCASE` in filters.

### ATT-05 · Field attendance — P2
- Honour `kind`: "Check out" without a check-in → error "You haven't checked in today."
- Reject readings with `accuracy_m` greater than the location radius ("GPS signal too weak — move outdoors and retry"); store the accuracy on the check.

### ATT-06 · Scans after the last working day — P2
- `mark_by_face` blocks employees whose approved exit has `last_working_day < today` ("Your last working day was {date}").

---

## 3. Security and access

### SEC-01 / SEC-02 · Stateless sessions can't be revoked — **P0**
- **Repro:** sign in, disable the login → `/me` still 200. Change the password → session still valid.
- **Fix:**
  - add `users.session_version` (int); put it in the cookie payload;
  - in `current_user`, load `active` and `session_version` for the uid (one indexed query, cacheable for a few seconds) and reject on mismatch or inactive;
  - bump `session_version` on password change or reset, disable, delete and role change;
  - add a "Sign out all devices" button on Settings → Logins;
  - shorten the kiosk default from 30 days to 7.

### SEC-03 · Reset re-enables disabled accounts — **P0**
- **Where:** `account.complete_reset` runs `UPDATE users SET active = 1`.
- **Fix:** remove that line. `complete_reset` must also refuse inactive users, and outstanding OTPs must be invalidated when an account is disabled.

### SEC-04 · No brute-force protection — P1
- Throttle per username and per IP (e.g. 5 failures in 10 minutes → 10-minute lockout with a generic message). Store attempts in a small `login_attempts` table. Audit lockouts.

### SEC-05 · Inactive employees keep their login — P1
- `employees.set_status('inactive')` also disables the linked user and bumps `session_version`. Reactivating asks HR whether to re-enable the portal login.

### SEC-06 · Upload size — P2
- Stream uploads with a hard limit: read in chunks, abort above 8 MB. Set a global request-body limit in the reverse proxy (`deploy/Caddyfile`: `request_body max_size 10MB`) and document it in `docs/DEPLOY.md`.

### SEC-07 · Headers and docs — P2
- Add middleware for `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and a CSP that allows only self + Google Fonts.
- Serve `/api/docs` only to HR sessions, or disable it in production (`HRMS_API_DOCS=0`).

---

## 4. Employees, settings and data integrity

### EMP-01 · Code change orphans the face template — P1
- **Fix (preferred):** enrol faces with a stable `external_id` = `emp-{id}` instead of the editable code. The kiosk resolves by id, falling back to code for legacy templates.
  - Write a migration that re-keys existing templates (or flags them for re-enrolment).
  - Until then, block code edits for enrolled employees: "Unenrol the face first."

### EMP-02 · Deleting a login crashes — P1
- **Where:** `leave_requests.applied_by` and `profile_change_requests.requested_by` reference `users(id)` without `ON DELETE`, so `delete_user` fails with `FOREIGN KEY constraint failed` (500).
- **Fix:** catch it and suggest **Disable** instead. Long term, rebuild those FKs with `ON DELETE SET NULL` in `_migrate` (the users-table rebuild pattern already exists).

### EMP-03 · Deleting an employee leaves data behind — P1
- `delete_employee` must also:
  - delete the files referenced by `documents`, `expense_claims.receipt_name`, `field_checks.selfie_name` and `tax_declarations.proof_name`;
  - delete the face template via the Face API (best effort, with a log line);
  - refuse when the employee appears in a finalised or paid payroll run (statutory records) — suggest "Mark inactive" instead.

### EMP-04 · HR never receives notifications — P1
- Add an **Email** field to Settings → Logins (create and edit) for HR users, and use it for leave, profile-change, regularization and resignation alerts.
- Show a warning on the dashboard if no HR user has an email address.

---

## 5. Exits, appraisals, notifications

### EXIT-01 · Unpaid months before the LWD — P1
- `compute_fnf` adds a salary line for **every** month from the first month without a payslip up to the LWD month, using the PAY-03 employed window for the last month.

### EXIT-02 · Double payment risk — P2
- `generate_run` skips employees with a `settled` exit whose LWD is before the period, even with `include_inactive`.

### EXIT-03 · F&F visibility — P2
- At settlement, email the employee the F&F statement as a PDF, instead of relying on a portal they can no longer open.

### APR-01 · Cycle can't close — P2
- Exits settled during an open cycle mark that appraisal `withdrawn` (excluded from close checks). HR can also mark a review "not applicable".

### NTF-01 · Duplicate sends — P2
- Claim rows atomically before sending (`UPDATE … SET status='sending' WHERE id=? AND status='pending'`), and send only if a row was updated.
- Exponential backoff (1, 5, 30 min) using a `next_attempt_at` column.
- Document that the app must run as a single worker process (or move the worker out of the web process).

### NTF-02 · "Only missing" payslip email — P2
- Match on `meta.slip_id` only; drop the `body LIKE '%{month}%'` clause.

### MISC-01 · Timezone — P2
- Use `orgtime.today(settings)` in `pdf.py`; remove the unused `today_iso` template global.

---

## 6. Order of work

| Step | Items | Why |
|---|---|---|
| 1 | SEC-01, SEC-02, SEC-03 | Live security holes; small, isolated changes in `auth.py` / `account.py` |
| 2 | PAY-04, PAY-07, PAY-06, PAY-05 | Stop wrong or irreversible money actions; each is a few lines plus a test |
| 3 | PAY-01, PAY-02, PAY-03 + ATT-03 | One shared change: the employed window and half-day LOP rules in the summary, the register and payroll together |
| 4 | LV-01 … LV-05 (+ `attendance.leave_id` migration) | Leave correctness, which payroll depends on |
| 5 | PAY-10, PAY-11, PAY-14, PAY-08, PAY-09 | Period locking, ESS visibility, reimbursements, statutory maths |
| 6 | SEC-04, SEC-05, EMP-01 … EMP-04, ATT-01, ATT-02, EXIT-01 | Workflow breakages |
| 7 | All P2 items | Hardening and polish |

Each step ends with: full pytest green, the full-site crawl (HR and employee) with 0 broken pages, and `docs/HR_GUIDE.md` updated wherever a rule changed.

---

## 7. Verification checklist

**Money**
- [x] Half day (no leave or unpaid leave) = 0.5 LOP; paid half-day leave = 0 LOP.
- [x] Joiner on the 20th → 11/30 of gross; leaver on the 10th → 10/30.
- [x] Future month can't be generated; current month can't be finalised early; paid runs can't be deleted.
- [x] Recalculating keeps bonus, advance, TDS override and notes.
- [x] TDS import of `"1,50,000"` = 150000; unquoted `1,50,000` is rejected with nothing applied.
- [x] PF and ESI scale with LOP; net pay never negative; approved expenses appear on the next payslip and are marked paid at finalisation.
- [x] Register *Payable* and *LOP* equal the payslip for every employee.

**Leave and attendance**
- [x] Leave beyond balance refused (unless HR overrides); overlapping leave refused.
- [x] Cancelling leave #14 never touches leave #140's days.
- [x] Half-day leave plus a gate scan stays a paid half day.
- [x] Nothing in a finalised month can be changed until the run is reopened.

**Security**
- [x] Disabling a login, changing its password or marking the employee inactive ends existing sessions immediately.
- [x] Password reset never re-enables a disabled account.
- [x] The 6th wrong password within 10 minutes is locked out.

**Data**
- [x] Changing an employee code doesn't break face recognition.
- [x] Deleting a login with history shows a friendly error, not a 500.
- [x] Deleting an employee removes their files and face template.
- [x] HR receives leave, profile, regularization and resignation emails.

**Regression**
- [x] `./.venv/bin/pytest -q` 100% green, with a new test per bug ID (name tests `test_<id>_…`).

---

## Appendix — change requests carried over from plan v2 (not bugs)

| ID | Request | Status / note |
|---|---|---|
| A1 | Remove per-row "Fix" on `/me/attendance` | Decision needed: (1) replace with one "Request correction" header button (recommended) or (2) remove regularization entirely. Removing "Fix" alone orphans the HR queue. |
| A2 | Show attendance source in the Note column | Map `face` → Gate scanner · device, `field` → Field (GPS), `manual` → Marked by HR, `manual` + `Regularized:` → Corrected on request, `leave` → Leave (there is no `kiosk` value). |
| A3 | Documents inside `/me/profile` | Move the list and upload form; `GET /me/documents` redirects; keep `POST /me/documents`. |
| C1 | Bank and personal fields on `/employees/new` | `create_employee` ignores them today; validate PAN/IFSC; store only Aadhaar last 4. |
| C2 | KYC uploads with "Skip" on onboarding | Create the employee first; partial upload failures must not lose the form. |
| C3 | Login and welcome email at onboarding | The `welcome` template exists but is unused. Send a one-time set-password link, not a plaintext password; add a `/me/password` change page (none exists today). |
| D3 | `tds` on `salary_revisions` | Dropped — TDS comes from the FY tax profile; a second source would conflict. |
