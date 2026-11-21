# Fakhri HRMS: Application Audit Report

**Date:** 20 Nov 2026
**Scope:** All of `app/`: security, attendance, leave, payroll, loans, F&F, and the UI.
**Baseline commit:** `2b4ef67` (main)

## How this was audited

- **Code review** of authentication, sessions and CSRF, route access checks, and the attendance, leave, payroll, loan and exit services. All templates and JS were reviewed too.
- **Page crawl:** every GET page (57 in all) was loaded through the test client with seeded demo data, as HR, as an employee and signed out. No page returned a 500 or showed raw values such as `None`, `undefined` or `nan`.
- **Scripts:** the most important logic and security findings were reproduced against a throwaway database. These are marked **Verified**.
- **Test suite:** `pytest` reports **3 failures** on `main` (see [§6](#6-failing-tests)).

Severity levels:
- **Critical:** a security hole or data-integrity risk that can be exploited now.
- **High:** a wrong money or leave-balance result in normal use.
- **Medium:** a workflow gap or a hardening issue.
- **UX:** a usability defect.

---

## Summary

| # | Severity | Area | Issue |
|---|---|---|---|
| C1 | Critical | Leave / HR UI | Stored XSS: the employee's leave reason is rendered as HTML in HR's browser |
| C2 | Critical | Kiosk | Remote employee can punch a colleague in, at a time they choose, through offline sync |
| C3 | Critical | Kiosk | The "scan your own face" check crashes with a 500 |
| H1 | High | Leave | Manager-routed requests skip the overlap check, so duplicate requests are accepted |
| H2 | High | Leave | Manager-routed requests skip the balance check, so leave goes past the balance |
| H3 | High | Payroll | Deductions added after the first run are dropped when the run is recalculated |
| H4 | High | Payroll / Loans | Loan EMI can make net pay negative |
| H5 | High | Payroll / Loans | Loan repayment is recorded even when HR removed the EMI from the slip |
| H6 | High | Payroll / Loans | Reopened run with a loan cannot be recalculated or deleted (FK crash) |
| H7 | High | Payroll | Retention set to 0% deducts 20%; 0 months becomes 3 |
| H8 | High | F&F | Outstanding loan not recovered; retention refund counts draft payslips |
| M1–M8 | Medium | Various | See [§3](#3-medium) |
| U1–U11 | UX | Various | See [§4](#4-uiux-issues) |

---

## 1. Critical

### C1. Stored XSS through the leave reason (employee → HR)

- **Where:** [app/templates/leave.html:765](app/templates/leave.html#L765). The data comes from `/leave/employee/{id}/history`, at [app/routes/leave.py:86](app/routes/leave.py#L86).
- **Problem:** the HR "Leave History" popup builds table rows with `innerHTML` and inserts `l.reason` without escaping. Employees write that reason themselves.
- **Impact:**
  - The CSP allows `'unsafe-inline'` scripts, so inline event handlers run.
  - A reason such as `<img src=x onerror="…">` runs as HR when HR opens the employee's history.
  - That script can read the CSRF token on the page and submit any HR form: create HR logins, change salaries, approve leave.
- **Fix:**
  - Build the cells with `textContent`, or escape every interpolated value. The same applies to `type_label`, `applied_label` and the attachment names.
  - Audit the other `innerHTML` sinks: [dashboard.html](app/templates/dashboard.html) (live scans, top performers), [employees.html:383](app/templates/employees.html#L383), [kiosk.js:189](app/static/js/kiosk.js#L189) and [app.js:549](app/static/js/app.js#L549).

### C2. Buddy punching and backdating through offline sync (Verified)

- **Where:** [app/routes/kiosk.py:418](app/routes/kiosk.py#L418) (`kiosk_offline_sync`)
- **Problem:**
  - An employee with `remote_work_enabled` may call this endpoint.
  - Unlike `/api/kiosk/scan`, it never checks that the recognised face belongs to the signed-in employee.
  - It also trusts the client-supplied `captured_at`, up to `kiosk_offline_max_hours` in the past.
- **Reproduced:** Remote employee "Ann" uploaded a photo of "Bob" with `captured_at` set to 3 hours earlier. The response was `200 {"action":"check_in"}`, and Bob's attendance row shows a check-in 3 hours in the past with `source=face_offline`.
- **Fix:**
  - Apply the same identity check as `/api/kiosk/scan`.
  - Better still, refuse offline sync for employee logins altogether. Offline sync exists for the shared gate device, where a client-supplied time is acceptable.

### C3. The identity-mismatch message crashes (Verified)

- **Where:** [app/routes/kiosk.py:358](app/routes/kiosk.py#L358). The same mistake is in `device_label_for`, at [kiosk.py:100](app/routes/kiosk.py).
- **Problem:**
  - The code reads `user.name`.
  - `SessionUser` is a `slots=True` dataclass with no `name` field, so this raises `AttributeError`.
- **Reproduced:** a remote employee scanning a colleague's face gets **HTTP 500** instead of the intended 403 "Please scan your own face" message.
- **Fix:** use `user.display_name`.

---

## 2. High: money and leave correctness

### H1. Duplicate leave requests when the employee has a manager (Verified)

- **Where:** `_check_overlap` in [app/services/leave.py:252](app/services/leave.py#L252)
- **Problem:**
  - The overlap query only looks at `status IN ('pending','approved')`.
  - Employees with a manager get `status='pending_manager'`, so their open requests are invisible to the check.
- **Reproduced:** three identical paid-leave requests for the same day were all accepted.
- **Fix:** include `'pending_manager'` in the overlap query.

### H2. Leave balance bypass through the manager route (Verified)

- **Where:**
  - [app/services/leave.py:358](app/services/leave.py#L358) (`approve_leave`)
  - `balance()` pending queries at [leave.py:700-775](app/services/leave.py#L700)
- **Problem:**
  - `balance()` subtracts only `status='pending'` requests, not `pending_manager`. So every new request is checked against the full balance.
  - `approve_leave` only re-checks the balance when `status == 'pending'`. When HR approves straight from `pending_manager`, the check is skipped entirely.
- **Reproduced:** with a paid-leave balance of 34.5 days, three requests of about 20 days each were accepted, and HR approved all three: about 60 days approved.
- **Fix:**
  - Count `pending_manager` as pending in `balance()`, and in the `ignore_pending_id` lookups in `_check_balance`.
  - Run the balance check in `approve_leave` for both `pending` and `pending_manager`.

### H3. Ledger deductions dropped when payroll is recalculated (Verified)

- **Where:** `_apply_adjustments` in [app/services/payroll.py:492](app/services/payroll.py#L492)
- **Problem:**
  - On recalculation, the `other_deduction` saved on the previous slip always overrides the freshly computed ledger total.
  - That saved value is never `None`, so the override always applies.
  - The note is recomputed, but the amount is not.
- **Reproduced:**
  1. Generate a draft run.
  2. Add a ₹1,500 "Damage" deduction for the same period.
  3. Recalculate.

  The slip shows `other_deduction = 0.0` alongside `other_deduction_note = 'Other: Damage'`.
- **Related:** the per-line notes HR types (`bonus_note`, `advance_note`, `tds_note`, etc.) are also lost on recalculation, because they are not in `ADJUSTMENT_FIELDS`.
- **Fix:**
  - Keep HR's manual "other deduction" separate from the ledger total, and always recompute the ledger part.
  - Carry the note fields forward on recalculation.

### H4. Loan EMI can make net pay negative (Verified)

- **Where:** [app/services/payroll.py:432](app/services/payroll.py#L432) (`generate_run`)
- **Problem:** the loan-EMI branch sets `advance = loan_emi` and recomputes totals directly. This bypasses the "recover only what this month can carry" cap in `_apply_adjustments`.
- **Reproduced:** ₹10,000 gross, 3 days worked, ₹20,000 EMI. Earned ₹2,258, net pay **−₹17,742**.
- **Fix:** cap the EMI at the pay still available after statutory and other deductions, and store the actual amount recovered.

### H5. Loan repayment recorded for an EMI that was never deducted (Verified)

- **Where:** [app/services/payroll.py:635](app/services/payroll.py#L635) (`set_run_status`, finalise branch)
- **Problem:**
  - Finalising records a repayment of `payslips.loan_emi`, the EMI the system computed.
  - It does not use `payslips.advance`, the amount actually deducted.
  - `update_slip` lets HR change the advance without updating `loan_emi`.
- **Reproduced:** HR set the advance to ₹0 and finalised. The loan still recorded a repayment of ₹10,000, and its remaining balance fell from ₹50,000 to ₹40,000.
- **Fix:** record the repayment from the deducted amount, capped by `loan_emi` and the remaining balance. Skip on-hold slips, and record the repayment when a slip is later released.

### H6. A reopened run with a loan cannot be recalculated or deleted (Verified)

- **Where:**
  - `reopen_run`, at [payroll.py:655](app/services/payroll.py#L655)
  - `delete_run`, at [payroll.py:679](app/services/payroll.py#L679)
  - `generate_run`, which deletes the old payslips first
- **Problem:**
  - `loan_repayments.payslip_id` references the payslips.
  - Reopening leaves the repayments in place, and never restores the loan balance.
  - When Recalculate or Delete later removes those payslips, the foreign key fails.
- **Reproduced:** finalise → reopen → recalculate raises `sqlite3.IntegrityError: FOREIGN KEY constraint failed` (HTTP 500). Delete fails the same way.
- **Impact:** HR cannot fix a mistake in any month that contained a loan EMI.
- **Fix:** in `reopen_run`, delete that run's `loan_repayments` and restore each loan's `total_repaid`, `remaining_balance` and `status`, all inside a single transaction.

### H7. A retention of 0% deducts 20% (Verified)

- **Where:** [app/services/payroll.py:300](app/services/payroll.py#L300) and [payroll.py:1024](app/services/payroll.py#L1024)
- **Problem:**
  - `float(employee.get("retention_percent") or 20.0)` treats 0 as "missing".
  - The same pattern turns `retention_months = 0` into 3.
- **Reproduced:** `retention_percent = 0.0` produced a ₹6,000 deduction on ₹30,000 gross.
- **Fix:** fall back to the default only when the value is `None`.

### H8. F&F settlement gaps

- **Where:** [app/services/exits.py:372-403](app/services/exits.py#L372)
- **Problems:**
  - The settlement has no deduction line for an outstanding loan or advance (`employee_loans.remaining_balance`). An employee can leave owing money, and the statement won't show it.
  - The retention refund sums `retention_amount` from **all** payslips, including draft runs that were never paid. It should only count finalised or paid runs.

---

## 3. Medium

| # | Issue | Where | Notes |
|---|---|---|---|
| M1 | Leave can get stuck waiting for a manager who doesn't exist or is inactive | [leave.py:190](app/services/leave.py#L190), [employees.py:439](app/services/employees.py#L439) | The manager route triggers whenever `manager_id` is set. Nothing checks that the manager exists or is active, and loops (A→B→A) are not blocked. Requests sit in `pending_manager` indefinitely. |
| M2 | "Your payslip is ready" is sent to on-hold slips | [routes/payroll.py:131](app/routes/payroll.py#L131) | The push and the WhatsApp PDF go to every slip in the run. Double-submitting Finalise sends them twice. |
| M3 | The backend allows draft → paid directly | `set_run_status`, [payroll.py:593](app/services/payroll.py#L593) | The run becomes permanently "paid" while its slips stay draft, and no loan repayments are recorded. The UI doesn't offer this, but the server accepts it. Allow only finalised → paid. |
| M4 | HR's rejection note overwrites the employee's original reason | [leave.py:404](app/services/leave.py#L404) | `reason = COALESCE(?, reason)`. Store the note in its own column. |
| M5 | Lockouts are shared across the whole company | [auth_routes.py:177](app/routes/auth_routes.py#L177), [passkeys.py:50](app/routes/passkeys.py#L50) | Face and passkey logins count failures under one shared key (`"face_login"` / `"passkey"`). Five failures by anyone lock face or passkey login for everyone for 10 minutes. Key the count by IP address instead. |
| M6 | Two simultaneous first scans crash | [attendance.py:653](app/services/attendance.py#L653) | Two scans in parallel can both see no row and both insert. The `UNIQUE(employee_id, work_date)` constraint then raises, giving a 500. Use `INSERT … ON CONFLICT`, or catch the error and re-read the row. |
| M7 | Leave attachments are handled unsafely | [me.py:400-415](app/routes/me.py#L400), [leave.py:346](app/routes/leave.py#L346) | Files are read fully into memory with `await upload.read()`, which ignores `read_capped`. Errors are swallowed with `except Exception: pass`. Files stay on disk when the leave request fails. The download only requires *any* signed-in user, the gate kiosk included. The names are random, but ownership or HR should still be checked. |
| M8 | Values are hard-coded instead of read from settings | [me.py:654](app/routes/me.py#L654), [notify.py:140,371,738](app/notify.py#L371), [leave.py:540](app/services/leave.py#L540) | The portal URL `https://hrms.fakhriitservices.com`, the address `hr@fakhriitservices.com` and the timezone `Asia/Kolkata` should come from `settings.public_base_url`, configuration and `settings.tz`. |

**Lower-priority hardening:**

- **Inconsistent CSRF:** `/api/kiosk/scan` has no CSRF check, while the offline-sync endpoint has one. `SameSite=Lax` cookies limit the exposure, but the two should match.
- **Kiosk login can fetch every face photo:** the kiosk role can download any employee's photo via `/employees/{id}/avatar`, so a stolen gate phone could scrape every face photo.
- **Punch photos are never deleted:** one JPEG is saved per scan, and `delete_employee` doesn't remove avatars, punch photos or leave attachments.
- **The kiosk spoof lockout isn't shared between workers:** it is kept in process memory, so each worker counts separately.
- **Expense receipts download without an extension:** the name is `receipt-12` ([records.py:363](app/routes/records.py)).

---

## 4. UI/UX issues

| # | Issue | Where | Suggested fix |
|---|---|---|---|
| U1 | **Leave attachment chips show blank text or "undefined".** The templates read `att.original_name`, but uploads are stored with the key `filename`. | `leave.html:57,197,757`, `me_leave.html:148`, `me_team.html:60` | Use `att.filename`. |
| U2 | **The upload box promises DOC/DOCX up to 10 MB.** The server accepts only PDF/JPG/PNG/WebP up to 8 MB and drops rejected files silently, so the employee believes the file was attached. | [me_leave.html:110](app/templates/me_leave.html#L110) | Match the `accept` list and the text to the server, and show a rejection error. |
| U3 | **Manager-stage requests look broken.** They show the raw text `pending_manager` in a grey chip and have **no Withdraw button**, which only renders for `pending`. The HR history popup also shows the raw status. | [me_leave.html:141,156](app/templates/me_leave.html#L156), `leave.html:751` | Add human labels ("Awaiting manager", "Awaiting HR") and allow withdrawing in both stages. |
| U4 | **The "My Team" menu item disappears on most portal pages.** `is_manager` is only passed on `/me` and `/me/team`. | [base.html:16](app/templates/base.html#L16), [me.py:163](app/routes/me.py#L163) | Work out `is_manager` in `page_context`. |
| U5 | **The "Org chart" is not a hierarchy.** It only groups people by department, but the README describes a reporting hierarchy. | [employees.py:160](app/routes/employees.py#L160), `org_chart.html` | Build the tree from `manager_id`, or rename the page. |
| U6 | **Confirmation dialogs are inconsistent.** Some actions use the browser's `prompt()`/`alert()` (leave rejection, putting payslips on hold, bulk uploads); others use the app's styled modals (manager rejection). | `leave.html:77`, `payroll.html:488,495`, `employees.html:363` | Use one styled modal component everywhere. |
| U7 | **The HR menu is long, and several items share an icon.** About 30 items. Payroll and Loans share the wallet icon; Deductions and Expenses share the receipt icon; LOP, Profile changes and HR Letters share one file icon. | [base.html:29-63](app/templates/base.html#L29) | Collapse sections and give each item its own icon. |
| U8 | **Remote and field staff have no quick punch on mobile.** Their most frequent action is not in the bottom tab bar. | [base.html:168](app/templates/base.html#L168) | Show a Punch tab when `remote_work_enabled` or self-punch is on. |
| U9 | **An accidental early check-out can't be corrected.** Once the minimum time has passed, a stray scan becomes the check-out, and any later scan only says "already complete". | [attendance.py:559](app/services/attendance.py#L559) | Let a later scan update the check-out, or ask for confirmation before an early check-out. |
| U10 | **The Ask HR drawer has accessibility gaps.** It has no `role="dialog"` or `aria-modal`, focus isn't kept inside it, and the ✕ button has no label. | [base.html:184-200](app/templates/base.html#L184) | Add the ARIA attributes, keep focus inside, and return focus to the button on close. |
| U11 | **Signing in again after the session expires drops the page's filters.** The redirect keeps only the path, not the query string. | [auth.py:231](app/auth.py#L231) | Include the query string in `next`. |

**Maintainability:** pages rely heavily on inline styling: 120 `style=` attributes in `leave.html`, 116 in `dashboard.html` and 109 in `letters_templates.html`. This makes visual consistency, theming and later restyling difficult. Moving it into `app.css` classes would help.

---

## 5. What looked solid

- **Passwords and sessions:** scrypt password hashing. Signed session cookies that can be revoked through `session_version`.
- **Login protection:** brute-force lockout on username and password login. Open-redirect protection on `next`.
- **Form protection:** route-level CSRF checks on every HTML form POST.
- **Employee data access:** payslips, documents, receipts and tax proofs check ownership correctly.
- **Uploads:** file types are checked by content, not extension, and stored under random names with path-traversal guards.
- **Deletion:** an employee who appears on finalised payslips cannot be deleted.
- **Rendering:** every page rendered without errors with demo data.

---

## 6. Failing tests

| Test | Cause |
|---|---|
| `tests/test_ess.py::test_employee_page_shows_the_portal_card` | The 360° profile redesign (commit `a911e42`) no longer renders the "Employee portal" card. |
| `tests/test_phase2.py::test_manager_edit_selection_and_contact_email_display` | The redesigned profile no longer shows the manager's email. |
| `tests/test_plan_v3.py::test_pay08_pf_and_esi_follow_earned_wages` | `statutory_deductions_enabled` now defaults to `"no"` ([payroll.py:190](app/services/payroll.py#L190)). The test needs to turn the setting on, or be updated to the new default. |

For the first two, decide whether the removal was intended. If it was, update the tests; if not, restore the elements.

---

## 7. Recommended fix order

1. **C1–C3:** the XSS hole and attendance integrity. These are small, isolated changes.
2. **H1–H2:** the leave overlap and balance checks.
3. **H3–H7:** payroll and loans. Add a regression test for each reproduction above.
4. **H8 and M1–M8.**
5. **The 3 failing tests.**
6. **UX:** U1–U4 first, since they are quick and affect users daily. Then U5–U11.

---

## 8. Resolution (fixed 20 Nov 2026)

All findings above are fixed in the working tree. The test suite now reports **619 passed, 0 failed** (previously 3 failures). Each reproduction from this report is a permanent regression test in [tests/test_audit_fixes.py](tests/test_audit_fixes.py), 28 tests in all.

| # | Fix |
|---|---|
| C1 | Global `escapeHtml()` in `base.html`, applied to every `innerHTML` sink: leave history, dashboard live scans, recognition cards and scorecard, bulk-import errors, kiosk card, toast, file lists. Jinja values inside inline handlers (`copyToClipboard`, `singleHoldPrompt`, letters JS) now use `\|tojson`. |
| C2 | Offline sync refuses employee logins; the offline queue is disabled on their scanner page. |
| C3 | `user.name` → `display_name`. Scan and challenge now require the CSRF token, and the scanner refreshes it from `/api/kiosk/status`. |
| H1/H2 | `pending_manager` counts in the overlap check, the balance and approval. Approval uses the **unclamped** balance. This also fixed an extra bug: approving a request when the balance was already negative used to pass. |
| H3 | New `other_override` / `advance_override` columns. Recalculation follows the ledger and loan EMI unless HR typed their own figure. Per-line notes survive recalculation. "Reset adjustments" no longer releases holds. |
| H4 | The loan EMI goes through the same cap as other recoveries, so net pay is never negative. Loans disbursed after the month end are ignored. |
| H5/H6 | Repayment = amount actually deducted (≤ EMI ≤ balance). Reopen, recalculate and delete reverse a run's repayments inside a transaction. Holding or releasing a slip in a finalised run reverses or books its EMI. |
| H7 | A retention of 0% or 0 months is respected. |
| H8 | F&F deducts the outstanding loan (minus EMIs a draft run will take) and books it on settlement. Retention refund counts finalised runs only. Claims already attached to a payroll run are not paid twice. |
| M1 | Leave skips an inactive or missing manager and goes to HR. Manager must exist and be active; reporting loops are refused. Creating an employee with an invalid manager no longer leaves a half-created record. |
| M2 | Payslip-ready alerts go only on a real draft → finalised change, and never to held slips. |
| M3 | Only finalised → paid; a slip can't be marked paid on its own. |
| M4 | HR's rejection note is stored in `decision_note`; the employee's reason is kept. Both notes are shown to the employee. |
| M5 | Face and passkey lockouts are keyed per IP. |
| M6 | A racing first scan replays against the winning row instead of returning a 500. |
| M7 | Leave attachments: size cap, type check with a visible error, at most 6 files, cleanup if the request fails. Download limited to HR, the applicant and their manager. |
| M8 | New `HRMS_HR_EMAIL` setting; portal URL from `public_base_url`; timezone from settings. |
| Hardening | The kiosk only sees photos of people who scanned today. Gate photos are pruned after 180 days in housekeeping. Deleting an employee removes face photos, gate snapshots and leave files. Receipts download with their extension. Avatars are served with the correct image type. |
| U1–U4 | Attachment names fixed; honest upload limits; readable statuses ("Awaiting manager", "Awaiting HR"); Withdraw available at both stages; "My Team" shown on every portal page. |
| U5 | Org chart has a **Reporting lines** tree (from `manager_id`) next to the department view. |
| U6 | A styled `askReason()` dialog replaces `prompt()` for leave rejection and payslip holds. |
| U7 | Distinct icons for Loans, Deductions, LOP, Profile changes and HR Letters. |
| U8 | Remote and field staff get a **Punch** tab on the mobile bar. |
| U9 | A later scan corrects a check-out made **before shift end**, restoring a half day caused by the early check-out but not one caused by late arrival. A check-out after shift end stays final. |
| U10 | Ask HR is an accessible dialog: role, labels, focus kept inside, focus returned on close. |
| U11 | Re-login keeps the page's query string. |

### Additional bugs found and fixed during the fix pass

1. **The approval balance check was wrong once a balance went negative.** It used the clamped balance (see H2 above).
2. **Regularisation approval waived the early-leave rule on a keyword.** If the employee's reason contained the words "early leave", approval set `early_leave_approved` with no fine and no half day. HR now ticks an explicit "Approved early leave" box instead.
3. **Email footers were never shown.** Every template's footer was silently dropped, including the OTP warning "Never share this code with anyone".
4. **The payroll Hold button broke for names with an apostrophe** (e.g. D'Souza).
5. **Amounts accepted NaN or infinity.** Every amount parser accepted `"nan"` / `"inf"`, which could corrupt payroll totals and loans. These are now rejected.
6. **Employees could stack loan requests.** A new request is now refused while an earlier one is still waiting for HR.
7. **Employees could "withdraw" requests that were already decided.** Withdrawing a rejected or cancelled request rewrote the history; now only open requests can be withdrawn.
8. **Sign-in could resolve to the wrong person.** When an email, code or phone was shared by two employees, sign-in and reset codes went to whoever matched first. Ambiguous identifiers now match nobody.
9. **The password-reset flow revealed which accounts exist.** Its error message differed for real accounts; it is now the same for every account.
10. **The live-scan board showed raw status codes** (`half_day`).
11. **CSV exports allowed spreadsheet formula injection.** An employee-supplied value such as `=HYPERLINK(...)` ran as a formula when HR opened the payroll, attendance, leave or employee CSVs in Excel. Exports now go through [app/services/csvsafe.py](app/services/csvsafe.py), which leaves real numbers untouched.
12. **The bulk-import salary history could block or alter real payroll.** The template's first column was the *current* month, so filling it in created a "paid" run that blocked this month's payroll. The import could also overwrite net pay on real finalised payslips. History now covers completed months only, and the import writes only into runs it created itself.
13. **Announcement emails inserted the notice text as raw HTML.** It is now escaped, keeping line breaks.

### Left as decisions for the team

- **Audit log retention:** `alerts._housekeeping` permanently deletes audit-log rows older than **120 days**. For payroll and HR records you may want a much longer period; change `AUDIT_LOG_RETENTION_DAYS`.
- **Kiosk spoof lockout:** it is kept per process in memory, which is fine for one worker. Move it to the database if you run several workers.
- **Inline styles:** the heavy inline styling in a few templates was left as is. It's a refactor, not a bug.

---

## 9. Follow-up: leave buckets, the "casual by default" issue, and crashes under load

Done 20 Nov 2026 in the order A → B → C. Each item was reproduced first, then re-tested after the fix.

### A. Leave saved as casual by default

**Cause:** the server always stored the type submitted (verified for all four types). The problem was in the employee leave form: clicking the "Family Emergency", "Personal Work" or "Event / Wedding" reason template silently switched the leave type to **Casual**, and "Medical" switched it to Sick, even if the employee had already picked Paid.

**Fix:**
- Templates now fill in the letter text only.
- The leave type dropdown starts empty and must be chosen, in both the employee and HR forms.
- The server refuses a request without a type, with a clear message instead of a raw 422 error.
- Marking a day as "Leave" on the attendance board without a type is refused; it used to be stored silently as **paid**.

### B. Leave balance checks

**Before:** the leave form checked the right bucket, but:
- Leave marked on the attendance board, register or profile skipped the balance completely: 13 days were paid with a casual quota of 2.
- The check used **today's** leave year. Next year's leave was either blocked by this year's usage, or unlimited: 24 sick days were accepted against a quota of 6.
- Leave crossing the year-end was charged entirely to the start year.
- The balance import matched columns by substring: "Em**pl**oyee Name" counted as the PL column.

**Fix:**
- `balance(..., cycle_on=)` computes the bucket for the leave year the leave falls in, counting days inside that year (`_bucket_days`, split at the boundary by working days).
- `check_request_balance` checks every leave year a request touches.
- Leave marked on the board creates an approved one-day request through `leave.book_hr_day`: it is checked against the bucket, with an HR "Allow leave beyond the balance" override, and released again when the day is changed.
- The balance import matches whole words, in any column order.
- The employee dropdown shows the days left in each bucket.

**After:**
- Next year: one 6-day request accepted, then refused.
- The 29 Mar–3 Apr leave is split 3 + 3 between the two years.
- Board leave: 2 accepted, then refused.
- A blank type is refused.
- Changing the day back returns it to the balance.
- Payroll pays only the booked leave.

### C. Crashes under continuous use

**Reproduced** on the real server with production settings (1 worker): when the Face API hangs, **every page took 20 s** and `/healthz` failed.

Causes:
- Gate scans held request threads for the full 20 s Face API timeout, with no limit on how many could wait.
- Bulk emails and WhatsApp messages were sent inside web requests.
- Supabase sync (**enabled in `.env.production`**) re-read every table into memory, and its change detection never worked: a fresh connection always reads the same `data_version`.

Fixes:
- **Face API back-pressure** ([app/face.py](app/face.py)):
  - at most 4 inference calls at once;
  - a scan waits at most 1.5 s for a slot, then gets "busy, scan again" (429);
  - scan timeout 8 s;
  - a circuit breaker (3 failures → 30 s pause, scans fail instantly).
  All configurable through `HRMS_FACE_*` settings.
- **Thread pool** raised to 100 (`HRMS_THREADPOOL_SIZE`); `/healthz` is async, so it never waits for a thread.
- **Notifications:** pages only queue messages and wake the worker (`notify.wake()`). The "send a test" button now sends exactly its own message; it used to send the oldest queued one.
- **Supabase:**
  - change detection uses one long-lived connection;
  - rows are streamed in 500-row batches;
  - unchanged tables are skipped;
  - only changed rows are sent (`updated_at` tables and append-only logs), with a full reconcile every 6 h;
  - the outbox and sign-in secrets are never mirrored;
  - failures back off up to 30 minutes.
- **Runtime:**
  - `MALLOC_ARENA_MAX=2`;
  - uvicorn `--limit-concurrency 200 --timeout-keep-alive 15`;
  - the container memory limit is 1G;
  - requests slower than 2 s are logged;
  - `/api/health` shows the Face API back-pressure state and the outbox size.

**Load test results** (80 employees, 1 worker):

| Scenario | Before | After |
|---|---|---|
| Face API hung: normal pages p95 | ~20 s | **0.05–0.27 s** |
| Face API hung: `/healthz` | failures, up to 9 s | **0.05 s, 0 failures** |
| Face API hung: gate scan | 20–40 s → 503 | **0.02 s** "not responding" |
| 50-gate rush: pages p95 | ~2 s | **0.06–0.28 s** |
| 4 gates scanning nonstop | — | all scans succeed in ~2 s |
| 10-minute soak (20 HR users, 8 gates, nonstop payroll recalculation) | — | **0 errors** in ~28,000 requests; `/healthz` 0.03 s with 0 failures; memory flat at ~170 MB (no leak), brief spikes to 427 MB |

Under that soak, `/leave` (p50 ~2.2 s) and the register (~1.1 s) queue behind the saturated single worker, but unloaded they render in 9 ms and 47 ms, so this is load, not slow code. Separately, the monthly register page is **1.5 MB of HTML** for 80 employees: worth slimming for phones later.

Memory measurements were taken on macOS, where `MALLOC_ARENA_MAX` has no effect. Confirm memory on the Linux server.

**Tests:** 631 passed. The new tests cover leave years, board leave, import columns, the Face API guard (busy reply, breaker, 4xx not counted), the async health check, and Supabase streaming / skip / incremental / change detection.
