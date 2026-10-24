# Fakhri HRMS — Plan v4: Biometric safety, kiosk UX, punch governance, late policy

> **Status of the original roadmap (checked against the code on 2026-10-24):**
> Feature 5 (A1–A3, C1–C3) and Phases 5–6 are **already done** and on `main` (305 tests).
> This plan covers the four new features. Each section lists the decisions taken where the
> original text conflicted with the code or with an earlier decision — review those first.

---

## 0. Corrections to the original roadmap

| Original text | Reality in the code | Consequence |
|---|---|---|
| SFace "512-dim" embeddings | Face API returns **128-dim** L2-normalised vectors | Doc fix only |
| Duplicate threshold "≥ 0.65 (standard SFace)" | The Face API's own threshold is **0.363** (OpenCV's recommended SFace cosine) | **Use the Face API's match decision (0.363).** At 0.65 most real duplicates (same person, different photo) score 0.4–0.6 and would slip through |
| Face key `emp_14` | Keys are `emp-14` (since EMP-01); older enrolments use the employee code | Resolve both formats |
| `/me/face` self-service enrolment | **Does not exist** | Not added: HR-witnessed enrolment is the anti-buddy-punching control. The duplicate check runs on the HR route only |
| "Zero external CDN dependencies" | Pages load Google Fonts | Doc fix; unchanged |
| Kiosk: employee gets "403 Forbidden" | Employees are redirected to `/me` | Return a real **403** (friendly error page for browsers, JSON for the API) |
| Phases 5–6 (merge, UI & KYC) | Done | Removed from the sequence |

---

## 1. Feature 1 — Duplicate face prevention

**Rule:** one face, one employee.

- Before enrolling, `employees.enrol_face` runs a 1:N `identify` on the photo.
- For every candidate the Face API marks as a match (similarity ≥ its threshold), resolve the key (`emp-<id>` or legacy code) to an employee:
  - **another active or inactive employee** → raise `DuplicateFaceError(name, code)` and do not enrol;
  - **the same employee** → allowed (re-enrolment / new photo);
  - **a key with no employee** (orphan template from a deleted record) → ignored and logged.
- HR sees: *"This face is already registered to **Name (CODE)**. A single face cannot be registered across multiple accounts."*
- No override switch. Twins are the only legitimate case; HR must contact support (documented in the HR guide).

**Tests:** A enrols → ok · B with A's face → blocked with A's name/code · A re-enrols → ok · orphan key → ok.

---

## 2. Feature 2 — Tap-to-scan kiosk and confirmation card

**Settings (Settings → Gate kiosk):**
- `kiosk_scan_mode`: `tap` (default) | `continuous`.
- `kiosk_confirmation`: `full` (default) | `anonymous`.

**Tap mode:**
- **Idle:** the camera is off, saving battery. The screen shows branding, the large clock and "Tap the screen to mark attendance".
- **Tap:** the camera starts and a "Verifying identity…" HUD appears. Up to 3 frames are tried over about 3 seconds, which covers "no face yet" while the person settles.
- **After the result:** the camera turns off again.

**Continuous mode:** today's behaviour (auto-scan every ~1.4 s), for the morning rush.

**Confirmation card (`full`):**
- Full-screen, green tick, and **IN** or **OUT**.
- Name, employee code, designation and department.
- Time and date, e.g. `09:42 AM · Saturday, 24 Oct 2026`.
- A late pill, e.g. "Late 31 min · ₹50 fine".
- Chime, then auto-dismisses after **3.5 s** back to idle.

**Avatar:** an initials monogram. HRMS deliberately stores **no face photos** (only the Face API template), so there is no `photo_url`.

**Punch type:** the existing state machine — first scan = IN, second = OUT, later scans = "already done". There are no odd/even multiple pairs, because payroll counts one in/out per day.

> **Privacy note:** the gate previously hid names (`HRMS_KIOSK_SHOW_NAME`, off). The card now shows the name to the person who just scanned, for 3.5 s, per this roadmap. Choose `anonymous` in Settings to go back to the old behaviour.

---

## 3. Feature 3 — Punch permissions (kiosk-only vs self-punch)

- **Global setting** `allow_employee_self_punch`:
  - `disabled` (**default**, per the roadmap): attendance only at the gate kiosk.
  - `geofence_only`: today's field-attendance behaviour.
  - `always_allowed`: mobile punch from anywhere; the location is still recorded when the browser gives it.
- **Per-employee override:** `employees.self_punch_mode` = `inherit` | `disabled` | `geofence_only` | `always_allowed`, set on the employee edit form.
  - *Enhancement over the original 0/1 flag:* one employee can be geofenced while another is unrestricted.
- **Enforcement:** the `/api/field/check` API refuses with a clear message. The **Field attendance** sidebar item and page are hidden when the effective mode is `disabled`; `/me` shows read-only status.
- **Kiosk:** employees get **403** at `/kiosk` and `/api/kiosk/*`.

> **Behaviour change:** with the default `disabled`, existing deployments lose employee field attendance until HR sets the global mode to `geofence_only` or enables it per employee. This follows the roadmap; the HR guide explains the switch.

---

## 4. Feature 4 — Tiered late fines and automatic half day

**Settings:**

| Setting | Default | Meaning |
|---|---|---|
| `late_policy_mode` | `tiered_fine_only` | `none` · `lop_count_only` (today's "every N late marks = ½ day LOP") · `tiered_fine_only` · `hybrid_both` |
| `late_grace_minutes` | 15 | Late starts after shift start + grace (exists) |
| `late_slab_1_minutes` / `late_slab_1_penalty` | 15 / ₹50 | Up to 15 min past grace (09:45–10:00) |
| `late_slab_2_minutes` / `late_slab_2_penalty` | 30 / ₹100 | The next 30 min (10:00–10:30); anything later but before the half-day cutoff also pays slab 2 |
| `auto_half_day_late_minutes` | 60 | Counted **from shift start**: arriving at or after 10:30 → half day. 0 = off |

**Per day (gate, field, manual entry and approved corrections):**
- Store `late_minutes` and a new `late_fine`.
- If the arrival crosses the half-day cutoff, the day becomes `half_day` with no fine. The existing half-day rule makes it 0.5 LOP.
- Policy `none` means no fine, no half day and no count rule.

**Payroll:**
- `payslips.late_fine` is the sum of the month's fines inside the employment window.
- It is a separate deduction line ("Late arrival fines"), capped at what the month can carry, and appears on the HTML and PDF payslips and the bank CSV.
- The count rule only applies in `lop_count_only` and `hybrid_both`.

> **Behaviour change:** existing databases move from the "3 late marks = ½ day" rule to fines by default. Finalised months keep their figures (locked); draft months change on recalculation.

---

## 5. Execution sequence

| Step | Work | Verification |
|---|---|---|
| 1 | Feature 1 — duplicate face check | Unit + route tests with the fake face service |
| 2 | Feature 3 — punch permissions + kiosk 403 | Route tests per mode and override; field nav hidden |
| 3 | Feature 4 — late policy engine | Tests per slab, cutoff, mode; payslip line; register/ESS totals |
| 4 | Feature 2 — kiosk modes and confirmation card | Scan API returns card data; screenshot of the card; JS mode switch |
| 5 | Docs + full regression | HR guide; full pytest; site crawl |

Work happens on branch `feat/plan-v4`, one commit per step. Merging to `main` and pushing need your go-ahead.
