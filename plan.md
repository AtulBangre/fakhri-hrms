# Fakhri HRMS — Complete Notification & WhatsApp System Master Plan

**Date:** 2026-11-12  
**Target:** 100% Automated, Zero-Cost WhatsApp & Multi-Channel HRMS Notification Infrastructure  
**Status:** Approved for Implementation  
**Deployment Target:** Production (`hrms.fakhriitservices.com` on VPS `140.245.224.173`)

---

## 1. Executive Summary

Fakhri IT Services HRMS now has an active, self-hosted WAHA (WhatsApp HTTP API) engine linked to official WhatsApp (`+91 6263638053`). While Kiosk gate scanner punches and manual payslips are connected, an audit of the codebase revealed several critical communication gaps where notifications only go to browser WebPush or email—or fail entirely when an employee does not have a corporate email address.

This plan details the complete roadmap to fix all identified bugs, remove architectural bottlenecks, and expand WhatsApp automations across every touchpoint of the employee and HR lifecycle.

---

## 2. Issues & Gaps Catalog

| ID | Module / File | Current Issue / Defect | Target Fix | Priority |
|---|---|---|---|---|
| **BUG-01** | `app/routes/payroll.py` | If an employee has no email address, clicking "Send Payslip" throws an error and completely skips WhatsApp PDF delivery. | Decouple WhatsApp from email: Allow sending WhatsApp PDF slip independently if phone exists, even if email is blank. | **P0 (Critical)** |
| **BUG-02** | `app/routes/leave.py` | `_notify_decision` returns early if `employee.get('email')` is missing or `notify_leave_email` is disabled, completely bypassing `queue_whatsapp`. | Ensure WhatsApp leave approval/rejection triggers regardless of email status. | **P0 (Critical)** |
| **GAP-01** | `app/services/field.py` | Mobile Geofenced / Field Self-Punch only calls `push_punch` (browser bell). No WhatsApp confirmation is queued. | Add `queue_whatsapp` on mobile check-in and check-out, matching Kiosk behavior. | **P1 (High)** |
| **GAP-02** | `app/routes/me.py` | Leave application submission only sends email to HR and employee. Templates `leave_applied_confirmation` and `leave_requested` exist in `notify.py` but are not called. | Queue WhatsApp confirmation to employee and WhatsApp alert to HR/Manager on leave submission. | **P1 (High)** |
| **GAP-03** | `app/routes/regularizations.py` | Missed punch regularization approval/rejection only notifies via browser WebPush. | Queue WhatsApp update to employee upon HR approval/rejection of missed punch correction. | **P1 (High)** |
| **GAP-04** | `app/services/alerts.py` | Shift check-in and check-out reminders ("You have not checked in yet") only notify via browser WebPush. | Send timely WhatsApp reminder to employees who haven't punched within grace period. | **P1 (High)** |
| **GAP-05** | `app/services/alerts.py` | Morning attendance summary (`_hr_summary`) only notifies HR via browser WebPush. | Send consolidated morning summary (Present/Late/Leave counts) directly to HR/Owner WhatsApp. | **P1 (High)** |
| **GAP-06** | `app/services/alerts.py` | Birthday and Work Anniversary greetings only send email and browser WebPush. | Send personalized WhatsApp greetings to celebrating employees. | **P2 (Medium)** |
| **GAP-07** | `app/routes/records.py` | Expense claim approval and payment notifications are restricted to browser WebPush. | Queue WhatsApp message when an expense claim is approved or marked paid. | **P2 (Medium)** |
| **GAP-08** | `app/routes/exits.py` | Resignation approval and exit clearance updates only go to browser WebPush. | Queue WhatsApp update for resignation approval/rejection and relieving notice. | **P2 (Medium)** |
| **GAP-09** | `app/routes/payroll.py` | Finalizing monthly payroll marks runs as complete but requires separate manual email actions for distribution. | Add automated option to dispatch WhatsApp PDF payslips to all active employees on run finalization. | **P2 (Medium)** |

---

## 3. Implementation Plan & Work Breakdown

