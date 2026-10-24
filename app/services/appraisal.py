"""Performance appraisal: cycles, weighted goals, self review and HR/manager review.

    HR creates a cycle (name, review period, common goals)        status = draft
    HR launches it → one appraisal per active employee            status = open
        employee scores each goal 1-5 and submits                 appraisal = manager_review
        HR scores each goal, writes feedback, recommends a raise  appraisal = completed
    HR closes the cycle                                           status = closed

Scores are weighted: ``sum(score x weight) / sum(weight)``. The final rating
defaults to the manager's weighted score and can be overridden. Employees see
their manager's rating only once the review is completed.
"""

from __future__ import annotations

from typing import Any

from ..db import Storage, utcnow_iso
from . import employees as employees_service
from . import orgtime

ValidationError = employees_service.ValidationError

RATING_LABELS = {
    1: "Needs improvement",
    2: "Partly meets",
    3: "Meets expectations",
    4: "Exceeds expectations",
    5: "Outstanding",
}
STATUS_LABELS = {
    "self_review": "Awaiting self review",
    "manager_review": "Awaiting HR review",
    "completed": "Completed",
    "withdrawn": "Not applicable",
}


def rating_label(value: float | None) -> str:
    if value is None:
        return "—"
    return RATING_LABELS.get(int(round(value)), "—")


def parse_goals(text: str | None) -> list[tuple[str, float]]:
    """One goal per line, optional weight after a pipe: ``Ship v2 | 40``."""
    goals: list[tuple[str, float]] = []
    for raw in (text or "").splitlines():
        line = raw.strip(" -•\t")
        if not line:
            continue
        title, _, weight = line.partition("|")
        try:
            value = float(weight.strip().rstrip("%")) if weight.strip() else 1.0
        except ValueError:
            value = 1.0
        if title.strip():
            goals.append((title.strip()[:200], max(0.0, value)))
    return goals


def _score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("Scores must be numbers from 1 to 5.") from None
    if not 1 <= number <= 5:
        raise ValidationError("Scores must be between 1 and 5.")
    return round(number, 1)


def weighted(goals: list[dict[str, Any]], field: str) -> float | None:
    scored = [(g[field], float(g["weight"] or 0)) for g in goals if g.get(field) is not None]
    total_weight = sum(weight for _, weight in scored)
    if not scored or not total_weight:
        return None
    return round(sum(score * weight for score, weight in scored) / total_weight, 2)


# ------------------------------------------------------------------- cycles
def create_cycle(storage: Storage, *, name: str, period_start: str, period_end: str,
                 self_due: str | None = None, goals_template: str | None = None,
                 actor: str | None = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Give the cycle a name, e.g. 'FY 2026-27 annual review'.")
    try:
        start, end = orgtime.parse_date(period_start), orgtime.parse_date(period_end)
    except ValueError as exc:
        raise ValidationError("Enter the review period dates.") from exc
    if end < start:
        raise ValidationError("The review period ends before it starts.")
    if not parse_goals(goals_template):
        raise ValidationError("Add at least one goal (one per line).")
    cursor = storage.execute(
        "INSERT INTO appraisal_cycles (name, period_start, period_end, self_due, goals_template, created_by, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (name[:120], start.isoformat(), end.isoformat(), (self_due or "").strip() or None,
         goals_template, actor, utcnow_iso()),
    )
    storage.audit(actor=actor, action="appraisal_cycle_created", entity="appraisal_cycle",
                  entity_id=cursor.lastrowid, detail=name)
    return get_cycle(storage, int(cursor.lastrowid))


def get_cycle(storage: Storage, cycle_id: int) -> dict[str, Any]:
    row = storage.query_one("SELECT * FROM appraisal_cycles WHERE id = ?", (cycle_id,))
    if row is None:
        raise ValidationError("Appraisal cycle not found.")
    row["goals"] = parse_goals(row.get("goals_template"))
    return row


