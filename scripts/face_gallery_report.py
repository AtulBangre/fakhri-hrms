#!/usr/bin/env python3
"""Compare employees with FaceAPI's templates. Reads only, and deletes nothing.

    # in the FaceAPI container or project:
    python scripts/export_gallery_index.py --db data/faceapi.db --collection employees > gallery.json
    # here:
    python scripts/face_gallery_report.py --hrms-db data/hrms.db --gallery gallery.json

Run this before setting HRMS_FACE_TEMPLATE_CHECK=enforce. An active employee with
no matching template would be locked out by the check. Prints codes and template
ids only, never names. Exit code 1 means enforce is not safe yet.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

MIN_GOOD_SCORE = 0.90
MIN_GOOD_FACE_PX = 80


def owner_of(external_id: str | None, by_id: dict[int, dict], by_code: dict[str, dict]) -> dict | None:
    if not external_id:
        return None
    if external_id.startswith("emp-") and external_id[4:].isdigit():
        return by_id.get(int(external_id[4:]))
    return by_code.get(external_id)


def analyse(employees: list[dict], faces: list[dict]) -> dict[str, list]:
    by_id = {e["id"]: e for e in employees}
    by_code = {e["code"]: e for e in employees}
    templates = {f["face_id"]: f for f in faces}

    orphans, stale, current, low_quality = [], [], [], []
    for face in faces:
        owner = owner_of(face.get("external_id"), by_id, by_code)
        if owner is None:
            orphans.append(face["face_id"])
        elif owner.get("face_id") != face["face_id"]:
            stale.append({"face_id": face["face_id"], "employee_code": owner["code"]})
        else:
            current.append(face["face_id"])
            box = face.get("box") or [0, 0, 0, 0]
            score = face.get("det_score") or 0.0
            if score < MIN_GOOD_SCORE or min(box[2], box[3]) < MIN_GOOD_FACE_PX:
                low_quality.append({"employee_code": owner["code"], "det_score": score, "box": box})

    no_template = []
    for employee in employees:
        if employee.get("status") == "active" and employee.get("face_id") not in templates:
            no_template.append(
                {"employee_code": employee["code"], "reason": "not enrolled" if not employee.get("face_id") else "template missing"}
            )

    return {
        "current_templates": current,
        "orphan_templates": orphans,
        "stale_templates": stale,
        "active_without_matching_template": no_template,
        "low_quality_templates": low_quality,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hrms-db", required=True, type=Path)
    parser.add_argument("--gallery", required=True, type=Path)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.hrms_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        employees = [dict(row) for row in conn.execute("SELECT id, code, status, face_id FROM employees")]
    finally:
        conn.close()
    faces = json.loads(args.gallery.read_text(encoding="utf-8"))["faces"]

    report = analyse(employees, faces)
    print(f"employees: {len(employees)}   templates: {len(faces)}")
    for name, items in report.items():
        print(f"{name}: {len(items)}")
    print(json.dumps({k: v for k, v in report.items() if k != "current_templates"}, indent=2))

    safe = not [row for row in report["active_without_matching_template"] if row["reason"] == "template missing"]
    print("\nEnforce:", "safe for every enrolled employee" if safe else "NOT safe, fix the employees listed above first")
    print("Employees who were never enrolled cannot scan either way.")
    print("This script changed nothing. Removing stale templates is a separate, approved step.")
    return 0 if safe else 1


if __name__ == "__main__":
    sys.exit(main())