### Phase 1: Core Decoupling & Bug Fixes (P0)

1. **Refactor `app/routes/payroll.py` (Single Payslip Send):**
   - Check if at least one delivery channel is available (`email` OR `phone`).
   - If email is missing but phone exists, send WhatsApp PDF payslip cleanly without returning an error.
   - If both exist, dispatch both.
   - Return clean flash message reflecting which channels were delivered.
2. **Refactor `app/routes/leave.py` (`_notify_decision`):**
   - Separate email dispatch from WhatsApp dispatch so that failure or absence of email does not block WhatsApp delivery.
   - Ensure employee receives WhatsApp update with approved/rejected status and HR decision note.

---

### Phase 2: Attendance & Field Mobile Punch WhatsApp Engine (P1)

1. **Enhance `app/services/field.py`:**
   - In `mark_by_face` / `mark_by_location`:
     - After successful punch, check `settings.notify_whatsapp_attendance` and `employee.get('phone')`.
     - Call `notify.queue_whatsapp(storage, employee['phone'], template, settings=settings, **ctx)` with time, date, location name, and late minutes/hours worked.
2. **Verify Gate Scanner vs Field Punch Consistency:**
   - Both punch sources share identical message formatting:
     - Check-In: `*Fakhri IT Services* — Attendance marked IN at HH:MM on DD/MM/YYYY. (Location: X).`
     - Check-Out: `*Fakhri IT Services* — Attendance marked OUT at HH:MM. Worked X hrs.`

---

### Phase 3: Leave, Regularization & Workflow Notifications (P1)

1. **Leave Application (`app/routes/me.py`):**
   - On leave submission:
     - Queue `leave_applied_confirmation` to employee phone: *"Your leave request for X day(s) (From → To) has been submitted to HR."*
     - If HR or manager has a phone on file, queue `leave_requested` alert.
2. **Attendance Regularization (`app/routes/regularizations.py` & `app/services/regularization.py`):**
   - Add template `regularization_decided` in `app/notify.py`:
     - *"Fakhri IT Services — Your missed punch regularization for DD/MM/YYYY was [Approved/Rejected]."*
   - When HR decides a regularization request, queue WhatsApp message to the employee.
3. **Expense Claims (`app/routes/records.py`):**
   - Add template `expense_update` in `app/notify.py`:
     - *"Fakhri IT Services — Your [category] expense claim for Rs X was [Approved/Rejected/Paid]."*
   - Queue message to employee upon claim approval and payment.

---

### Phase 4: Cron-Driven Reminders & Celebrations (P1 & P2)

1. **Daily Attendance Reminders (`app/services/alerts.py`):**
   - In `_remind_attendance`:
     - For employees with missed punch past grace period, queue a gentle WhatsApp reminder:
       - *"Namaste [Name], your shift started at [Time]. Please mark your attendance."*
     - For employees with missed punch-out after shift end, queue an evening reminder:
       - *"Your shift ended at [Time]. Remember to mark check-out before leaving."*
2. **Daily Morning Attendance Summary for HR/Owner (`app/services/alerts.py`):**
   - Read HR notification phone from settings (`settings.hr_whatsapp_phone` or primary HR user).
   - Format daily digest:
     ```text
     *Fakhri IT Services — Today's Attendance Summary* 📊
     Date: DD/MM/YYYY | Time: 10:00 AM
     • Present: 14
     • Late: 2
     • On Leave: 1
     • Not Marked Yet: 1
     Portal: https://hrms.fakhriitservices.com/attendance
     ```
   - Queue to HR WhatsApp automatically.
3. **Celebrations — Birthdays & Anniversaries (`app/services/alerts.py`):**
   - Add WhatsApp template `birthday_wish` and `anniversary_wish` in `app/notify.py`.
   - Dispatch warm personalized greeting to employee WhatsApp on their special day.

---

### Phase 5: Payroll Finalization Automation (P2)

