#!/usr/bin/env python3
"""Fill the HRMS with believable demo data so you can click around immediately.

    ./.venv/bin/python3 cli.py seed-demo --employees 12
    ./.venv/bin/python3 cli.py seed-demo --enrol      # also enrol faces (needs the Face API)

What it creates:
  * N employees across Engineering / Marketing / Operations / Finance
  * two months of attendance with a realistic mix of present, late, half day,
    absent and leave days
  * paid and unpaid leave records (so balances are non-zero)
  * a generated payroll run for last month

It is safe to re-run: employees are matched by code, and `--reset` removes the
demo codes first.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from app.config import Settings
from app.db import Storage
from app.face import FaceService, FaceServiceError
from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import leave as leave_service
from app.services import orgtime, payroll

FIRST_NAMES = [
    "Aarav", "Isha", "Rohan", "Ananya", "Vikram", "Priya", "Kabir", "Sneha", "Arjun", "Meera",
    "Zoya", "Imran", "Neha", "Devansh", "Riya", "Siddharth", "Fatima", "Yash", "Kavya", "Nikhil",
]
LAST_NAMES = [
    "Sharma", "Verma", "Khan", "Patel", "Iyer", "Nair", "Gupta", "Singh", "Joshi", "Mehta",
    "Sheikh", "Chouhan", "Rathore", "Bansal", "Kulkarni",
]
DEPARTMENTS = [
    ("Engineering", "Backend Developer", 62000),
    ("Engineering", "Frontend Developer", 58000),
    ("Marketing", "Performance Marketer", 46000),
    ("Marketing", "Content Lead", 44000),
    ("Operations", "Fulfilment Executive", 28000),
    ("Operations", "Catalog Associate", 26000),
    ("Finance", "Accounts Executive", 38000),
    ("Design", "Graphic Designer", 34000),
]

DEMO_PREFIX = "DEMO"


def _names(count: int, rng: random.Random) -> list[tuple[str, str, str, float]]:
    people = []
    used: set[str] = set()
    for _ in range(count):
        for _attempt in range(50):
            first = rng.choice(FIRST_NAMES)
            last = rng.choice(LAST_NAMES)
            full = f"{first} {last}"
            if full not in used:
                used.add(full)
                break
        department, designation, salary = rng.choice(DEPARTMENTS)
        jitter = rng.randint(-6, 8) * 500
        people.append((full, department, designation, float(max(18000, salary + jitter))))
    return people


def seed(settings: Settings, *, employees: int = 10, enrol: bool = False, reset: bool = False) -> int:
    storage = Storage(settings)
    rng = random.Random(20260105)  # deterministic demo data
    today = orgtime.today(settings)

    try:
        if reset:
            existing = storage.query("SELECT id, code FROM employees WHERE code LIKE ?", (f"{DEMO_PREFIX}%",))
            for row in existing:
                employees_service.delete_employee(storage, row["id"], actor="seed")
            print(f"  removed {len(existing)} previous demo employees")
            for period in [run["period"] for run in payroll.list_runs(storage)]:
                payroll.delete_run(storage, period, actor="seed")

        # ---------------------------------------------------------- employees
        created: list[dict] = []
        for index, (name, department, designation, salary) in enumerate(_names(employees, rng), start=1):
            code = f"{DEMO_PREFIX}{index:03d}"
            existing = employees_service.get_employee_by_code(storage, code)
            if existing:
                created.append(existing)
                continue
            joined = today - timedelta(days=rng.randint(60, 900))
            employee_id = employees_service.create_employee(
                storage,
                {
                    "name": name,
                    "code": code,
                    "department": department,
                    "designation": designation,
                    "email": f"{name.split()[0].lower()}.{name.split()[-1].lower()}@fakhriitservices.com",
                    "phone": f"+91 9{rng.randint(100000000, 999999999)}",
                    "joined_on": joined.isoformat(),
                    "monthly_gross": salary,
                    "pf": round(salary * 0.06 / 10) * 10,
                    "professional_tax": 200,
                    "employment_type": rng.choice(["full_time"] * 8 + ["intern", "contract"]),
                    "notes": "demo record created by `cli.py seed-demo`",
                    "_actor": "seed",
                },
            )
            created.append(employees_service.get_employee(storage, employee_id))
        print(f"  employees ready: {len(created)}")

        # --------------------------------------------------------- attendance
        months = [orgtime.period_of(today), orgtime.period_of(_first_of_previous_month(today))]
        marked = 0
        for employee in created:
            for period in months:
                for day in orgtime.days_in_period(period):
                    if day > today:
                        continue
                    if orgtime.day_kind(storage, day) != "working":
                        continue
                    if attendance_service.get_row(storage, employee["id"], day):
                        continue
                    roll = rng.random()
                    if day == today and roll < 0.45:
                        continue  # some people simply have not arrived yet
                    if roll < 0.80:                       # on time
                        check_in, status = "09:%02d" % rng.randint(12, 44), "present"
                        check_out = "18:%02d" % rng.randint(5, 55)
                    elif roll < 0.90:                     # late
                        check_in, status = "10:%02d" % rng.randint(5, 50), "present"
                        check_out = "18:%02d" % rng.randint(30, 55)
                    elif roll < 0.95:                     # half day
                        check_in, status = "09:%02d" % rng.randint(20, 45), "half_day"
                        check_out = "13:%02d" % rng.randint(0, 45)
                    elif roll < 0.98:                     # absent (leave a gap)
                        continue
                    else:                                 # unpaid leave later
                        continue
                    attendance_service.mark_manual(
                        storage,
                        employee_id=employee["id"],
                        work_date=day.isoformat(),
                        status=status,
                        check_in=check_in,
                        check_out=check_out,
                        note="demo data",
                        actor="seed",
                    )
                    marked += 1
        print(f"  attendance rows marked: {marked}")

        # -------------------------------------------------------------- leave
        leaves = 0
        for employee in created[: max(3, len(created) // 2)]:
            start = today - timedelta(days=rng.randint(5, 55))
            days = rng.choice([1, 1, 2, 0.5])
            leave_type = rng.choice(["paid", "paid", "sick", "casual", "unpaid"])
            try:
                leave_service.apply_leave(
                    storage,
                    settings,
                    employee_id=employee["id"],
                    leave_type=leave_type,
                    from_date=start.isoformat(),
                    to_date=start.isoformat() if days == 0.5 else (start + timedelta(days=max(0, int(days) - 1))).isoformat(),
                    half_day=days == 0.5,
                    reason="demo leave",
                    actor="seed",
                )
                leaves += 1
            except employees_service.ValidationError:
                continue
        print(f"  leave records: {leaves}")

        # ------------------------------------------------------------ payroll
        last_month = orgtime.period_of(_first_of_previous_month(today))
        try:
            payroll.generate_run(storage, settings, last_month, actor="seed")
            run = payroll.get_run(storage, last_month)
            print(f"  payroll {last_month} generated: {run['total_net']:,.0f} net across "
                  f"{len(payroll.list_slips(storage, last_month))} payslips")
        except employees_service.ValidationError as exc:
            print(f"  payroll skipped: {exc}")

        # --------------------------------------------------------------- faces
        if enrol:
            face_service = FaceService(settings)
            health = face_service.health()
            if not health.get("ok"):
                print(f"  face enrolment skipped — {health.get('detail')}")
            else:
                from pathlib import Path

                assets = sorted((settings.data_dir.parent / "faceapi" / "tests" / "assets").glob("person_*.jpg"))
                if not assets:
                    print("  face enrolment skipped — no sample portraits found in faceapi/tests/assets")
                else:
                    enrolled = 0
                    for index, employee in enumerate(created):
                        if employee.get("face_id"):
                            continue
                        sample = assets[index % len(assets)].read_bytes()
                        try:
                            employees_service.enrol_face(storage, face_service, employee["id"], sample, actor="seed")
                            enrolled += 1
                        except (FaceServiceError, ValueError) as exc:
                            print(f"  enrolment failed for {employee['code']}: {exc}")
                            break
                    print(f"  faces enrolled: {enrolled} (rotating sample portraits — for demos only)")

        print()
        print("  Sign in and look around:")
        print(f"    HR console : {settings.public_base_url.rstrip('/')}/dashboard")
        print(f"    Gate kiosk : {settings.public_base_url.rstrip('/')}/kiosk")
        return 0
    finally:
        storage.close()


def _first_of_previous_month(day: date) -> date:
    first = day.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)
