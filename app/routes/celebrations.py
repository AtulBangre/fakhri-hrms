"""Celebrations page for HR: track birthdays, wedding anniversaries and work milestones."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from ..auth import SessionUser, require_hr
from ..deps import get_settings_dep, get_storage, render
from ..services import celebrations as celebrations_service
from ..services import employees as employees_service
from ..services import orgtime

router = APIRouter(tags=["celebrations"])


@router.get("/celebrations")
def celebrations_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    kind: str = Query("birthday"),
    department: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    tab: str = Query("celebrations"),
):
    today = orgtime.today(settings)
    f_date = orgtime.parse_date(from_date) if from_date else None
    t_date = orgtime.parse_date(to_date) if to_date else None

    # Retrieve filtered celebration events
    items = celebrations_service.list_celebrations(
        storage,
        today=today,
        from_date=f_date,
        to_date=t_date,
        kind=kind,
        department=department,
        search=search,
    )

    stats = celebrations_service.celebration_stats(storage, today)
    missing = celebrations_service.missing_dob_employees(storage)
    dept_list = employees_service.departments(storage)

    # Quick presets
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = date(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(today.year, today.month + 1, 1) - timedelta(days=1)

    presets = {
        "this_month_from": month_start.isoformat(),
        "this_month_to": month_end.isoformat(),
        "next_7_from": today.isoformat(),
        "next_7_to": (today + timedelta(days=7)).isoformat(),
        "next_30_from": today.isoformat(),
        "next_30_to": (today + timedelta(days=30)).isoformat(),
    }

    return render(
        request,
        "celebrations.html",
        active="celebrations",
        user=user,
        today=today,
        items=items,
        stats=stats,
        missing=missing,
        departments=dept_list,
        presets=presets,
        from_date=from_date or "",
        to_date=to_date or "",
        kind=kind,
        selected_dept=department or "",
        search=search or "",
        tab=tab,
    )