1. **Auto-Dispatch on Finalize:**
   - In `app/routes/payroll.py` (`finalize_run`):
     - When a payroll run is finalized, provide an automatic option: "Dispatch WhatsApp Payslips to All Employees".
     - Runs asynchronously via `NotificationWorker` without blocking the HTTP request.
     - Each employee receives their individual PDF attachment directly.

---

### Phase 6: Admin Settings & Configuration UI (P2)

1. **Settings Page Enhancements (`app/templates/settings.html`):**
   - Add dedicated notification toggles under WhatsApp settings card:
     - [x] Send Check-In / Check-Out alerts
     - [x] Send Monthly Payslip PDF
     - [x] Send Leave & Regularization updates
     - [x] Send Daily Shift Reminders
     - [x] Send Morning Attendance Digest to HR (with HR Phone Number field)
     - [x] Send Birthday & Anniversary greetings
   - Test WhatsApp sender box (already implemented & functional).

---

## 4. Verification & Testing Strategy

1. **Unit & Integration Tests (`tests/test_waha_whatsapp.py`):**
   - Test decoupling of email and WhatsApp in payslip & leave modules.
   - Test message formatting for all new templates.
   - Test PDF file attachment encoding via WAHA `sendFile` endpoint.
2. **End-to-End Functional Verification:**
   - Test self-punch from mobile/field: verify immediate WhatsApp confirmation.
   - Test leave submission & decision: verify employee receives WhatsApp status.
   - Test missed punch request & approval: verify employee receives approval notice.
   - Test single & bulk payslip dispatch: verify PDF opens properly on WhatsApp mobile app.
3. **Zero-Downtime Deployment:**
   - Commit & push changes to `main`.
   - Pull on VPS (`ubuntu@140.245.224.173`).
   - Run `docker compose build api && docker compose up -d --no-deps api`.
   - Verify health and live message delivery.

---

## 5. Execution Roadmap & Timeline

- [x] **Step 1:** Fix P0 bugs (decouple email dependency in payroll and leave routes).
- [x] **Step 2:** Implement Mobile / Field Self-Punch WhatsApp alerts.
- [x] **Step 3:** Implement Leave application confirmation & Regularization WhatsApp updates.
- [x] **Step 4:** Implement Scheduled Reminders (Late check-in, Missed check-out, Morning HR Digest).
- [x] **Step 5:** Implement Birthday & Work Anniversary WhatsApp greetings.
- [x] **Step 6:** Implement Trainee Employment Type & Dynamic Stipend/Bank Details logic.
- [x] **Step 7:** Add Settings UI toggles & HR Digest phone number configuration.
- [x] **Step 8:** Normalize all 23 number input fields to `step="any"` to remove `00`/`0` constraints.
- [x] **Step 9:** Wire Loan and Salary Advance notifications (Employee & HR) + F&F Settlement WhatsApp alerts.
- [x] **Step 10:** Run full test suite, deploy to production, and verify live delivery.

---

## 6. Trainee Role & Smart Stipend / Bank Details Architecture

### 6.1 Overview & Business Purpose
In IT services and technology companies, trainees generally fall into two categories:
1. **Unpaid Trainees (Evaluation / College Freshers / Interns):** Stipend is ₹0. In this case, capturing Bank Account Number, IFSC, EPFO UAN, and ESIC adds unnecessary friction to onboarding.
2. **Paid Trainees (Graduate Engineer Trainees / Stipendiary):** They receive a fixed monthly stipend (e.g., ₹5,000 – ₹15,000) and require verified bank details for direct payout.

The HRMS will intelligently adapt based on the role and stipend value.

---

### 6.2 Architectural & Functional Specifications

#### REQ-T01: Backend Employment Type Expansion
- **File:** `app/services/employees.py`
  - Update `EMPLOYMENT_TYPES = ("full_time", "part_time", "intern", "contract", "trainee")`.
  - Update `EMPLOYMENT_LABELS`:
    ```python
    EMPLOYMENT_LABELS = {
        "full_time": "Full time",
        "part_time": "Part time",
        "intern": "Intern",
        "contract": "Contract",
        "trainee": "Trainee",
    }
    ```
