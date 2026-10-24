"""HR review of attendance regularization (missed-punch) requests."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_hr, verify_form_csrf
from ..deps import get_storage, redirect, render
from ..services import regularization

router = APIRouter(prefix="/attendance/regularizations", tags=["attendance"])

TABS = ("pending", "approved", "rejected", "all")


@router.get("")
def queue(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    status: str = "pending",
):
    status = status if status in TABS else "pending"
    counts = {
        row["status"]: int(row["n"])
        for row in storage.query("SELECT status, COUNT(*) AS n FROM attendance_regularizations GROUP BY status")
    }
    return render(
        request, "regularizations.html", active="regularizations", user=user,
        rows=regularization.list_requests(storage, status=status, limit=300),
        status=status, counts=counts, tabs=TABS,
    )


@router.post("/bulk")
def bulk(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    action: str = Form(...),
    ids: list[int] = Form([]),
):
    verify_form_csrf(user, csrf)
    if action not in ("approve", "reject"):
        return redirect("/attendance/regularizations", err="Unknown action.")
    if not ids:
        return redirect("/attendance/regularizations", err="Select at least one request.")
    done, failed = 0, []
    decide = regularization.approve if action == "approve" else regularization.reject
    for request_id in ids:
        try:
            decide(storage, request_id, actor=user.username)
            done += 1
        except regularization.ValidationError as exc:
            failed.append(f"#{request_id}: {exc}")
    verb = "approved" if action == "approve" else "rejected"
    if failed:
        return redirect("/attendance/regularizations", err=f"{done} {verb}; skipped " + "; ".join(failed[:3]))
    return redirect("/attendance/regularizations", ok=f"{done} request(s) {verb}.")


@router.post("/{request_id}/{decision}")
def decide(
    request: Request,
    request_id: int,
    decision: str,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    note: str = Form(""),
    release_leave: str = Form(""),
):
    verify_form_csrf(user, csrf)
    if decision not in ("approve", "reject"):
        return redirect("/attendance/regularizations", err="Unknown action.")
    try:
        if decision == "approve":
            row = regularization.approve(storage, request_id, actor=user.username, note=note.strip() or None,
                                         release_leave=bool(release_leave))
        else:
            row = regularization.reject(storage, request_id, actor=user.username, note=note.strip() or None)
    except regularization.ValidationError as exc:
        return redirect("/attendance/regularizations", err=str(exc))
    return redirect("/attendance/regularizations",
                    ok=f"{row['name']} · {row['work_date']} {row['status']}.")
