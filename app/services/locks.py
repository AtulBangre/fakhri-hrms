"""Payroll period locks.

Once a month's payroll run is finalised (or paid), the attendance and leave that
produced its payslips must not change underneath it — otherwise the payslip, the
register and the bank file silently disagree. Every write path that touches a
day calls :func:`assert_period_open`; HR reopens the run to make a correction.
"""

from __future__ import annotations

from datetime import date

from ..db import Storage
from . import employees as employees_service
from . import orgtime


def locked_status(storage: Storage, period: str) -> str | None:
    row = storage.query_one("SELECT status FROM payroll_runs WHERE period = ?", (period,))
    return row["status"] if row and row["status"] != "draft" else None


def assert_period_open(storage: Storage, day: date | str, until: date | str | None = None) -> None:
    """Raise if any month in ``day..until`` has a finalised or paid payroll run."""
    start = orgtime.parse_date(day)
    end = orgtime.parse_date(until) if until else start
    period, last = orgtime.period_of(start), orgtime.period_of(end)
    while period <= last:
        status = locked_status(storage, period)
        if status:
            raise employees_service.ValidationError(
                f"{orgtime.month_label(period)} payroll is {'finalised' if status == 'finalized' else 'paid'} — "
                + ("reopen the run first to change its attendance or leave."
                   if status == "finalized" else "a paid month cannot be changed.")
            )
        year, month = (int(p) for p in period.split("-"))
        period = f"{year + (month == 12)}-{(month % 12) + 1:02d}"
