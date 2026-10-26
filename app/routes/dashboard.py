"""Dashboard: today at a glance for HR."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import notify
from ..auth import SessionUser, require_hr
from ..deps import get_face, get_settings_dep, get_storage, paginate, render
from ..services import announcements as announcement_service
from ..services import attendance as attendance_service
from ..services import employees as employees_service
from ..services import leave as leave_service
from ..services import orgtime, payroll

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
):
    board = attendance_service.dashboard(storage, settings)
    current_period = orgtime.period_of(orgtime.today(settings))
    run = payroll.get_run(storage, current_period)

    payroll_overview = {
        "period": current_period,
        "label": orgtime.month_label(current_period),
        "run": run,
        "slips": len(payroll.list_slips(storage, current_period)) if run else 0,
        "estimated_cost": employees_service.stats(storage)["monthly_payroll"],
    }

    return render(
        request,
        "dashboard.html",
        active="dashboard",
        user=user,
        board=board,
        payroll_overview=payroll_overview,
        pending_face=employees_service.employees_missing_face(storage)[:5],
        leave_today=leave_service.list_leave(storage, period=current_period, limit=5),
        recent_leave=leave_service.list_leave(storage, limit=5),
        announcements=announcement_service.visible_for(storage, "hr"),
        company_notice_footer=storage.get_setting("company_notice_footer", ""),
        kiosks=attendance_service.kiosk_devices(storage),
        hr_email_missing=not notify.hr_emails(storage),
        face_ok=bool(face.health().get("ok")),
    )


@router.get("/api/dashboard/live-scans")
def live_scans(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    board = attendance_service.dashboard(storage, settings)
    return {
        "recent": board.get("recent", []),
        "counts": board.get("counts", {}),
    }


@router.get("/audit")
def audit_log(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    page: int = 1,
    q: str | None = None,
):
    where, params = "", []
    if q:
        where = "WHERE actor LIKE ? OR action LIKE ? OR entity LIKE ? OR detail LIKE ?"
        params = [f"%{q}%"] * 4
    pager = paginate(storage.scalar(f"SELECT COUNT(*) AS n FROM audit_log {where}", params), page, 100)
    rows = storage.query(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, pager["per_page"], pager["offset"]),
    )
    return render(request, "audit.html", active="audit", user=user, rows=rows, pager=pager, q=q or "")
