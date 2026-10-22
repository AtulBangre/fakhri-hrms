"""Documents, assets and expense claims — the "employee records" features.

All three follow the same shape: an employee owns something (a file, a laptop, a
claim), HR can see everything, and every state change is recorded with who did it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from ..uploads import UploadError, delete_file, save_upload
from . import employees as employees_service
from . import orgtime

# ------------------------------------------------------------------ documents
DOC_CATEGORIES = {
    "aadhaar": "Aadhaar",
    "pan": "PAN",
    "offer_letter": "Offer letter",
    "contract": "Contract",
    "resume": "Resume / CV",
    "other": "Other",
}

#: Which documents HR expects for a complete file (used for the completeness meter).
REQUIRED_DOCS = ("aadhaar", "pan")


def add_document(
    storage: Storage,
    settings: Settings,
    *,
    employee_id: int,
    data: bytes,
    original_name: str | None,
    category: str,
    title: str | None = None,
    notes: str | None = None,
    uploaded_by: str | None = None,
) -> dict[str, Any]:
    employees_service.require_employee(storage, employee_id)
    if category not in DOC_CATEGORIES:
        category = "other"
    stored = save_upload(settings, "documents", data, original_name=original_name)
    cursor = storage.execute(
        """
        INSERT INTO documents (employee_id, category, title, filename, stored_name, mime, size,
                               sha256, notes, uploaded_by, uploaded_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            employee_id, category, (title or DOC_CATEGORIES[category])[:120], stored.filename,
            stored.stored_name, stored.mime, stored.size, stored.sha256, notes, uploaded_by,
            utcnow_iso(),
        ),
    )
    storage.audit(actor=uploaded_by, action="document_uploaded", entity="employee",
                  entity_id=employee_id, detail=f"{category}: {stored.filename}")
    return get_document(storage, int(cursor.lastrowid))


def get_document(storage: Storage, document_id: int) -> dict[str, Any]:
    row = storage.query_one(
        """
        SELECT d.*, e.name AS employee_name, e.code AS employee_code
        FROM documents d JOIN employees e ON e.id = d.employee_id WHERE d.id = ?
        """,
        (document_id,),
    )
    if row is None:
        raise ValueError("Document not found.")
    row["category_label"] = DOC_CATEGORIES.get(row["category"], row["category"])
    return row


