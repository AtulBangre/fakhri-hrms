# Fakhri HRMS: Remaining Plan

**Updated:** 2026-11-06

Everything else in the earlier upgrade plan was removed because the feature exists in the code. These items were listed as done but were not found. This is a code check, not a test of each feature.

**Rules:** keep the Fakhri maroon theme (`#870808`). New tables or columns need separate owner approval, because database changes are on hold.

## Attendance

- [ ] Monthly register (`/attendance/register`): add filters for shift and attendance status, without a full page reload. Today it filters by month and department only.
- [ ] Add a branch filter. The code has no branch concept yet, so this probably needs a schema change.

## Leave and payroll

- [ ] Add a loss-of-pay ledger that links each unapproved absence to the month-end salary deduction. Today LOP appears only as totals and row chips.

## Dashboard

- [ ] Show the verification result on each entry in the live scan feed.
- [ ] Add the remaining quick actions: Quick Regularize, Approve Leaves and Run Payroll.

## People

- [ ] Org chart (`/employees/chart`): add expand and collapse for branches.

## Ask HR assistant

The assistant answers from the app's own data using keyword matching. It does not call an LLM or stream responses.

- [ ] Decide whether a real LLM is wanted. It would send employee questions, and possibly data, to an outside service.

## Deployment

See `faceapi/plan.md`. The security and rollout work for both apps is tracked there.
