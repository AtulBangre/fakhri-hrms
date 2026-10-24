"""Performance appraisal: HR cycles and reviews, employee self review."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_employee, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import appraisal, orgtime

router = APIRouter(tags=["appraisal"])

DEFAULT_GOALS = (
    "Quality of work | 30\n"
    "Delivery and ownership of targets | 30\n"
    "Teamwork and communication | 20\n"
    "Attendance and discipline | 10\n"
    "Learning and initiative | 10"
)


def _scores(form, prefix: str) -> dict[int, str]:
    found: dict[int, str] = {}
    for key, value in form.multi_items():
        if key.startswith(prefix):
            try:
                found[int(key[len(prefix):])] = value
            except ValueError:
                continue
    return found


# ====================================================================== HR
@router.get("/appraisals")
def cycles_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    today = orgtime.today(settings)
    return render(
        request, "appraisals.html", active="appraisals", user=user,
        cycles=appraisal.list_cycles(storage), default_goals=DEFAULT_GOALS,
        default_start=f"{today.year - 1 if today.month < 4 else today.year}-04-01",
        default_end=f"{today.year if today.month < 4 else today.year + 1}-03-31",
    )


@router.post("/appraisals/cycles")
def create_cycle(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    name: str = Form(""),
    period_start: str = Form(""),
    period_end: str = Form(""),
    self_due: str = Form(""),
    goals: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        cycle = appraisal.create_cycle(storage, name=name, period_start=period_start, period_end=period_end,
                                       self_due=self_due, goals_template=goals, actor=user.username)
    except appraisal.ValidationError as exc:
        return redirect("/appraisals", err=str(exc))
    return redirect(f"/appraisals/cycles/{cycle['id']}", ok="Cycle created as a draft. Launch it when ready.")


@router.get("/appraisals/cycles/{cycle_id}")
def cycle_page(
    request: Request,
    cycle_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    try:
        cycle = appraisal.get_cycle(storage, cycle_id)
    except appraisal.ValidationError as exc:
        return redirect("/appraisals", err=str(exc))
    rows = appraisal.cycle_appraisals(storage, cycle_id)
    distribution = {score: 0 for score in range(1, 6)}
    for row in rows:
        if row["final_rating"] is not None:
            distribution[min(5, max(1, int(round(row["final_rating"]))))] += 1
    return render(
        request, "appraisal_cycle.html", active="appraisals", user=user, cycle=cycle, rows=rows,
        distribution=distribution, labels=appraisal.RATING_LABELS,
        progress={
            "total": len(rows),
            "self_done": sum(1 for r in rows if r["status"] != "self_review"),
            "completed": sum(1 for r in rows if r["status"] == "completed"),
        },
    )


@router.post("/appraisals/cycles/{cycle_id}/{action}")
def cycle_action(
    request: Request,
    cycle_id: int,
    action: str,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    back = f"/appraisals/cycles/{cycle_id}"
    try:
        if action == "launch":
            count = appraisal.launch_cycle(storage, cycle_id, actor=user.username)
            return redirect(back, ok=f"Cycle launched for {count} employee(s). They can self-review from their portal.")
        if action == "close":
            appraisal.close_cycle(storage, cycle_id, actor=user.username)
            return redirect(back, ok="Cycle closed.")
        if action == "delete":
            appraisal.delete_cycle(storage, cycle_id, actor=user.username)
            return redirect("/appraisals", ok="Draft cycle deleted.")
    except appraisal.ValidationError as exc:
        return redirect(back, err=str(exc))
    return redirect(back, err="Unknown action.")


@router.get("/appraisals/{appraisal_id}")
def review_page(
    request: Request,
    appraisal_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    try:
        row = appraisal.get_appraisal(storage, appraisal_id)
    except appraisal.ValidationError as exc:
        return redirect("/appraisals", err=str(exc))
    history = [item for item in appraisal.for_employee(storage, row["employee_id"]) if item["id"] != appraisal_id]
    return render(
        request, "appraisal_review.html", active="appraisals", user=user, row=row, history=history,
        labels=appraisal.RATING_LABELS, rating_label=appraisal.rating_label,
    )


@router.post("/appraisals/{appraisal_id}/review")
async def save_review(
    request: Request,
    appraisal_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    form = await request.form()
    verify_form_csrf(user, str(form.get("_csrf") or ""))
    complete = form.get("complete") == "1"
    try:
        appraisal.review(
            storage, appraisal_id, scores=_scores(form, "score_"), notes=_scores(form, "note_"),
            comment=str(form.get("comment") or ""), final_rating=form.get("final_rating"),
            increment_pct=form.get("increment_pct"), complete=complete, actor=user.username,
        )
    except appraisal.ValidationError as exc:
        return redirect(f"/appraisals/{appraisal_id}", err=str(exc))
    return redirect(f"/appraisals/{appraisal_id}", ok="Review completed." if complete else "Draft saved.")


@router.post("/appraisals/{appraisal_id}/goals")
def add_goal(
    request: Request,
    appraisal_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    title: str = Form(""),
    weight: str = Form("10"),
    target: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        appraisal.add_goal(storage, appraisal_id, title=title, weight=float(weight or 1), target=target,
                           actor=user.username)
    except (appraisal.ValidationError, ValueError) as exc:
        return redirect(f"/appraisals/{appraisal_id}", err=str(exc))
    return redirect(f"/appraisals/{appraisal_id}", ok="Goal added.")


@router.post("/appraisals/{appraisal_id}/withdraw")
def withdraw(
    request: Request,
    appraisal_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    try:
        appraisal.withdraw(storage, appraisal_id, actor=user.username)
    except appraisal.ValidationError as exc:
        return redirect(f"/appraisals/{appraisal_id}", err=str(exc))
    return redirect(f"/appraisals/{appraisal_id}", ok="Marked not applicable — it no longer blocks closing the cycle.")


@router.post("/appraisals/{appraisal_id}/reopen")
def reopen(
    request: Request,
    appraisal_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    try:
        appraisal.reopen(storage, appraisal_id, actor=user.username)
    except appraisal.ValidationError as exc:
        return redirect(f"/appraisals/{appraisal_id}", err=str(exc))
    return redirect(f"/appraisals/{appraisal_id}", ok="Sent back to the employee for self review.")


@router.post("/appraisal-goals/{goal_id}/delete")
def delete_goal(
    request: Request,
    goal_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    try:
        appraisal_id = appraisal.remove_goal(storage, goal_id)
    except appraisal.ValidationError as exc:
        return redirect("/appraisals", err=str(exc))
    return redirect(f"/appraisals/{appraisal_id}", ok="Goal removed.")


# ===================================================================== ESS
@router.get("/me/appraisal")
def my_appraisal(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    items = appraisal.for_employee(storage, int(user.employee_id or 0))
    return render(
        request, "me_appraisal.html", active="me-appraisal", user=user, items=items,
        labels=appraisal.RATING_LABELS, rating_label=appraisal.rating_label,
    )


@router.post("/me/appraisal/{appraisal_id}")
async def save_self_review(
    request: Request,
    appraisal_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    form = await request.form()
    verify_form_csrf(user, str(form.get("_csrf") or ""))
    submit = form.get("submit") == "1"
    try:
        appraisal.submit_self(
            storage, appraisal_id, employee_id=int(user.employee_id or 0),
            scores=_scores(form, "score_"), notes=_scores(form, "note_"),
            comment=str(form.get("comment") or ""), submit=submit, actor=user.username,
        )
    except appraisal.ValidationError as exc:
        return redirect("/me/appraisal", err=str(exc))
    return redirect("/me/appraisal", ok="Self review submitted to HR." if submit else "Draft saved.")
