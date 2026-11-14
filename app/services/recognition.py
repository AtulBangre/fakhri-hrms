"""Employee Recognition & Star Performer Scoring Service.

Computes a multi-dimensional 100-point performance index based on real HRMS data:
- Attendance Reliability (Max 35 pts)
- Punctuality & Early Arrivals (Max 25 pts)
- Working Hours & Dedication (Max 20 pts)
- Leave Discipline & Zero LOP (Max 15 pts)
- Biometric Kiosk Authenticity (Max 5 pts)
- Missed Punch Regularization Penalties (Deductions)
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from ..config import Settings
from ..db import Storage
from . import orgtime

# Lightweight in-memory cache: (period, is_live) -> (timestamp, data)
_CACHE: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_LIVE = 300.0  # 5 minutes for live period
_CACHE_TTL_PAST = 86400.0  # 24 hours for finalized past months


def clear_cache() -> None:
    """Clear recognition cache (e.g. after attendance scans or testing)."""
    _CACHE.clear()


def available_periods(today: date, count: int = 6) -> list[dict[str, Any]]:
    """Return the list of periods (current month + past months) for the UI switcher."""
    periods = []
    cur_year = today.year
    cur_month = today.month

    for i in range(count):
        y = cur_year
        m = cur_month - i
        while m <= 0:
            m += 12
            y -= 1
        p_str = f"{y:04d}-{m:02d}"
        is_live = (i == 0)
        label = f"{orgtime.month_label(p_str)}{' (Live)' if is_live else ''}"
        periods.append({
            "period": p_str,
            "label": label,
            "is_live": is_live,
        })
    return periods


def get_monthly_recognition(
    storage: Storage,
    settings: Settings,
    period: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Calculate and return the Top 3 performers and scorecard for the given period."""
    today = orgtime.today(settings)
    current_period = orgtime.period_of(today)

    if not period:
        period = current_period
    else:
        parsed = orgtime.parse_period(period)
        if parsed:
            period = parsed

    is_live = (period == current_period)
    cache_key = (period, is_live)

    now = time.time()
    if use_cache and cache_key in _CACHE:
        cached_time, cached_data = _CACHE[cache_key]
        ttl = _CACHE_TTL_LIVE if is_live else _CACHE_TTL_PAST
        if (now - cached_time) < ttl:
            return cached_data

    # Period dates
    month_start, month_end = orgtime.month_bounds(period)
    end_date = min(today, month_end) if is_live else month_end
    start_iso = month_start.isoformat()
    end_iso = end_date.isoformat()

    # Calculate company working days in this range (excluding Sundays & registered holidays)
    holiday_rows = storage.query("SELECT day FROM holidays WHERE day >= ? AND day <= ?", (start_iso, end_iso))
    holidays_set = {r["day"] for r in holiday_rows}
    working_dates = []
    cur_d = month_start
    while cur_d <= end_date:
        d_iso = cur_d.isoformat()
        if cur_d.weekday() != 6 and d_iso not in holidays_set:
            working_dates.append(d_iso)
        cur_d += timedelta(days=1)
    total_working_days = max(1, len(working_dates))

    # 1. Fetch active employees
    employees = storage.query(
        """
        SELECT id, code, name, department, designation, photo_filename, joined_on, status
        FROM employees
        WHERE status = 'active'
        ORDER BY name
        """
    )
    if not employees:
        result = {
            "period": period,
            "label": orgtime.month_label(period) + (" (Live)" if is_live else ""),
            "is_live": is_live,
            "total_working_days": total_working_days,
            "top3": [],
            "candidates_count": 0,
            "available_periods": available_periods(today),
        }
        _CACHE[cache_key] = (now, result)
        return result

    # 2. Fast Aggregation: Attendance in date range
    att_rows = storage.query(
        """
        SELECT employee_id,
               COUNT(CASE WHEN status = 'present' THEN 1 END) AS present_days,
               COUNT(CASE WHEN status = 'half_day' THEN 1 END) AS half_days,
               COUNT(CASE WHEN status = 'absent' THEN 1 END) AS absent_days,
               COUNT(CASE WHEN status = 'on_leave' OR status = 'leave' THEN 1 END) AS leave_days,
               SUM(COALESCE(late_minutes, 0)) AS total_late_minutes,
               COUNT(CASE WHEN late_minutes > 0 THEN 1 END) AS late_days,
               SUM(COALESCE(worked_minutes, 0)) AS total_worked_minutes,
               COUNT(CASE WHEN source = 'face' OR source = 'kiosk_face' THEN 1 END) AS face_scans
        FROM attendance
        WHERE work_date >= ? AND work_date <= ?
        GROUP BY employee_id
        """,
        (start_iso, end_iso),
    )
    att_by_emp = {r["employee_id"]: r for r in att_rows}

    # 3. Regularizations in date range
    reg_rows = storage.query(
        """
        SELECT employee_id, COUNT(*) AS reg_count
        FROM attendance_regularizations
        WHERE work_date >= ? AND work_date <= ?
        GROUP BY employee_id
        """,
        (start_iso, end_iso),
    )
    reg_by_emp = {r["employee_id"]: r["reg_count"] for r in reg_rows}

    # 4. Approved Leave Requests overlapping date range
    leave_rows = storage.query(
        """
        SELECT employee_id, SUM(days) AS total_leaves
        FROM leave_requests
        WHERE status = 'approved' AND from_date <= ? AND to_date >= ?
        GROUP BY employee_id
        """,
        (end_iso, start_iso),
    )
    leave_by_emp = {r["employee_id"]: float(r["total_leaves"] or 0) for r in leave_rows}

    candidates = []
    for emp in employees:
        emp_id = emp["id"]
        att = att_by_emp.get(emp_id) or {}
        present = att.get("present_days") or 0
        half = att.get("half_days") or 0
        absent = att.get("absent_days") or 0
        late_days = att.get("late_days") or 0
        late_mins = att.get("total_late_minutes") or 0
        worked_mins = att.get("total_worked_minutes") or 0
        face_scans = att.get("face_scans") or 0
        reg_count = reg_by_emp.get(emp_id) or 0
        approved_leaves = leave_by_emp.get(emp_id) or 0.0

        # Working days expected for this specific employee (respecting joined_on)
        joined_str = emp.get("joined_on") or start_iso
        emp_working_days = sum(1 for d in working_dates if d >= joined_str)
        emp_working_days = max(1, emp_working_days)

        # Ignore if zero attendance records and zero activity
        if present == 0 and half == 0 and absent == 0:
            continue

        # --- A. Attendance Score (Max 35) ---
        effective_attended = present + (0.5 * half)
        att_ratio = min(1.0, effective_attended / emp_working_days)
        s_attendance = round(att_ratio * 35.0, 1)

        # --- B. Punctuality Score (Max 25) ---
        p_deduction = (late_days * 2.0) + min(10.0, (late_mins // 15) * 0.5)
        s_punctuality = round(max(0.0, 25.0 - p_deduction), 1)

        # --- C. Working Hours / Diligence (Max 20) ---
        target_daily_mins = 480  # 8 hrs
        expected_mins = effective_attended * target_daily_mins
        if expected_mins > 0:
            hours_ratio = min(1.2, worked_mins / expected_mins)
            s_hours = round(min(20.0, hours_ratio * 20.0), 1)
        else:
            s_hours = 0.0

        # --- D. Leave Discipline & Zero Unexcused Absence (Max 15) ---
        s_discipline = round(max(0.0, 15.0 - (absent * 7.5)), 1)

        # --- E. Biometric Kiosk Authenticity (Max 5) ---
        kiosk_ratio = min(1.0, face_scans / max(1, present))
        s_kiosk = round(kiosk_ratio * 5.0, 1)

        # --- F. Penalties ---
        penalties = round(min(5.0, reg_count * 1.0), 1)

        # Total Score
        total_score = round(
            max(0.0, min(100.0, s_attendance + s_punctuality + s_hours + s_discipline + s_kiosk - penalties)),
            1,
        )

        # Dynamic Reason highlights
        reasons = []
        if late_days == 0:
            reasons.append("100% on-time (zero late arrivals)")
        elif late_days == 1:
            reasons.append(f"Only 1 minor late arrival ({late_mins}m)")
        else:
            reasons.append(f"{late_days} late arrivals ({late_mins}m total)")

        if absent == 0:
            reasons.append("Zero unexcused absences / LOP")

        att_pct = round(att_ratio * 100)
        if att_pct >= 100:
            reasons.append(f"Perfect attendance ({present}/{emp_working_days} days)")
        else:
            reasons.append(f"{att_pct}% attendance ({present} present)")

        if worked_mins > 0 and effective_attended > 0:
            avg_hrs = round((worked_mins / effective_attended) / 60, 1)
            reasons.append(f"Avg {avg_hrs}h/day ({round(worked_mins / 60)}h logged)")

        if face_scans >= present and present > 0:
            reasons.append("100% kiosk face-scan verified")

        candidates.append({
            "employee": {
                "id": emp["id"],
                "code": emp["code"],
                "name": emp["name"],
                "department": emp["department"] or "General",
                "designation": emp["designation"] or "Staff",
                "photo_filename": emp.get("photo_filename"),
            },
            "score": total_score,
            "breakdown": {
                "attendance": s_attendance,
                "punctuality": s_punctuality,
                "hours": s_hours,
                "discipline": s_discipline,
                "kiosk": s_kiosk,
                "penalties": penalties,
            },
            "stats": {
                "present_days": present,
                "working_days": emp_working_days,
                "late_days": late_days,
                "late_minutes": late_mins,
                "total_hours": round(worked_mins / 60, 1),
                "absent_days": absent,
                "regularizations": reg_count,
            },
            "reasons": reasons[:3],  # top 3 highlights
        })

    # Sort descending by total score, then attendance, then punctuality, then hours
    candidates.sort(
        key=lambda c: (
            c["score"],
            c["breakdown"]["attendance"],
            c["breakdown"]["punctuality"],
            c["stats"]["total_hours"],
        ),
        reverse=True,
    )

    top3 = []
    for rank_idx, cand in enumerate(candidates[:3], start=1):
        cand["rank"] = rank_idx
        if rank_idx == 1:
            cand["badge_title"] = "Leading Contender" if is_live else "Employee of the Month"
            cand["badge_icon"] = "crown"
            # Win chances note
            if is_live:
                if len(candidates) > 1:
                    margin = round(cand["score"] - candidates[1]["score"], 1)
                    if margin >= 5.0:
                        cand["chance_label"] = f"Strong lead (+{margin} pts ahead)"
                    elif margin >= 1.0:
                        cand["chance_label"] = f"Leading by {margin} pts in a close race"
                    else:
                        cand["chance_label"] = "Tied / highly competitive lead"
                else:
                    cand["chance_label"] = "Top performer"
            else:
                cand["chance_label"] = "Winner"
        elif rank_idx == 2:
            cand["badge_title"] = "2nd Place Contender" if is_live else "1st Runner-Up"
            cand["badge_icon"] = "silver"
            if is_live and len(candidates) > 0:
                deficit = round(candidates[0]["score"] - cand["score"], 1)
                cand["chance_label"] = f"Close contender (-{deficit} pts)"
            else:
                cand["chance_label"] = "1st Runner-Up"
        else:
            cand["badge_title"] = "3rd Place Contender" if is_live else "2nd Runner-Up"
            cand["badge_icon"] = "bronze"
            cand["chance_label"] = "Top 3 Contender" if is_live else "2nd Runner-Up"

        top3.append(cand)

    result = {
        "period": period,
        "label": orgtime.month_label(period) + (" (Live)" if is_live else ""),
        "is_live": is_live,
        "total_working_days": total_working_days,
        "candidates_count": len(candidates),
        "top3": top3,
        "available_periods": available_periods(today),
    }

    _CACHE[cache_key] = (now, result)
    return result
