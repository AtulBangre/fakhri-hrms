# Fakhri HRMS: Second Audit (Bugs, Missing Features and the SaaS Plan)

**Date:** 21 Nov 2026 · **Revision 2**
**Baseline commit:** `1fd083d` ("application audit fixes…")
**Scope:** the whole application, plus the multi-tenant SaaS design in [plan.md](plan.md).

## How this audit was done

- **Code review** of modules not covered in depth in [report.md](report.md): F&F (gratuity, encashment), income tax, statutory exports (EPFO ECR, ESIC), letters and PDFs, scheduled alerts, appraisals, manager flows, field check-in, night shifts, the CLI and the deployment files.
- **Scripts:** bugs marked **Verified (run)** were reproduced against a throwaway database, and the observed output is quoted. **Code-read** means confirmed by reading the code, without running it.
- **Starting point:** the test suite passes: **632 passed, 0 failed**. The fixes from report.md sections 1–9 are in place and are not re-reported.

Severity:
- **Critical:** wrong statutory or legal output, a leaked secret, or data loss.
- **High:** wrong money or attendance in normal use.
- **Medium:** a broken feature, wrong behaviour in less common cases, or a hardening gap.
- **Low:** polish.

Effort:
- **S:** under half a day.
- **M:** 1–2 days.
- **L:** 3 days or more.

### What changed in revision 2

| Change | Details |
|---|---|
| Re-verified by running | B1, B2, B9, B10, B13 and B14 were only code-read in revision 1; all six now reproduced. B2 turned out worse than described. |
| Corrected | B7 figures now include the 4% cess. F22 wrongly said expense categories were free text: they are a fixed list, and only the limits are missing. |
| New findings | B17–B23: ESIC days, ESIC declaring contributions never deducted, the hard-coded WhatsApp gateway key, login-page disclosure plus plaintext credential files, field punches trusting GPS, F&F on gross when no basic is set, PL accruing during LOP. |
| Added | Effort, a fix outline and an **acceptance test** for every bug, so each fix can be checked. |

---

## Summary

