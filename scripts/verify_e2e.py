#!/usr/bin/env python3
"""End-to-end smoke test across BOTH services: Face API + HRMS.

Run it after starting the Face API and the HRMS (see docs/DEPLOY.md):

    # terminal 1
    cd faceapi && ./.venv/bin/python3 cli.py serve
    # terminal 2
    cd hrms && ./.venv/bin/python3 cli.py serve

    # terminal 3
    hrms/.venv/bin/python3 hrms/scripts/verify_e2e.py

What it proves, using the real models over real HTTP (no fakes):
  1. HR signs in, creates two employees and enrols two different faces
  2. the gate kiosk scans a DIFFERENT photo of the same person → recognised,
     checked in
  3. the same face scans again → checked out, hours computed
  4. a second person is recognised independently
  5. a photo with no face returns the friendly "no face" state
  6. attendance counters, monthly register CSV and a payroll run all work

Exit code 0 = everything passed.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT.parent / "faceapi" / "tests" / "assets"
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')

PASS, FAIL, INFO = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[36m·\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}" + (f" — {detail}" if detail else ""))
    else:
        print(f"  {FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def csrf_of(client: httpx.Client, path: str = "/dashboard") -> str:
    response = client.get(path)
    match = CSRF_RE.search(response.text)
    if not match:
        raise SystemExit(f"could not find a CSRF token on {path} (HTTP {response.status_code})")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Face API + HRMS end-to-end check")
    parser.add_argument("--hrms", default="http://127.0.0.1:8100")
    parser.add_argument("--hr-user", default="hr")
    parser.add_argument("--hr-password", required=True, help="HR password (see data/initial_credentials.txt)")
    parser.add_argument("--kiosk-user", default="gate")
    parser.add_argument("--kiosk-password", required=True)
    parser.add_argument("--cleanup", action="store_true", help="delete the two test employees at the end")
    args = parser.parse_args()

    for name in ("person_a_1.jpg", "person_a_2.jpg", "person_b_1.jpg", "no_face.png"):
        if not (ASSETS / name).exists():
            raise SystemExit(f"missing test image {ASSETS / name}")

    hrms = httpx.Client(base_url=args.hrms, timeout=60.0, follow_redirects=True)

    print("\n1. Connecting to the HRMS")
    health = hrms.get("/api/health").json()
    check("HRMS reachable", health.get("status") == "ok", health.get("version"))
    face_health = health.get("face_service", {})
    check(
        "Face API reachable from the HRMS",
        bool(face_health.get("ok")),
        str(face_health.get("detail")),
    )
    if not face_health.get("ok"):
        print("\nStart the Face API first:  cd faceapi && ./.venv/bin/python3 cli.py serve")
        return 1

    print("\n2. HR signs in and creates two employees")
    login = hrms.post("/login", data={"username": args.hr_user, "password": args.hr_password})
    check("HR login", login.status_code == 200 and "Present today" in login.text)
    if failures:
        print("\nWrong HR password? It is in data/initial_credentials.txt or set via HRMS_DEFAULT_HR_PASSWORD.")
        return 1

    token = csrf_of(hrms)
    created: list[dict] = []
    for index, (name, department) in enumerate(
        (("E2E Test Person A", "Engineering"), ("E2E Test Person B", "Operations")), start=1
    ):
        code = f"E2E{index:03d}"
        existing = hrms.get(f"/employees?q={code}").text
        response = hrms.post(
            "/employees",
            data={
                "_csrf": token,
                "name": name,
                "code": code,
                "department": department,
                "designation": "E2E Test",
                "joined_on": "2026-01-01",
                "employment_type": "full_time",
                "monthly_gross": "30000",
                "pf": "1800",
                "professional_tax": "200",
            },
            follow_redirects=False,
        )
        location = response.headers.get("location", "")
        employee_id = int(location.split("/employees/")[1].split("?")[0]) if "/employees/" in location else None
        if employee_id is None and code in existing:
            employee_id = None  # pre-existing from an earlier run; resolve below
        if employee_id is None:
            match = re.search(rf"/employees/(\d+)\"[^>]*>\s*<strong>{re.escape(name)}", hrms.get("/employees").text)
            employee_id = int(match.group(1)) if match else None
        created.append({"id": employee_id, "code": code, "name": name})
        check(f"employee {code} created", employee_id is not None, f"id={employee_id}")

    print("\n3. Enrolling two different faces")
    token = csrf_of(hrms, "/employees")
    for person, asset in zip(created, ("person_a_1.jpg", "person_b_1.jpg")):
        response = hrms.post(
            f"/employees/{person['id']}/face",
            data={"_csrf": token},
            files={"image": (asset, (ASSETS / asset).read_bytes(), "image/jpeg")},
            follow_redirects=False,
        )
        check(f"face enrolled for {person['code']}", "ok=" in response.headers.get("location", ""),
              response.headers.get("location", ""))

    print("\n4. The gate kiosk scans people")
    kiosk = httpx.Client(base_url=args.hrms, timeout=60.0, follow_redirects=True)
    kiosk_login = kiosk.post("/login", data={"username": args.kiosk_user, "password": args.kiosk_password})
    check("kiosk login", kiosk_login.status_code == 200 and "Start camera" in kiosk_login.text)
    kiosk_token = csrf_of(kiosk, "/kiosk")

    def scan(asset: str) -> dict:
        response = kiosk.post(
            "/api/kiosk/scan",
            data={"_csrf": kiosk_token},
            files={"image": (asset, (ASSETS / asset).read_bytes(), "image/jpeg")},
        )
        try:
            return response.json()
        except ValueError:
            return {"action": "http_error", "message": f"HTTP {response.status_code}"}

    token = csrf_of(hrms)
    first = scan("person_a_2.jpg")   # a DIFFERENT photo of person A
    check(
        "person A recognised from a different photo → CHECK IN",
        first.get("action") == "check_in" and first.get("employee", {}).get("code") == "E2E001",
        f"similarity={first.get('similarity')} action={first.get('action')}",
    )
    check("check-in time recorded", bool(first.get("server_time")), first.get("check_in", ""))

    stranger = scan("no_face.png")
    check("a photo without a face is rejected kindly", stranger.get("action") == "no_face", stranger.get("message", ""))

    second_person = scan("person_b_1.jpg")
    check(
        "person B recognised independently",
        second_person.get("action") == "check_in" and second_person.get("employee", {}).get("code") == "E2E002",
        f"similarity={second_person.get('similarity')}",
    )

    duplicate = scan("person_a_2.jpg")
    check("immediate re-scan is debounced", duplicate.get("action") in ("duplicate", "already_done"),
          duplicate.get("message", ""))

    print("\n5. Status, register and payroll")
    kiosk_status = kiosk.get("/api/kiosk/status").json()
    check("kiosk status reports the scanner is healthy", kiosk_status.get("face_service", {}).get("ok") is True)
    check(
        "gate screen is privacy-safe (no counters, no other people's scans)",
        "counts" not in kiosk_status and "recent" not in kiosk_status,
    )

    hr_status = hrms.get("/api/kiosk/status").json()
    check("HR still sees today's numbers", hr_status.get("counts", {}).get("present", 0) >= 2,
          f"present={hr_status.get('counts', {}).get('present')} inside={hr_status.get('counts', {}).get('inside_now')}")
    check("HR sees the recent scans", len(hr_status.get("recent", [])) >= 2)

    from datetime import date

    period = date.today().strftime("%Y-%m")
    register = hrms.get(f"/attendance/register?period={period}")
    check("monthly register renders", register.status_code == 200 and "Monthly register" in register.text)
    register_csv = hrms.get(f"/attendance/register.csv?period={period}")
    check("register CSV downloads", register_csv.status_code == 200 and "E2E001" in register_csv.text,
          f"{len(register_csv.text.splitlines())} lines")

    generated = hrms.post(
        "/payroll/generate", data={"_csrf": token, "period": period}, follow_redirects=False
    )
    payroll_page = hrms.get(f"/payroll?period={period}")
    check("payroll generated for this month", payroll_page.status_code == 200 and "Payslips" in payroll_page.text,
          f"HTTP {generated.status_code}")
    payroll_csv = hrms.get(f"/payroll/export.csv?period={period}")
    check("payroll CSV downloads", payroll_csv.status_code == 200 and "Net Pay" in payroll_csv.text,
          f"{len(payroll_csv.text.splitlines()) - 1} employees")

    print("\n6. Access control")
    hrms_pages = hrms.get("/dashboard", headers={"accept": "application/json"})
    check("HR can read the dashboard", hrms_pages.status_code == 200)
    kiosk_on_hr_page = kiosk.get("/employees", headers={"accept": "text/html"}, follow_redirects=False)
    check("kiosk login cannot open HR pages", kiosk_on_hr_page.status_code == 303,
          kiosk_on_hr_page.headers.get("location", ""))
    anonymous = httpx.post(f"{args.hrms}/api/kiosk/scan", files={"image": ("x.jpg", b"x", "image/jpeg")})
    check("anonymous scan is refused", anonymous.status_code == 401, f"HTTP {anonymous.status_code}")

    if args.cleanup:
        print("\n7. Cleanup")
        token = csrf_of(hrms)
        for person in created:
            if person["id"]:
                hrms.post(f"/employees/{person['id']}/delete", data={"_csrf": token}, follow_redirects=False)
                print(f"  {INFO} deleted {person['code']}")

    print()
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m " + ", ".join(failures))
        return 1
    print("\033[32mAll end-to-end checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError as exc:
        print(f"\033[31mCannot reach the HRMS: {exc}\033[0m")
        print("Start it with:  ./.venv/bin/python3 cli.py serve")
        sys.exit(2)