- **File:** `app/services/employees_bulk.py` & `app/services/employees_export.py`
  - Allow `trainee` in CSV bulk import and Excel export formats.

#### REQ-T02: Dynamic Smart UI in Add/Edit Employee Form
- **File:** `app/templates/employee_form.html`
  - **Dynamic Salary / Stipend Labeling:**
    - When `employment_type` is `trainee` or `intern`:
      - The Gross Salary input label changes dynamically to **"Monthly Stipend (₹)"**.
      - Hint displays: *"Enter 0 for unpaid trainee / evaluation period."*
  - **Conditional Bank & Statutory Details Card:**
    - When `employment_type` is `trainee` and Stipend is `0`:
      - The "Personal & Bank Details" card automatically collapses or hides the bank inputs.
      - A clean, informative card appears:
        `ℹ️ Unpaid Trainee Mode: Bank account, IFSC, EPFO UAN, and ESIC are bypassed since no monetary payout is configured.`
      - Bank inputs (`bank_name`, `account_number`, `ifsc`) are marked non-required and ignored during validation.
    - When Stipend is `> 0`:
      - The Bank Details card automatically expands with inputs for Bank Name, Account Number, and IFSC enabled for payout processing.
    - Standard full-time, part-time, and contract employees retain standard salary and bank inputs.

#### REQ-T03: Attendance, Kiosk & Full System Access
- Trainees (both paid and unpaid) retain **100% full feature parity** across:
  - Gate scanner / Kiosk face recognition and QR scan.
  - Mobile geofenced self-punch.
  - Daily Attendance Board & Monthly Register.
  - Leave requests and quota management.
  - WhatsApp punch-in, punch-out, and welcome credential alerts.

#### REQ-T04: Payroll Engine Handling for Unpaid Trainees
- **File:** `app/routes/payroll.py` & `app/services/payroll.py`
  - During monthly payroll generation:
    - Employees with `monthly_gross == 0` (Unpaid Trainees) are marked as non-payable attendance records.
    - They will not generate zero-rupee payout entries in bank NEFT/IMPS transfer export batches.
    - Paid trainees with stipend > 0 receive standard payslip generation, bank export lines, and WhatsApp PDF slips.

---

### 6.3 Detailed Execution Plan for Trainee Feature

| Task ID | Component | Changes Required | Verification Method |
|---|---|---|---|
| **TRN-01** | Backend Service | Add `"trainee"` to `EMPLOYMENT_TYPES` & `EMPLOYMENT_LABELS` in `app/services/employees.py`. | Unit test: Create employee with `employment_type="trainee"` succeeds. |
| **TRN-02** | Bulk Upload & Export | Update `employees_bulk.py` & `employees_export.py` to recognize `trainee`. | Verify CSV import with `employment_type: trainee` parses cleanly. |
| **TRN-03** | Dynamic Form JS | Add real-time JS switcher in `employee_form.html` for Stipend vs Salary and Bank toggle based on `stipend == 0`. | Browser test: selecting Trainee + 0 stipend hides bank inputs; stipend > 0 reveals bank inputs. |
| **TRN-04** | Detail Page Display | Update `employee_detail.html` to display "Trainee" chip and format stipend correctly. | Inspect employee profile page with Trainee role. |
| **TRN-05** | Payroll Protection | Ensure `payroll.py` safely handles ₹0 stipend trainees without bank transfer errors. | Generate test payroll period with an unpaid trainee. |

---

## 7. Number Input Step Validation Fix (Removing Restrictive `00` & `0` Constraints)

### 7.1 Problem Description & Root Cause
In HTML5, `<input type="number">` elements enforce that values entered must match the exact mathematical multiple of the `step` attribute. 
Across the HRMS, 23 financial and numerical input fields currently specify `step="100"`, `step="10"`, `step="500"`, or `step="25"`. This inadvertently forces the browser to block form submissions unless the entered number ends in `00` or `0`, showing an error:
`"Please enter a valid value. The two nearest valid values are 15200 and 15300."`

