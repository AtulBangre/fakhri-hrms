# Fakhri HRMS: Remaining Plan

**Updated:** 2026-11-06

These items need a decision or a schema change, so they could not simply be built. The security and rollout work for both apps is in `faceapi/plan.md`.

**Rules:** keep the Fakhri maroon theme (`#870808`). New tables or columns need separate owner approval.

- [ ] **Branch filter on the monthly register.** The app has no branch concept yet, so this needs a schema change.
- [ ] **Face match score in the dashboard's live feed.** Attendance rows do not store the score, so this needs a schema change. Today the score is only in the audit log.
- [ ] **A real LLM for Ask HR.** Decide first: it would send employee questions, and possibly data, to an outside service.
- [ ] **Clear passwords already saved in old notifications.** New login messages use a one-time link. Removing the old rows is a database write, so it needs approval.