def list_cycles(storage: Storage) -> list[dict[str, Any]]:
    rows = storage.query(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM appraisals a WHERE a.cycle_id = c.id) AS total,
               (SELECT COUNT(*) FROM appraisals a WHERE a.cycle_id = c.id AND a.status != 'self_review') AS self_done,
               (SELECT COUNT(*) FROM appraisals a WHERE a.cycle_id = c.id AND a.status = 'completed') AS completed,
               (SELECT ROUND(AVG(final_rating), 2) FROM appraisals a WHERE a.cycle_id = c.id
                  AND a.final_rating IS NOT NULL) AS average
        FROM appraisal_cycles c ORDER BY c.id DESC
        """
    )
    return rows


def launch_cycle(storage: Storage, cycle_id: int, *, actor: str | None = None) -> int:
    cycle = get_cycle(storage, cycle_id)
    if cycle["status"] != "draft":
        raise ValidationError(f"This cycle is already {cycle['status']}.")
    staff = storage.query("SELECT id FROM employees WHERE status = 'active'")
    if not staff:
        raise ValidationError("There are no active employees to review.")
    now = utcnow_iso()
    with storage.transaction():
        for person in staff:
            cursor = storage.execute_tx(
                "INSERT OR IGNORE INTO appraisals (cycle_id, employee_id, created_at) VALUES (?,?,?)",
                (cycle_id, person["id"], now),
            )
            if cursor.rowcount:
                for position, (title, weight) in enumerate(cycle["goals"]):
                    storage.execute_tx(
                        "INSERT INTO appraisal_goals (appraisal_id, title, weight, position) VALUES (?,?,?,?)",
                        (cursor.lastrowid, title, weight, position),
                    )
        storage.execute_tx("UPDATE appraisal_cycles SET status = 'open', launched_at = ? WHERE id = ?",
                           (now, cycle_id))
    storage.audit(actor=actor, action="appraisal_cycle_launched", entity="appraisal_cycle",
                  entity_id=cycle_id, detail=f"{len(staff)} employee(s)")
    return len(staff)


def close_cycle(storage: Storage, cycle_id: int, *, actor: str | None = None) -> None:
    cycle = get_cycle(storage, cycle_id)
    if cycle["status"] != "open":
        raise ValidationError("Only an open cycle can be closed.")
    open_reviews = int(storage.scalar(
        "SELECT COUNT(*) AS n FROM appraisals WHERE cycle_id = ? AND status NOT IN ('completed','withdrawn')",
        (cycle_id,),
    ))
    if open_reviews:
        raise ValidationError(f"{open_reviews} review(s) are not completed yet.")
    storage.execute("UPDATE appraisal_cycles SET status = 'closed', closed_at = ? WHERE id = ?",
                    (utcnow_iso(), cycle_id))
    storage.audit(actor=actor, action="appraisal_cycle_closed", entity="appraisal_cycle", entity_id=cycle_id)


def delete_cycle(storage: Storage, cycle_id: int, *, actor: str | None = None) -> None:
    cycle = get_cycle(storage, cycle_id)
    if cycle["status"] != "draft":
        raise ValidationError("Only a draft cycle can be deleted.")
    storage.execute("DELETE FROM appraisal_cycles WHERE id = ?", (cycle_id,))
    storage.audit(actor=actor, action="appraisal_cycle_deleted", entity="appraisal_cycle", entity_id=cycle_id)


# --------------------------------------------------------------- appraisals
def cycle_appraisals(storage: Storage, cycle_id: int) -> list[dict[str, Any]]:
    rows = storage.query(
        """
        SELECT a.*, e.name, e.code, e.department, e.designation FROM appraisals a
        JOIN employees e ON e.id = a.employee_id WHERE a.cycle_id = ? ORDER BY e.name COLLATE NOCASE
        """,
        (cycle_id,),
    )
    for row in rows:
        row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    return rows


def get_appraisal(storage: Storage, appraisal_id: int) -> dict[str, Any]:
    row = storage.query_one(
        """
        SELECT a.*, e.name, e.code, e.department, e.designation, e.joined_on, c.name AS cycle_name,
               c.status AS cycle_status, c.period_start, c.period_end, c.self_due
        FROM appraisals a JOIN employees e ON e.id = a.employee_id
        JOIN appraisal_cycles c ON c.id = a.cycle_id WHERE a.id = ?
        """,
        (appraisal_id,),
    )
    if row is None:
        raise ValidationError("Appraisal not found.")
    row["goals"] = storage.query(
        "SELECT * FROM appraisal_goals WHERE appraisal_id = ? ORDER BY position, id", (appraisal_id,)
    )
    row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    return row


def for_employee(storage: Storage, employee_id: int) -> list[dict[str, Any]]:
    rows = storage.query(
        """
        SELECT a.id FROM appraisals a JOIN appraisal_cycles c ON c.id = a.cycle_id
        WHERE a.employee_id = ? AND c.status != 'draft' ORDER BY c.id DESC
        """,
        (employee_id,),
    )
    return [get_appraisal(storage, row["id"]) for row in rows]


def add_goal(storage: Storage, appraisal_id: int, *, title: str, weight: float, target: str | None = None,
             actor: str | None = None) -> None:
    row = get_appraisal(storage, appraisal_id)
    if row["status"] == "completed":
        raise ValidationError("This review is completed.")
    if not (title or "").strip():
        raise ValidationError("Enter the goal.")
    storage.execute(
        "INSERT INTO appraisal_goals (appraisal_id, title, weight, target, position) VALUES (?,?,?,?,?)",
        (appraisal_id, title.strip()[:200], max(0.0, float(weight or 1)), (target or "").strip() or None,
         len(row["goals"])),
    )


def remove_goal(storage: Storage, goal_id: int) -> int:
    goal = storage.query_one(
        "SELECT g.appraisal_id, a.status FROM appraisal_goals g JOIN appraisals a ON a.id = g.appraisal_id "
        "WHERE g.id = ?", (goal_id,),
    )
    if goal is None:
        raise ValidationError("Goal not found.")
    if goal["status"] == "completed":
        raise ValidationError("This review is completed.")
    storage.execute("DELETE FROM appraisal_goals WHERE id = ?", (goal_id,))
    return int(goal["appraisal_id"])


def submit_self(storage: Storage, appraisal_id: int, *, employee_id: int, scores: dict[int, Any],
                notes: dict[int, str], comment: str | None, submit: bool, actor: str | None = None) -> dict[str, Any]:
    row = get_appraisal(storage, appraisal_id)
    if row["employee_id"] != employee_id:
        raise ValidationError("Appraisal not found.")
    if row["cycle_status"] != "open" or row["status"] != "self_review":
        raise ValidationError("Your self review is already submitted.")
    for goal in row["goals"]:
        storage.execute(
            "UPDATE appraisal_goals SET self_score = ?, self_note = ? WHERE id = ?",
            (_score(scores.get(goal["id"])), (notes.get(goal["id"]) or "").strip() or None, goal["id"]),
        )
    refreshed = get_appraisal(storage, appraisal_id)
    if submit and any(goal["self_score"] is None for goal in refreshed["goals"]):
        raise ValidationError("Score every goal before submitting (your draft is saved).")
    storage.execute(
        "UPDATE appraisals SET self_comment = ?, self_score = ?, status = ?, self_submitted_at = ? WHERE id = ?",
        ((comment or "").strip() or None, weighted(refreshed["goals"], "self_score"),
         "manager_review" if submit else "self_review", utcnow_iso() if submit else None, appraisal_id),
    )
    if submit:
        storage.audit(actor=actor, action="self_review_submitted", entity="employee", entity_id=employee_id,
                      detail=row["cycle_name"])
    return get_appraisal(storage, appraisal_id)


def review(storage: Storage, appraisal_id: int, *, scores: dict[int, Any], notes: dict[int, str],
           comment: str | None, final_rating: Any = None, increment_pct: Any = None,
           complete: bool = False, actor: str | None = None) -> dict[str, Any]:
    row = get_appraisal(storage, appraisal_id)
    if row["cycle_status"] != "open":
        raise ValidationError("The cycle is not open.")
    if row["status"] == "completed":
        raise ValidationError("This review is already completed.")
    for goal in row["goals"]:
        storage.execute(
            "UPDATE appraisal_goals SET manager_score = ?, manager_note = ? WHERE id = ?",
            (_score(scores.get(goal["id"])), (notes.get(goal["id"]) or "").strip() or None, goal["id"]),
        )
    refreshed = get_appraisal(storage, appraisal_id)
    manager_score = weighted(refreshed["goals"], "manager_score")
    final = _score(final_rating) if final_rating not in (None, "") else manager_score
    try:
        increment = float(increment_pct) if increment_pct not in (None, "") else None
    except (TypeError, ValueError):
        raise ValidationError("Increment must be a percentage, e.g. 8.") from None
    if increment is not None and not -50 <= increment <= 200:
        raise ValidationError("That increment looks wrong.")
    if complete:
        if any(goal["manager_score"] is None for goal in refreshed["goals"]):
            raise ValidationError("Score every goal before completing the review (your draft is saved).")
        if final is None:
            raise ValidationError("Give a final rating.")
    storage.execute(
        "UPDATE appraisals SET manager_score = ?, manager_comment = ?, final_rating = ?, increment_pct = ?, "
        "status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
        (manager_score, (comment or "").strip() or None, final, increment,
         "completed" if complete else row["status"], actor if complete else row.get("reviewed_by"),
         utcnow_iso() if complete else row.get("reviewed_at"), appraisal_id),
    )
    if complete:
        storage.audit(actor=actor, action="appraisal_completed", entity="employee", entity_id=row["employee_id"],
                      detail=f"{row['cycle_name']}: {final}")
    return get_appraisal(storage, appraisal_id)


def withdraw(storage: Storage, appraisal_id: int, *, actor: str | None = None) -> None:
    """Mark a review "not applicable" (the person left, went on long leave …) — APR-01."""
    row = get_appraisal(storage, appraisal_id)
    if row["status"] == "completed":
        raise ValidationError("A completed review cannot be withdrawn.")
    storage.execute("UPDATE appraisals SET status = 'withdrawn' WHERE id = ?", (appraisal_id,))
    storage.audit(actor=actor, action="appraisal_withdrawn", entity="employee", entity_id=row["employee_id"])


def withdraw_open_for(storage: Storage, employee_id: int, *, actor: str | None = None) -> int:
    """Withdraw every unfinished review in open cycles (used when an exit is settled)."""
    cursor = storage.execute(
        "UPDATE appraisals SET status = 'withdrawn' WHERE employee_id = ? AND status NOT IN ('completed','withdrawn') "
        "AND cycle_id IN (SELECT id FROM appraisal_cycles WHERE status = 'open')",
        (employee_id,),
    )
    return cursor.rowcount or 0


def reopen(storage: Storage, appraisal_id: int, *, actor: str | None = None) -> None:
    row = get_appraisal(storage, appraisal_id)
    if row["cycle_status"] != "open":
        raise ValidationError("The cycle is closed.")
    if row["status"] == "self_review":
        return
    storage.execute("UPDATE appraisals SET status = 'self_review' WHERE id = ?", (appraisal_id,))
    storage.audit(actor=actor, action="appraisal_reopened", entity="employee", entity_id=row["employee_id"])