| # | Sev. | Area | Issue | Evidence | Effort |
|---|---|---|---|---|---|
| B1 | **Critical** | EPFO ECR | Declares 12% PF for employees with **no PF deducted** | Verified (run) | S |
| B2 | **Critical** | EPFO ECR | EPF wages from **gross**, not basic; the line contradicts itself | Verified (run) | M |
| B17 | **Critical** | ESIC return | Declares wages for employees with **no ESI deducted**, including above the ₹21k ceiling | Verified (run) | S |
| B19 | **Critical** | Security | WhatsApp gateway (WAHA) API key **hard-coded and committed** | Code-read | S |
| B3 | **High** | Attendance | Night shifts: the morning scan becomes a new check-in | Verified (run) | L |
| B4 | **High** | F&F | Encashment subtracts **sick and casual** days from PL | Verified (run) | S |
| B5 | **High** | F&F | Fixed PL mode: a leaver encashes the **full year's** quota | Verified (run) | S |
| B6 | **High** | Payroll | Back-dated raises into finalised months never pay **arrears** | Verified (run) | M |
| B7 | **High** | Income tax | No 87A **marginal relief**: up to ₹53k a year over-deducted | Verified (run) | S |
| B8 | **High** | Letters | Letter PDF **crashes** on `₹`, `—`, curly quotes or Hindi | Verified (run) | S–M |
| B18 | **High** | ESIC return | "Days worked" excludes paid weekly offs and holidays (27 of 31 reported) | Verified (run) | S |
| B22 | **High** | F&F | Gratuity and encashment use **full gross** when no basic is set (the common case) | Code-read | S |
| B9 | Medium | Payslips | Non-Latin names print as `????` | Verified (run) | M |
| B10 | Medium | Income tax | Old regime: payroll PF not counted towards 80C | Verified (run) | S |
| B11 | Medium | Alerts | Missed-punch reminders ignore custom shift times | Code-read | S |
| B12 | Medium | CLI | `production-reset --erase-all-data` can never run | Verified (run) | S |
| B13 | Medium | Payroll | Inactive without an exit means no salary for the partial last month | Verified (run) | S |
| B20 | Medium | Security | Login page reveals the HR username; plaintext credential files kept forever | Code-read | S |
| B21 | Medium | Attendance integrity | Field punches trust browser GPS; the selfie is optional and never face-matched | Code-read | M |
| B23 | Medium | Leave | PL keeps accruing through long unpaid (LOP) absences | Code-read | S |
| B14 | Low | Info disclosure | Public `/api/health` shows the internal Face API URL and the outbox size | Verified (run) | S |
| B15 | Low | Fairness | Recognition score gives kiosk-only points | Code-read | S |
| B16 | Low | Performance | Monthly register page is ~1.5 MB | Measured | M |
| P1–P12 | — | SaaS plan | Not started; unsafe assumptions | See [§2](#2-audit-of-the-saas-plan-planmd) | — |
| F1–F40 | — | Product | Missing features | See [§3](#3-missing-features) | — |

---

## 1. Bugs

### B1. EPFO ECR declares PF that was never deducted (Critical · S)

- **Where:** [payroll.py:1067](app/services/payroll.py#L1067): `ee_share = int(round(pf)) if pf > 0 else int(round(epf_wages * 0.12))`
- **Reproduced:** with statutory deductions **off** (the default since `a911e42`), the payslip shows PF = 0 and ESI = 0. The ECR line is:
  ```
  100200300400#~#Statutory Off#~#18000#~#15000#~#15000#~#15000#~#1800#~#1250#~#550#~#0#~#0
  ```
  It declares EE ₹1,800, EPS ₹1,250 and ER ₹550, none of which was withheld.
- **Impact:** uploading this file to the EPFO portal creates a liability for every employee with a UAN.
- **Fix:** take the shares only from the PF on the slip. If it is 0, leave the member out, and list the skipped members in the export summary.
- **Acceptance:** a slip with PF = 0 produces no ECR line; a slip with PF = 1,080 produces EE = 1,080.

### B2. ECR EPF wages come from gross; the line contradicts itself (Critical · M)

- **Where:** [payroll.py:1064](app/services/payroll.py#L1064): `epf_wages = min(earned_gross, 15000)`
- **Reproduced** (auto mode, basic 9,000, gross 18,000): the payslip PF is **₹1,080** (12% of basic). The ECR line is:
  ```
  …#~#18000#~#15000#~#15000#~#15000#~#1080#~#750#~#330#~#0#~#0
  ```
  It shows EPF wages of 15,000 next to an EE share of 1,080. 12% of 15,000 is 1,800, so the portal validation fails or the file is wrong.
- **Missing:** members aged 58 and over (EPS must be 0); the higher-wage / voluntary option.
- **Fix:** save the earned basic + DA on each payslip, and derive EPF, EPS and EDLI wages from it, capped at ₹15,000 unless higher-wage applies.
- **Acceptance:** for every line, `EE share = round(12% × EPF wages)` and `EPS + ER = EE`.

### B17. ESIC return declares wages where no ESI was deducted (Critical · S, new)

- **Where:** `export_esic_return` ([payroll.py:1093](app/services/payroll.py#L1093)) includes anyone with an IP number, whatever was deducted.
- **Reproduced:** with statutory deductions off, both employees with an IP number were exported. One of them earns **₹35,000** (above the ₹21,000 coverage ceiling):
  ```
  3100999999,Above ESI ceiling,27,35000.00,,
  3100200300,Statutory Off,27,18000.00,,
  ```
- **Impact:** the ESIC portal computes 0.75% + 3.25% on these wages, a liability for contributions that were never collected, plus coverage claimed for someone who isn't covered.
- **Fix:** export only slips with ESI > 0, or out-of-coverage rows with zero wages and the correct reason code. Flag slips that have ESI but no IP number, because the portal rejects them.
- **Acceptance:** a gross of ₹35,000 or a slip with ESI = 0 produces no wage line.

### B18. ESIC "days" leaves out paid weekly offs and holidays (High · S, new)

- **Where:** `worked_days = present + paid_leave` ([payroll.py:1115](app/services/payroll.py#L1115)).
- **Reproduced:** an employee present on every working day of October 2026 was reported with **27** days. Wages were paid for 31.
- **Impact:** ESIC benefit eligibility depends on days for which contributions were paid. Under-reporting can deny employees sickness benefit.
- **Fix:** report days for which wages were paid: the employed days minus LOP days.
- **Acceptance:** a full month with no LOP reports the number of calendar days.

### B19. WAHA (WhatsApp gateway) API key is hard-coded and committed (Critical · S, new)

- **Where:**
  - [docker-compose.yml:62](docker-compose.yml#L62) and [docker-compose.yml:95](docker-compose.yml#L95): `fakhri_hrms_waha_secret`;
  - also the code default in [config.py:143](app/config.py#L143).
- **Impact:** WAHA controls the company's WhatsApp account. The compose file doesn't publish its port, but every deployment that doesn't override the value uses a key that is public in the repo. Anyone who can reach WAHA, through a mis-set `HRMS_WAHA_URL`, another host on the network, or a later port mapping, can read chats and send messages as the company.
- **Fix:**
  - Move the key to `.env`, required and with no default.
  - Rotate the key on the running WAHA.
  - Fail at startup if it equals the old value.
- **Acceptance:** `grep fakhri_hrms_waha_secret` finds nothing in the repo, and the app refuses to start without `HRMS_WAHA_API_KEY` when the WhatsApp provider is `waha`.

### B3. Night shifts are not supported (High · L)

- **Where:** `mark_by_face` uses the scan's calendar date as the work day ([attendance.py:469](app/services/attendance.py#L469)).
- **Reproduced** with a 22:00–06:00 shift:
  - 21:55 on 24 Nov → check-in for 24 Nov.
  - 06:05 on 25 Nov → **a new check-in for 25 Nov**.
  - Result: 24 Nov has no check-out, and 25 Nov shows a phantom present day.
- **Also wrong for these shifts:** worked minutes, half-day rules, late marks, reminders and payroll.
- **Fix:** a "work date" resolver. A scan before shift end + buffer on D+1 closes an open D-day row of a shift that crosses midnight. Late, half-day, reminder and register logic all use shift-relative times.
- **Acceptance:** a 22:00 in / 06:00 out pair creates one row dated 24 Nov with 480 worked minutes.

### B4. Encashment subtracts sick and casual days from PL (High · S)

- **Where:**
  - `balance()` returns `"balance" = accrued_PL − (used_PL + used_SL + used_CL)` ([leave.py:920](app/services/leave.py#L920));
  - F&F encashes it ([exits.py:351](app/services/exits.py#L351)).
- **Reproduced:** after 3 approved sick days, PL available stayed **34.5**, but the encashable balance dropped to **31.5**.
- **Fix:** encash `accrued_PL − used_PL − pending_PL` according to policy.
- **Acceptance:** SL and CL usage does not change the F&F encashment line.

### B5. Fixed PL mode lets a leaver encash the full year's quota (High · S)

- **Where:** fixed mode credits `annual_pl + carried_forward` on day 1 of the leave year ([leave.py:810](app/services/leave.py#L810)).
- **Reproduced:** quota 18, leaving on 10 April → **18 days** encashable.
- **Fix:** pro-rate the year's quota by months served in the leave year for encashment, using a "pro-rate on exit" setting (on by default).
- **Acceptance:** leaving on 10 April with an 18-day quota encashes at most 1.5 days plus carry-forward.

### B6. Back-dated salary revisions never pay arrears (High · M)

- **Where:** `set_salary` ([employees.py:632](app/services/employees.py#L632)) accepts any date. The employee form offers a "Retroactive (previous month)" preset that "applies retroactively" ([employee_form.html:445](app/templates/employee_form.html#L445)).
- **Reproduced:** October was finalised at ₹30,000, then a raise to ₹40,000 effective 1 Oct was saved. October stays 30,000, November pays 40,000, and there is **no arrears line**: ₹10,000 is never paid.
- **Fix:** when a revision reaches into finalised months, compute the difference per month and add an **Arrears** earning to the next draft run, with PF/ESI/TDS applied. Change the form text to say arrears will be added.
- **Acceptance:** the scenario above shows "Arrears (Oct 2026) ₹10,000" on November's draft.

### B7. No section 87A marginal relief in the new regime (High · S)

- **Where:** `rebate = rebate_amount if taxable <= rebate_cap else 0` ([tax.py:417](app/services/tax.py#L417)).
- **Reproduced** (tax including 4% cess):

| Gross | Taxable | App's tax | Correct (with marginal relief) | Over-deducted per year |
|---|---|---|---|---|
| ₹12.75 L | ₹12.00 L | ₹0 | ₹0 | — |
| ₹12.85 L | ₹12.10 L | **₹63,960** | ₹10,400 | **₹53,560** |
| ₹13.00 L | ₹12.25 L | **₹66,300** | ₹26,000 | **₹40,300** |

- **Fix:** if taxable income is above ₹12 L and slab tax exceeds `taxable − 12,00,000`, cap the tax at that difference (before cess).
- **Acceptance:** boundary tests at 12.00 L, 12.10 L, 12.25 L and 12.75 L (where relief stops applying).

### B8. Letter PDFs crash on common characters (High · S–M)

- **Where:** `render_letter_pdf` ([letters.py:246](app/services/letters.py#L246)) uses fpdf2 core fonts without the payslip sanitiser.
- **Reproduced:**
  ```
  FPDFUnicodeEncodingException: Character "—" … outside the range of characters supported by the font
  ```
  The same happens for `₹30,000`, curly quotes (normal in text pasted from Word) and Hindi.
- **Fix (S):** pass letter text through `pdf._text()` now.
- **Fix (M):** bundle Noto Sans and Noto Sans Devanagari and `add_font()` them for letters and payslips. This also fixes B9.
- **Acceptance:** a letter with `₹`, `—`, `“ ”` and Devanagari renders and keeps those characters.

### B22. F&F gratuity and encashment on full gross when no basic is set (High · S, new)

- **Where:** `basic = float(salary.get("basic") or 0) or gross` ([exits.py:294](app/services/exits.py#L294)).
- **Problem:** most employees here are created with only a monthly gross: basic is 0 unless HR splits it. Then gratuity (`15/26 × basic × years`) and encashment (`basic ÷ 30`) are calculated on the **whole gross**, typically about twice the correct amount. The statement shows a small note, but the totals are wrong.
- **Fix:** refuse to settle until a basic is set, or use a configurable default basic share (commonly 50% of gross), and show it prominently on the statement.
- **Acceptance:** an employee with gross ₹30,000 and no basic split gets gratuity on ₹15,000 (with the 50% default), and the statement says so.

### B9. Non-Latin names print as `????` on payslips (Medium · M)

- **Reproduced:** `_text("राम कुमार")` returns `'??? ?????'` ([pdf.py `_text`](app/pdf.py)).
- **Fix:** same as B8 (Unicode font).

### B10. Old regime ignores payroll PF for 80C (Medium · S)

- **Where:** 80C comes only from declarations ([tax.py:380](app/services/tax.py#L380)); the projection never reads the PF on payslips.
- **Fix:** add the year's employee PF (actual months plus projected months) to 80C, within the ₹1.5 L cap.
- **Acceptance:** an old-regime employee with ₹1,800/month PF and no declarations gets 80C = ₹21,600.

### B11. Missed-punch reminders ignore custom shift times (Medium · S)

- **Where:** `_attendance_reminders` ([alerts.py:138](app/services/alerts.py#L138)).
- **Problem:** it groups by `shift_id`, times everyone from `members[0]`, and doesn't load `custom_shift_start` / `custom_shift_end` / `custom_grace_minutes`.
- **Fix:** select the custom columns, and group by the effective `(start, end, grace)`.
- **Acceptance:** an employee with a custom 11:00 start is reminded after 11:00 + grace, not 09:30 + grace.

### B12. `production-reset` can never run from the CLI (Medium · S)

- **Reproduced:** `python cli.py production-reset --erase-all-data` → `error: unrecognized arguments: --erase-all-data` ([cli.py:471](cli.py#L471)).
- **Fix:** declare the flag on the sub-parser and pass it through. Keep the 12-character password check.

### B13. Inactive without an exit means no salary for the partial last month (Medium · S)

- **Reproduced:** an employee worked 10 days in October, was set to inactive (no exit record), then October payroll was generated: **0 payslips** for them.
- **Fix:** include everyone employed during the month (joined on or before the month end, and active, or inactive with no settled exit), and flag them on the payroll screen.

### B20. Login page reveals the HR username; plaintext credential files (Medium · S, new)

- **Where:** while `data/initial_credentials.txt` exists, the **public login page** says to "sign in as `hr`" and names the file ([login.html:137](app/templates/login.html#L137)). Nothing ever deletes the file ([auth.py:520](app/auth.py#L520)), and it holds the first HR and kiosk passwords in plain text.
- **Also:** the local `data/` folder holds a legacy `employee_portal_logins.txt` with portal passwords in plain text. Current code doesn't write it; it is gitignored, so it isn't in the repo.
- **Fix:**
  - Delete the credentials file once the HR password is changed, or after the first successful HR login.
  - Show the hint only to requests from localhost.
  - Remove the legacy file from every server.
- **Acceptance:** after the first HR password change, the file is gone and the login page shows no hint.

### B21. Field punches trust browser GPS; the selfie is never checked (Medium · M, new)

- **Where:** `field_service.check_in` ([field.py](app/services/field.py)) marks attendance from client-reported latitude, longitude and accuracy.
- **Problem:** the selfie is optional and never compared with the enrolled face. Weak accuracy is rejected only in `geofence_only` mode. Mock-location apps make this easy to fake.
- **Fix:**
  - Optionally require a selfie, and run it through the same Face API identity check as the kiosk.
  - Reject readings whose accuracy is worse than the fence in every mode.
  - Report repeated identical coordinates.
- **Acceptance:** with "require selfie" on, a selfie of a different person or no face is refused.

### B23. PL keeps accruing through long unpaid absences (Medium · S, new)

- **Where:** accrual mode counts calendar months since joining ([leave.py](app/services/leave.py) `balance()`), with no reduction for LOP or unpaid leave months.
- **Problem:** an employee on two months of unpaid leave still earns 3 PL days. Most leave policies (and the Factories Act basis of 1 day per 20 days worked) don't allow that.
- **Fix:** a policy setting, "no accrual in months with more than N LOP days", or accrue per paid day.

### B14. Public `/api/health` reveals internal details (Low · S)

- **Reproduced** while signed out:
  ```
  {'org': 'Fakhri IT Services', 'face_service': {'ok': False, 'detail': 'cannot reach http://127.0.0.1:8000: ConnectError'},
   'face_backpressure': {...}, 'outbox_pending': 0}
  ```
- **Note:** the last two fields were added in the previous round, an oversight on my part.
- **Fix:** keep `/healthz` public, and require a session for the details in `/api/health` (anonymous callers get `status` only).

### B15. Recognition score gives points only for kiosk scans (Low · S)

- **Where:** 5 of the 100 points come from the share of kiosk scans ([recognition.py:213](app/services/recognition.py#L213)), so field and remote staff can't reach the top score.
- **Fix:** count any verified punch.

### B16. Monthly register page is about 1.5 MB (Low · M)

- **Measured:** 1,579 KB of HTML for 80 employees; the server takes 47 ms, so the cost is on the phone.
- **Fix:** send cell details as compact JSON, or paginate by department.

---

## 2. Audit of the SaaS plan (`plan.md`)

**Status: not started.** There is no `master_db.py`, `tenant_pool.py`, tenant middleware, `/super-admin` route or `migrate-*` CLI command. P1 and P5 were re-checked against the code in this revision.

| # | Problem in the plan | Why it matters | Recommendation |
|---|---|---|---|
| P1 | "95% code reuse — only `get_storage` changes." | Several places bypass `get_storage`. `auth.get_session` and `require_hr_or_kiosk` read `request.app.state.storage` directly ([auth.py:213](app/auth.py#L213), [auth.py:258](app/auth.py#L258)). App-wide singletons hold per-org state: `notify._WAKE` and the single `NotificationWorker`, the single `SupabaseSyncWorker`, `app.state.face` (one collection, one circuit breaker), `app.state.kiosk_spoofs` (keyed by device label), `recognition._CACHE`, and `get_settings()` (one `secret_key`). `recognition._CACHE` is keyed only by `(period, is_live)`, verified, so tenants would see **each other's** "Employee of the month". | Inventory and scope every global before Phase 1. Add a test that fails if a request path reads `app.state.storage`. |
| P2 | The LRU pool "evicts and closes" a tenant's `Storage`. | Closing a connection that another thread is using raises "Cannot operate on a closed database". | Reference-count per request, and evict only idle tenants. |
| P3 | "Per-tenant locks → parallel across CPU cores." | One uvicorn process has one GIL. Load tests (report.md §9) saturated one worker at about 30 concurrent clients. | Plan for several worker processes. Move the scan rate limiting and circuit breaker into shared state (DB or Redis). |
| P4 | "1,000+ orgs / 50,000 employees on the Oracle free tier." | The bottleneck is **face inference**: about 2 scans/s for one Face API with 1.5 CPUs. 50,000 punches in a 30-minute window need about **28 scans/s**. | Size by peak scans/s. Add Face API replicas per N tenants, and per-tenant scan quotas. |
| P5 | Central notification queue in `master.db`. | The `notifications` table is also each user's **in-app inbox**: `inbox_rows()` reads `channel = 'push'` rows from it (verified, [notify.py:452](app/notify.py#L452)). Moving it breaks or mixes inboxes. | Keep each tenant's outbox and inbox in its own DB, with one worker that iterates over active tenants. |
| P6 | No scheduled jobs per tenant. | Wishes, reminders, the HR digest, housekeeping and Supabase sync run against one `Storage` today. | Add a tenant job runner with per-tenant claim keys and time zones. |
| P7 | Supabase RLS "so tenants can view their replica". | Sync uses a privileged connection that bypasses RLS, and there's no tenant-facing access. One shared cloud database contradicts "100% physical isolation". | Use a schema or project per tenant, make sync opt-in per tenant, or drop RLS from the plan. |
| P8 | Rename the face collection `employees` → `faces_tenant_fakhri`. | FaceAPI may not support renaming, and templates are keyed by employee code. A wrong migration breaks every gate scan. | Make the collection name configurable per tenant; keep `employees` for the first tenant. |
| P9 | Path mode `/t/{slug}/`. | The service worker scope (`/`), manifests, the passkey relying-party ID (per host, not per path), and hundreds of hard-coded absolute links and fetch URLs assume the root path. | Use subdomains only, or budget a full base-path refactor. |
| P10 | Inaccurate inventory. | Letters use **fpdf2**, not ReportLab; the performance claims are untested. | Refresh the inventory before estimating. |
| P11 | Missing: backups and DR, DPDP Act (biometrics processed for client companies) with data processing agreements, per-tenant export and deletion, super-admin access audit and 2FA, Razorpay plus GST invoicing, Caddy on-demand TLS "ask" hardening, noisy-neighbour limits, **per-tenant secrets** (B19-style shared keys). | Needed to sell to other companies. | Add a compliance and operations phase. |
| P12 | About 9.5 days for five phases. | P1–P9 make that several times too low. | Run a 2–3 day spike with two tenants and isolation tests, then re-estimate. |

---

## 3. Missing features

Priorities:
- **P0:** legal or statutory requirement, or a promise the app makes but doesn't keep.
- **P1:** expected by an Indian SME HRMS buyer.
- **P2:** nice to have.

### P0: statutory and legal

| # | Feature | Notes | Effort |
|---|---|---|---|
| F1 | **Maternity leave** (26 weeks, Maternity Benefit Act) and configurable leave types | Leave types are hard-coded (`paid`, `sick`, `casual`, `unpaid`). No maternity, paternity, bereavement, marriage or comp-off. | L |
| F2 | **Professional Tax slabs by state** | PT is a flat amount per salary structure; no state slabs or month rules (e.g. Maharashtra's ₹300 in February). | M |
| F3 | **Labour Welfare Fund** | Not present. | S–M |
| F4 | **Employer contributions and CTC** | No employer PF (EPS/EPF split, admin, EDLI), no employer ESIC (3.25%), no CTC view. Also needed for B1, B2 and B17. | M |
| F5 | **Payment of Bonus Act** | Only ad-hoc bonus. | M |
| F6 | **Arrears** | See B6. | M |
| F7 | **TDS returns** (24Q, Form 16 Part A via TRACES, Form 12BB) | The current Form 16 is a projection. | L |
| F8 | **DPDP Act 2023** for biometric and personal data | Consent and purpose notice, employee data-export request, retention and deletion schedule for ex-employees' data and face templates, breach log. | L |

### P1: expected by customers

| # | Feature | Notes | Effort |
|---|---|---|---|
| F9 | **Night and rotating shifts, rosters** | See B3. | L |
| F10 | **Overtime** calculation and pay | `overtime_min_minutes` exists on shifts but is never used. | M |
| F11 | **Comp-off** for work on weekly offs or holidays | Only noted ("worked on a weekly off"); no credit or pay. | M |
| F12 | **Multiple punches / breaks** per day | One in and one out only. | M |
| F13 | **Manager approvals beyond leave** | Regularisation and expense tables have unused `manager_*` columns; appraisal "manager review" is done by HR. | M |
| F14 | **Team view for managers** | Today's attendance, leave calendar, pending items. | M |
| F15 | **Granular HR roles (RBAC)** | One all-powerful `hr` role. Needed: payroll-only, recruiter, read-only auditor, branch HR. | L |
| F16 | **2FA (TOTP) for HR** | Passkeys are optional; password-only logins have full payroll access. | M |
| F17 | **Automated backups and restore** | No `cli.py backup`/`restore`; only a hint in Settings. | M |
| F18 | **Payroll maker-checker** | One user can calculate, edit, finalise and mark paid. | M |
| F19 | **Password-protected payslip PDFs** (PAN or date of birth) | Common in India when payslips are emailed or sent on WhatsApp. | S |
| F20 | **Bank-specific payout formats** | One generic NEFT CSV. | M |
| F21 | **Salary components and templates** | Fixed fields only; no custom earnings or deductions, no grade templates. | L |
| F22 | **Expense policy limits** | Categories are a fixed list (`EXPENSE_CATEGORIES`) but have no per-category caps, per-diem rules or receipt-required thresholds. *(Corrected in revision 2.)* | S–M |
| F23 | **Probation tracking and confirmation** | Probation exists only as a letter token. | M |
| F24 | **Onboarding checklist** | Exit has a clearance checklist; onboarding has none. | M |
| F25 | **Document expiry reminders** | The `documents` table has no expiry column (verified). | S–M |
| F26 | **Holidays by branch, and optional holidays** | One company-wide list. | M |
| F27 | **Leave year-end processing** | No lapse or carry-forward job or report. | M |
| F28 | **Configurable encashment and leave liability** | Rules are hard-coded in F&F. See B4, B5 and B22. | M |
| F29 | **Reports** | Headcount and attrition, payroll variance, leave liability, overtime, late-comers trend, custom exports. | L |
| F30 | **Unicode PDFs** | See B8 and B9. | M |
| F31 | **Biometric device import** (ZKTeco and similar) | No import or API. | M |
| F32 | **Accounting export** (Tally / journal voucher) | Not present. | M |

### P2: nice to have

| # | Feature |
|---|---|
| F33 | Recruitment / ATS pipeline (candidate letters exist; no requisitions or interviews) |
| F34 | Helpdesk / HR tickets, grievance and POSH register |
| F35 | Policy documents with employee acknowledgement |
| F36 | Surveys / eNPS, training and certification tracking |
| F37 | Employee directory with privacy controls |
| F38 | Hindi UI (the assistant already understands Hinglish) |
| F39 | Public REST API and webhooks |
| F40 | Promotions and transfers history (designation and department change log) |

---

## 4. Recommended order of work

| Step | Items | Effort |
|---|---|---|
| **1. Before the next EPFO / ESIC upload** | B1, B2, B17, B18, plus F4 employer shares where the returns need them | ~2–3 days |
| **2. Security, now** | B19 (rotate the WAHA key), B20, B14 | ~0.5 day |
| **3. Money correctness** | B7, B4, B5, B22, B13, B10 | ~2 days |
| **4. Breakage** | B8 (sanitiser now, Unicode font next), B12, B11 | ~1 day |
| **5. Larger fixes** | B6 / F6 arrears; B3 / F9 night shifts; B21 field selfie check; B23 | ~1.5–2 weeks |
| **6. Statutory features** | F1, F2, F8, F17 | ~3–4 weeks |
| **7. Customer features** | F13–F16, F10 / F11, F18 | ~4–6 weeks |
| **8. SaaS plan** | Fix P1–P9 in the design, run a two-tenant spike, then re-estimate | spike: 2–3 days |

Every fix should ship with its **Acceptance** test from §1, added to `tests/`.