This blocks genuine values like **₹15,250**, **₹1,875** (12% PF on ₹15,625), **₹114.50** (ESI), **₹1,450** (asset cost), or **₹3,200** (advance/loan EMI).

---

### 7.2 Complete Catalog of Affected Fields (23 Locations)

| # | Template File | Field Name / ID | Current Restrictive Step | New Safe Attribute | Description |
|---|---|---|---|---|---|
| 1 | `app/templates/employee_form.html` | `monthly_gross` | `step="100"` | `step="any"` | Monthly Salary / Stipend |
| 2 | `app/templates/employee_form.html` | `basic` | `step="100"` | `step="any"` | Basic Salary Component |
| 3 | `app/templates/employee_form.html` | `hra` | `step="100"` | `step="any"` | House Rent Allowance |
| 4 | `app/templates/employee_form.html` | `other_allowances` | `step="100"` | `step="any"` | Special / Other Allowances |
| 5 | `app/templates/employee_form.html` | `pf` | `step="10"` | `step="any"` | Provident Fund Deduction |
| 6 | `app/templates/employee_form.html` | `esi` | `step="10"` | `step="any"` | ESIC Deduction |
| 7 | `app/templates/employee_form.html` | `professional_tax` | `step="10"` | `step="any"` | Professional Tax |
| 8 | `app/templates/employee_detail.html` | `monthly_gross` | `step="100"` | `step="any"` | Edit Monthly Salary |
| 9 | `app/templates/employee_detail.html` | `basic` | `step="100"` | `step="any"` | Edit Basic Salary |
| 10 | `app/templates/employee_detail.html` | `hra` | `step="100"` | `step="any"` | Edit HRA |
| 11 | `app/templates/employee_detail.html` | `other_allowances` | `step="100"` | `step="any"` | Edit Other Allowances |
| 12 | `app/templates/employee_detail.html` | `pf` | `step="10"` | `step="any"` | Edit PF |
| 13 | `app/templates/employee_detail.html` | `esi` | `step="10"` | `step="any"` | Edit ESI |
| 14 | `app/templates/employee_detail.html` | `professional_tax` | `step="10"` | `step="any"` | Edit Professional Tax |
| 15 | `app/templates/payroll.html` | `bonus` | `step="100"` | `step="any"` | Payroll Slip Bonus Adjustment |
| 16 | `app/templates/payroll.html` | `advance` | `step="100"` | `step="any"` | Payroll Advance Deduction |
| 17 | `app/templates/payroll.html` | `other_deduction` | `step="100"` | `step="any"` | Payroll Other Deductions |
| 18 | `app/templates/assets.html` | `cost` | `step="100"` | `step="any"` | Company Asset Purchase Cost |
| 19 | `app/templates/me_loans.html` | `principal_amount` | `step="500"` | `step="any"` | Employee Loan Request Amount |
| 20 | `app/templates/me_loans.html` | `monthly_emi` | `step="500"` | `step="any"` | Employee Loan EMI Selection |
| 21 | `app/templates/loans.html` | `principal_amount` | `step="500"` | `step="any"` | HR Loan Principal Allocation |
| 22 | `app/templates/loans.html` | `monthly_emi` | `step="100"` | `step="any"` | HR Loan Monthly EMI |
| 23 | `app/templates/locations.html` | `radius_m` | `step="25"` | `step="1"` | Geofence Radius in Meters |

---

### 7.3 Implementation & Verification Plan

1. **Mass Replace Restrictive Steps:**
   - In all salary, tax, deduction, loan, and asset inputs: replace `step="100"`, `step="10"`, `step="500"` with `step="any"`.
   - In `locations.html` geofence radius: replace `step="25"` with `step="1"` (allowing any integer radius e.g., 30m, 45m, 60m).
2. **Behavior Verification:**
   - Test submitting odd salary amounts (e.g., `₹25,255.50` or `₹1,875.00`): form submits cleanly without browser warnings.
   - Test submitting loan/advance requests for non-round figures (e.g., `₹3,250`): form submits cleanly.
   - Test setting Geofence radius to `35m`: saved cleanly.