def list_documents(
    storage: Storage, *, employee_id: int | None = None, category: str | None = None, limit: int = 300
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if employee_id:
        clauses.append("d.employee_id = ?")
        params.append(employee_id)
    if category and category != "all":
        clauses.append("d.category = ?")
        params.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = storage.query(
        f"""
        SELECT d.*, e.name AS employee_name, e.code AS employee_code
        FROM documents d JOIN employees e ON e.id = d.employee_id
        {where} ORDER BY d.uploaded_at DESC LIMIT ?
        """,
        params,
    )
    for row in rows:
        row["category_label"] = DOC_CATEGORIES.get(row["category"], row["category"])
    return rows


def delete_document(storage: Storage, settings: Settings, document_id: int, *, actor: str | None = None) -> None:
    row = get_document(storage, document_id)
    delete_file(settings, "documents", row["stored_name"])
    storage.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    storage.audit(actor=actor, action="document_deleted", entity="employee",
                  entity_id=row["employee_id"], detail=row["filename"])


def set_verified(storage: Storage, document_id: int, *, verified: bool, actor: str | None = None) -> dict[str, Any]:
    get_document(storage, document_id)
    storage.execute(
        "UPDATE documents SET verified = ?, verified_by = ?, verified_at = ? WHERE id = ?",
        (1 if verified else 0, actor if verified else None, utcnow_iso() if verified else None, document_id),
    )
    return get_document(storage, document_id)


def document_summary(storage: Storage, employee_id: int | None = None) -> dict[str, Any]:
    params: list[Any] = []
    clause = ""
    if employee_id:
        clause = "WHERE employee_id = ?"
        params.append(employee_id)
    rows = storage.query(
        f"SELECT category, COUNT(*) AS n FROM documents {clause} GROUP BY category", params
    )
    by_category = {row["category"]: int(row["n"]) for row in rows}
    return {
        "total": sum(by_category.values()),
        "by_category": by_category,
        "missing_required": [c for c in REQUIRED_DOCS if not by_category.get(c)],
    }


# --------------------------------------------------------------------- assets
ASSET_CATEGORIES = {
    "laptop": "Laptop",
    "phone": "Phone",
    "sim": "SIM card",
    "id_card": "ID card",
    "peripheral": "Peripheral",
    "other": "Other",
}

ASSET_STATUSES = {
    "in_stock": "In stock",
    "assigned": "Assigned",
    "repair": "In repair",
    "retired": "Retired",
}


def add_asset(
    storage: Storage,
    *,
    tag: str,
    name: str,
    category: str = "laptop",
    serial: str | None = None,
    purchase_date: str | None = None,
    cost: float = 0.0,
    notes: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    tag = (tag or "").strip().upper()
    name = (name or "").strip()
    if not tag:
        raise ValueError("An asset needs a tag (e.g. FITS-LAP-001).")
    if not name:
        raise ValueError("An asset needs a name.")
    if category not in ASSET_CATEGORIES:
        category = "other"
    if storage.query_one("SELECT id FROM assets WHERE tag = ?", (tag,)):
        raise ValueError(f"Asset tag '{tag}' already exists.")

    storage.execute(
        """
        INSERT INTO assets (tag, name, category, serial, purchase_date, cost, status, notes, created_at)
        VALUES (?,?,?,?,?,?, 'in_stock', ?,?)
        """,
        (tag, name, category, serial, purchase_date, float(cost or 0), notes, utcnow_iso()),
    )
    storage.audit(actor=actor, action="asset_added", entity="asset", detail=tag)
    return get_asset_by_tag(storage, tag)


def get_asset(storage: Storage, asset_id: int) -> dict[str, Any]:
    row = storage.query_one("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if row is None:
        raise ValueError("Asset not found.")
    return _decorate_asset(storage, row)


def get_asset_by_tag(storage: Storage, tag: str) -> dict[str, Any]:
    row = storage.query_one("SELECT * FROM assets WHERE tag = ?", (tag.strip().upper(),))
    if row is None:
        raise ValueError(f"Asset '{tag}' not found.")
    return _decorate_asset(storage, row)


def _decorate_asset(storage: Storage, row: dict[str, Any]) -> dict[str, Any]:
    row["category_label"] = ASSET_CATEGORIES.get(row["category"], row["category"])
    row["status_label"] = ASSET_STATUSES.get(row["status"], row["status"])
    holder = storage.query_one(
        """
        SELECT a.*, e.name AS employee_name, e.code AS employee_code
        FROM asset_assignments a JOIN employees e ON e.id = a.employee_id
        WHERE a.asset_id = ? AND a.returned_on IS NULL ORDER BY a.id DESC LIMIT 1
        """,
        (row["id"],),
    )
    row["holder"] = holder
    return row


def list_assets(
    storage: Storage, *, status: str | None = None, category: str | None = None, q: str | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if category and category != "all":
        clauses.append("category = ?")
        params.append(category)
    if q:
        clauses.append("(tag LIKE ? OR name LIKE ? OR serial LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = storage.query(f"SELECT * FROM assets {where} ORDER BY tag LIMIT ?", params)
    return [_decorate_asset(storage, row) for row in rows]


def assign_asset(
    storage: Storage,
    *,
    asset_id: int,
    employee_id: int,
    condition_note: str | None = None,
    actor: str | None = None,
    assigned_on: str | None = None,
) -> dict[str, Any]:
    asset = get_asset(storage, asset_id)
    employees_service.require_employee(storage, employee_id)
    if asset["holder"]:
        raise ValueError(
            f"{asset['tag']} is already with {asset['holder']['employee_name']}. Return it first."
        )
    storage.execute(
        """
        INSERT INTO asset_assignments (asset_id, employee_id, assigned_on, condition_note, assigned_by)
        VALUES (?,?,?,?,?)
        """,
        (asset_id, employee_id, assigned_on or orgtime.today_str(storage.settings),
         condition_note, actor),
    )
    storage.execute("UPDATE assets SET status = 'assigned' WHERE id = ?", (asset_id,))
    storage.audit(actor=actor, action="asset_assigned", entity="asset", entity_id=asset_id,
                  detail=f"to employee {employee_id}")
    return get_asset(storage, asset_id)


def return_asset(
    storage: Storage, *, asset_id: int, condition_note: str | None = None, actor: str | None = None
) -> dict[str, Any]:
    asset = get_asset(storage, asset_id)
    if not asset["holder"]:
        raise ValueError(f"{asset['tag']} is not currently assigned to anyone.")
    storage.execute(
        "UPDATE asset_assignments SET returned_on = ?, condition_note = COALESCE(?, condition_note) "
        "WHERE id = ?",
        (orgtime.today_str(storage.settings), condition_note, asset["holder"]["id"]),
    )
    storage.execute("UPDATE assets SET status = 'in_stock' WHERE id = ?", (asset_id,))
    storage.audit(actor=actor, action="asset_returned", entity="asset", entity_id=asset_id)
    return get_asset(storage, asset_id)


def assets_for_employee(storage: Storage, employee_id: int) -> list[dict[str, Any]]:
    rows = storage.query(
        """
        SELECT a.*, x.assigned_on, x.condition_note
        FROM asset_assignments x JOIN assets a ON a.id = x.asset_id
        WHERE x.employee_id = ? AND x.returned_on IS NULL ORDER BY a.tag
        """,
        (employee_id,),
    )
    for row in rows:
        row["category_label"] = ASSET_CATEGORIES.get(row["category"], row["category"])
    return rows


def asset_history(storage: Storage, asset_id: int) -> list[dict[str, Any]]:
    return storage.query(
        """
        SELECT x.*, e.name AS employee_name, e.code AS employee_code
        FROM asset_assignments x JOIN employees e ON e.id = x.employee_id
        WHERE x.asset_id = ? ORDER BY x.id DESC
        """,
        (asset_id,),
    )


def asset_stats(storage: Storage) -> dict[str, Any]:
    rows = storage.query("SELECT status, COUNT(*) AS n FROM assets GROUP BY status")
    by_status = {row["status"]: int(row["n"]) for row in rows}
    return {
        "total": sum(by_status.values()),
        "assigned": by_status.get("assigned", 0),
        "in_stock": by_status.get("in_stock", 0),
        "repair": by_status.get("repair", 0),
        "retired": by_status.get("retired", 0),
        "value": float(storage.scalar("SELECT COALESCE(SUM(cost),0) AS n FROM assets", (), 0)),
    }


# ------------------------------------------------------------------- expenses
EXPENSE_CATEGORIES = {
    "travel": "Travel",
    "food": "Food",
    "accommodation": "Accommodation",
    "internet": "Internet / phone",
    "other": "Other",
}


def submit_claim(
    storage: Storage,
    settings: Settings,
    *,
    employee_id: int,
    amount: float,
    expense_date: str,
    category: str = "travel",
    description: str | None = None,
    receipt: tuple[bytes, str] | None = None,
    submitted_by: str | None = None,
) -> dict[str, Any]:
    employees_service.require_employee(storage, employee_id)
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid amount.") from None
    if amount <= 0:
        raise ValueError("The amount must be greater than zero.")
    if amount > 500_000:
        raise ValueError("That amount looks wrong — split the claim or ask HR.")
    if category not in EXPENSE_CATEGORIES:
        category = "other"
    try:
        day = orgtime.parse_date(expense_date)
    except ValueError:
        raise ValueError("Enter a valid expense date.") from None
    if day > orgtime.today(settings):
        raise ValueError("The expense date cannot be in the future.")

    receipt_name = receipt_mime = None
    receipt_size = None
    if receipt:
        data, original = receipt
        stored = save_upload(settings, "receipts", data, original_name=original)
        receipt_name = stored.stored_name
        receipt_mime = stored.mime
        receipt_size = stored.size

    cursor = storage.execute(
        """
        INSERT INTO expense_claims (employee_id, category, amount, expense_date, description,
                                    receipt_name, receipt_mime, receipt_size, status, submitted_at)
        VALUES (?,?,?,?,?,?,?,?, 'pending', ?)
        """,
        (employee_id, category, round(amount, 2), day.isoformat(), description,
         receipt_name, receipt_mime, receipt_size, utcnow_iso()),
    )
    storage.audit(actor=submitted_by, action="expense_submitted", entity="employee",
                  entity_id=employee_id, detail=f"{category} {amount:.2f}")
    return get_claim(storage, int(cursor.lastrowid))


def get_claim(storage: Storage, claim_id: int) -> dict[str, Any]:
    row = storage.query_one(
        """
        SELECT c.*, e.name AS employee_name, e.code AS employee_code
        FROM expense_claims c JOIN employees e ON e.id = c.employee_id WHERE c.id = ?
        """,
        (claim_id,),
    )
    if row is None:
        raise ValueError("Claim not found.")
    row["category_label"] = EXPENSE_CATEGORIES.get(row["category"], row["category"])
    row["has_receipt"] = bool(row["receipt_name"])
    return row


def list_claims(
    storage: Storage, *, employee_id: int | None = None, status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if employee_id:
        clauses.append("c.employee_id = ?")
        params.append(employee_id)
    if status and status != "all":
        clauses.append("c.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = storage.query(
        f"""
        SELECT c.*, e.name AS employee_name, e.code AS employee_code
        FROM expense_claims c JOIN employees e ON e.id = c.employee_id
        {where}
        ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END, c.id DESC LIMIT ?
        """,
        params,
    )
    for row in rows:
        row["category_label"] = EXPENSE_CATEGORIES.get(row["category"], row["category"])
        row["has_receipt"] = bool(row["receipt_name"])
    return rows


def decide_claim(
    storage: Storage, *, claim_id: int, approve: bool, decided_by: str | None = None, note: str | None = None
) -> dict[str, Any]:
    claim = get_claim(storage, claim_id)
    if claim["status"] != "pending":
        raise ValueError(f"That claim is already {claim['status']}.")
    storage.execute(
        "UPDATE expense_claims SET status = ?, decided_at = ?, decided_by = ?, note = ? WHERE id = ?",
        ("approved" if approve else "rejected", utcnow_iso(), decided_by, note, claim_id),
    )
    storage.audit(
        actor=decided_by, action="expense_approved" if approve else "expense_rejected",
        entity="employee", entity_id=claim["employee_id"],
        detail=f"{claim['category']} {claim['amount']:.2f}",
    )
    return get_claim(storage, claim_id)


def expense_stats(storage: Storage, *, employee_id: int | None = None) -> dict[str, Any]:
    clause, params = "", []
    if employee_id:
        clause, params = "WHERE employee_id = ?", [employee_id]
    rows = storage.query(f"SELECT status, COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM expense_claims {clause} GROUP BY status", params)
    by_status = {row["status"]: {"count": int(row["n"]), "total": float(row["total"])} for row in rows}
    return {
        "pending": by_status.get("pending", {"count": 0, "total": 0.0}),
        "approved": by_status.get("approved", {"count": 0, "total": 0.0}),
        "rejected": by_status.get("rejected", {"count": 0, "total": 0.0}),
        "paid": by_status.get("paid", {"count": 0, "total": 0.0}),
        "pending_count": by_status.get("pending", {"count": 0})["count"],
    }


def recent_expense_dates(storage: Storage, employee_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT * FROM expense_claims WHERE employee_id = ? ORDER BY id DESC LIMIT ?",
        (employee_id, limit),
    )
