"""Ask HR: in-app intelligent assistant for employees and HR."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import SessionUser, current_user
from ..deps import get_settings_dep, get_storage
from ..services import attendance as attendance_service
from ..services import leave as leave_service
from ..services import orgtime

router = APIRouter(prefix="/api/ai-hr", tags=["ai-hr"])


class QueryRequest(BaseModel):
    query: str


@router.post("/query")
def ask_hr(
    payload: QueryRequest,
    user: SessionUser | None = Depends(current_user),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    if not user:
        return {"answer": "Please log in to ask questions.", "suggestions": []}

    q = payload.query.lower().strip()
    today = orgtime.today(settings)
    shift_start = storage.get_setting("shift_start", "09:30")
    shift_end = storage.get_setting("shift_end", "18:30")
    grace = storage.get_setting("late_grace_minutes", "15")

    # 1. Leave & Balances
    if any(k in q for k in ["leave", "chhutti", "balance", "sick", "casual", "pl"]):
        if user.is_employee and user.employee_id:
            bal = leave_service.balance_for(storage, settings, int(user.employee_id))
            ans = (
                f"You have {bal.get('available', 0)} days available (accrued: {bal.get('accrued', 0)}, "
                f"used paid: {bal.get('used_paid', 0)}, unpaid: {bal.get('used_unpaid', 0)}). "
                f"You can apply for leave directly from the Leave menu."
            )
            return {"answer": ans, "suggestions": ["Apply for leave", "Upcoming holidays", "Shift timing"]}
        elif user.is_hr:
            pending = storage.scalar("SELECT COUNT(*) FROM leave_requests WHERE status = 'pending'")
            ans = f"There are currently {pending} pending leave request(s) waiting for your review in the Leave dashboard."
            return {"answer": ans, "suggestions": ["Review pending leaves", "Today's attendance", "Upcoming holidays"]}

    # 2. Holidays
    if any(k in q for k in ["holiday", "chutti", "tyohar", "festival", "off"]):
        holidays_dict = storage.holidays()
        upcoming = [(day, name) for day, name in sorted(holidays_dict.items()) if day >= today.isoformat()][:4]
        if upcoming:
            items = ", ".join([f"{name} ({day})" for day, name in upcoming])
            ans = f"Upcoming company holidays: {items}."
        else:
            ans = "No more upcoming gazetted holidays recorded for this period."
        return {"answer": ans, "suggestions": ["Leave balance", "Shift timing", "Daily attendance"]}

    # 3. Shift timings & Policies
    if any(k in q for k in ["shift", "time", "timing", "grace", "late", "punch"]):
        ans = (
            f"General office timing is {shift_start} to {shift_end}. "
            f"Grace period is {grace} minutes (scans after {shift_start} + {grace}m are marked Late). "
            f"Working less than 4 hours is counted as a Half Day."
        )
        return {"answer": ans, "suggestions": ["Leave balance", "Upcoming holidays", "My payslips"]}

    # 4. Today's Attendance (for HR)
    if user.is_hr and any(k in q for k in ["today", "attendance", "absent", "present", "scan"]):
        board = attendance_service.dashboard(storage, settings)
        counts = board.get("counts", {})
        ans = (
            f"Today's stats ({today.isoformat()}): {counts.get('present', 0)} Present, "
            f"{counts.get('late', 0)} Late, {counts.get('leave', 0)} On Leave, "
            f"{counts.get('absent', 0)} Absent, and {counts.get('not_marked', 0)} not marked yet."
        )
        return {"answer": ans, "suggestions": ["Open daily board", "Monthly register", "Run payroll"]}

    # 5. Payslips
    if any(k in q for k in ["salary", "payslip", "pay", "ctc"]):
        if user.is_employee:
            ans = "You can view and download your monthly salary slips under the 'Payslips' section in your profile."
            return {"answer": ans, "suggestions": ["View my payslips", "Leave balance", "Upcoming holidays"]}
        else:
            current_period = orgtime.period_of(today)
            ans = f"Payroll for {current_period} can be generated and approved from the Payroll section."
            return {"answer": ans, "suggestions": ["Open payroll", "Bank CSV export", "Attendance register"]}

    # Default friendly fallback
    return {
        "answer": (
            f"Hello {user.display_name or 'there'}! I am your Fakhri HR Assistant. "
            f"You can ask me about your leave balance, company holidays, shift timings, or attendance policies."
        ),
        "suggestions": ["Leave balance", "Upcoming holidays", "Shift timing", "Office policy"],
    }
