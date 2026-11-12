"""Birthdays, anniversaries, HR reminders, attendance nudges, the bell and web push."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, notify, push
from app.services import alerts, attendance as attendance_service, orgtime, wishes
from tests.conftest import KIOSK_PASSWORD, KIOSK_USERNAME
from tests.helpers import csrf_of

# A Monday in a month with no holidays set up in these tests.
NOW = datetime(2026, 6, 22, 10, 0)
TODAY = NOW.date()
GOOD_DEVICE = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
    "keys": {"p256dh": "key", "auth": "secret"},
}


def _dated(storage, employee: dict, **fields) -> None:
    for column, value in fields.items():
        storage.execute(f"UPDATE employees SET {column} = ? WHERE id = ?", (value, employee["id"]))


def _rows(storage, *, channel: str | None = None, category: str | None = None) -> list[dict]:
    rows = storage.query("SELECT * FROM notifications ORDER BY id")
    return [r for r in rows if (not channel or r["channel"] == channel) and (not category or r["category"] == category)]


def _email(storage, employee, value="person@example.com"):
    storage.execute("UPDATE employees SET email = ? WHERE id = ?", (value, employee["id"]))


# ----------------------------------------------------------------------- wishes
def test_one_person_gets_a_new_birthday_line_every_year():
    lines = [wishes.pick(wishes.BIRTHDAY_WISHES, 42, 2026 + n) for n in range(len(wishes.BIRTHDAY_WISHES))]
    assert len(set(lines)) == len(lines)


@pytest.mark.parametrize("pool", [wishes.BIRTHDAY_OPENERS, wishes.BIRTHDAY_CLOSERS, wishes.ANNIVERSARY_WISHES,
                                  wishes.WORK_ANNIVERSARY_WISHES])
def test_every_wording_pool_changes_from_year_to_year(pool):
    assert len({wishes.pick(pool, 7, 2026 + n) for n in range(len(pool))}) == len(pool)


def test_people_with_the_same_birthday_get_different_lines():
    used: set[str] = set()
    lines = [wishes.pick(wishes.BIRTHDAY_WISHES, employee_id, 2026, used) for employee_id in range(1, 21)]
    assert len(set(lines)) == 20


# ----------------------------------------------------------------- celebrations
def test_upcoming_finds_birthdays_within_three_days_only(storage, staff):
    _dated(storage, staff[0], date_of_birth="1990-06-25")   # in 3 days
    _dated(storage, staff[1], date_of_birth="1990-06-26")   # in 4 days
    found = [i for i in alerts.upcoming(storage, TODAY) if i["kind"] == "birthday"]
    assert [i["employee"]["id"] for i in found] == [staff[0]["id"]]
    assert found[0]["when_label"] == "In 3 days"


def test_upcoming_handles_year_end_and_leap_day(storage, staff):
    _dated(storage, staff[0], date_of_birth="1990-01-01")
    _dated(storage, staff[1], date_of_birth="1992-02-29")
    assert [i["in_days"] for i in alerts.upcoming(storage, date(2026, 12, 30)) if i["kind"] == "birthday"] == [2]
    assert [i["employee"]["id"] for i in alerts.upcoming(storage, date(2026, 2, 28)) if i["kind"] == "birthday"] == [staff[1]["id"]]


def test_upcoming_skips_inactive_people_and_a_joining_day(storage, staff):
    _dated(storage, staff[0], date_of_birth="1990-06-22", status="inactive")
    _dated(storage, staff[1], joined_on="2026-06-22")
    _dated(storage, staff[2], joined_on="2024-06-23")
    found = alerts.upcoming(storage, TODAY)
    assert [(i["employee"]["id"], i["kind"], i["years"]) for i in found] == [(staff[2]["id"], "work", 2)]


def test_birthday_email_goes_once_with_a_formatted_wish_from_hr(storage, settings, staff):
    _dated(storage, staff[0], date_of_birth="1990-06-22")
    _email(storage, staff[0])
    alerts.run_due(storage, settings, NOW)
    alerts.run_due(storage, settings, NOW + timedelta(hours=2))  # a second worker, or a restart

    emails = _rows(storage, channel="email", category="birthday")
    assert len(emails) == 1
    first = staff[0]["name"].split()[0]
    assert emails[0]["recipient"] == "person@example.com" and first in emails[0]["subject"]
    assert "Team HR" in emails[0]["body"] and "<blockquote" in emails[0]["body"]
    expected = wishes.pick(wishes.BIRTHDAY_WISHES, staff[0]["id"], 2026)
    assert expected.replace("'", "&#x27;") in emails[0]["body"]


def test_wishes_wait_for_the_set_hour(storage, settings, staff):
    _dated(storage, staff[0], date_of_birth="1990-06-22")
    _email(storage, staff[0])
    alerts.run_due(storage, settings, NOW.replace(hour=settings.celebration_hour - 1))
    assert _rows(storage, category="birthday") == []


def test_wishes_can_be_switched_off(storage, settings, staff):
    settings.notify_celebrations = False
    _dated(storage, staff[0], date_of_birth="1990-06-22")
    _email(storage, staff[0])
    alerts.run_due(storage, settings, NOW)
    assert _rows(storage, category="birthday") == []


def test_anniversaries_send_their_own_wishes(storage, settings, staff):
    _dated(storage, staff[0], anniversary="2015-06-22")
    _dated(storage, staff[1], joined_on="2024-06-22")
    _email(storage, staff[0], "a@example.com")
    _email(storage, staff[1], "b@example.com")
    alerts.run_due(storage, settings, NOW)
    assert _rows(storage, channel="email", category="anniversary")[0]["recipient"] == "a@example.com"
    assert "2nd work anniversary" in _rows(storage, channel="email", category="work_anniversary")[0]["subject"]


@pytest.mark.parametrize("days,label", [(3, "in 3 days"), (1, "tomorrow"), (0, "today")])
def test_hr_is_reminded_three_days_ahead_tomorrow_and_on_the_day(storage, settings, staff, hr_user, days, label):
    storage.execute("UPDATE users SET email = 'hr@example.com' WHERE id = ?", (hr_user["id"],))
    _dated(storage, staff[0], date_of_birth=(TODAY + timedelta(days=days)).replace(year=1990).isoformat())
    alerts.run_due(storage, settings, NOW)
    rows = _rows(storage, category="celebration_reminder")
    assert {r["channel"] for r in rows} == {"email", "push"}
    assert all(staff[0]["name"] in r["subject"] and label in r["subject"] for r in rows)


def test_hr_is_not_reminded_two_days_ahead(storage, settings, staff, hr_user):
    _dated(storage, staff[0], date_of_birth="1990-06-24")
    alerts.run_due(storage, settings, NOW)
    assert _rows(storage, category="celebration_reminder") == []


def test_dashboard_highlights_what_is_coming_up(hr_client: TestClient, storage, settings, staff):
    today = orgtime.today(settings)
    _dated(storage, staff[0], date_of_birth=(today + timedelta(days=2)).replace(year=1990).isoformat())
    page = hr_client.get("/dashboard").text
    assert "celebrate-card" in page and staff[0]["name"] in page and "In 2 days" in page


# -------------------------------------------------------------------- attendance
def test_punches_confirm_to_the_employee_and_a_refused_one_does_not(storage, settings, staff, ess_credentials):
    moment = datetime(2026, 6, 22, 9, 30)
    notify.push_punch(storage, settings, staff[0],
                      attendance_service.mark_by_face(storage, settings, staff[0], moment=moment))
    notify.push_punch(storage, settings, staff[0],
                      attendance_service.mark_by_face(storage, settings, staff[0], moment=moment + timedelta(hours=1)))
    rows = _rows(storage, channel="push")
    assert [r["subject"] for r in rows] == ["Checked in at 09:30"]
    assert rows[0]["recipient"] == f"user:{ess_credentials['user']['id']}"


def test_reminders_go_to_those_who_missed_a_punch_once(storage, settings, staff, ess_credentials):
    other, _ = auth.ensure_employee_login(storage, staff[1], password="portal-pass-2")
    attendance_service.mark_by_face(storage, settings, staff[1], moment=datetime(2026, 6, 22, 9, 30))
    for _ in range(2):
        alerts.run_due(storage, settings, NOW)
    missed = _rows(storage, category="attendance_reminder")
    assert [(r["recipient"], r["subject"]) for r in missed] == [
        (f"user:{ess_credentials['user']['id']}", "You have not checked in yet")
    ]
    alerts.run_due(storage, settings, NOW.replace(hour=19, minute=5))
    out = _rows(storage, category="attendance_reminder")[-1]
    assert (out["recipient"], out["subject"]) == (f"user:{other['id']}", "Don't forget to check out")


def test_no_attendance_nudges_on_a_weekly_off(storage, settings, staff, ess_credentials):
    alerts.run_due(storage, settings, datetime(2026, 6, 21, 10, 0))  # a Sunday
    assert _rows(storage, category="attendance_reminder") == []
    assert _rows(storage, category="attendance_summary") == []


def test_hr_gets_one_daily_summary(storage, settings, staff, hr_user):
    for _ in range(2):
        alerts.run_due(storage, settings, NOW.replace(hour=11))
    rows = _rows(storage, category="attendance_summary")
    recipients = [r["recipient"] for r in rows]  # one each for every HR login
    assert rows and len(recipients) == len(set(recipients)) and "present" in rows[0]["body"]


def test_old_claims_and_inbox_rows_are_cleared(storage, settings, staff, hr_user):
    storage.execute("INSERT INTO app_settings (key, value) VALUES ('alert_day:2020-01-01:wishes', '1')")
    notify.push_hr(storage, settings, "old", "old")
    storage.execute("UPDATE notifications SET created_at = '2020-01-01T00:00:00Z'")
    alerts.run_due(storage, settings, NOW)
    assert storage.get_setting("alert_day:2020-01-01:wishes", "") == ""
    assert not [r for r in _rows(storage, channel="push") if r["subject"] == "old"]


# --------------------------------------------------------------------- the bell
def test_inbox_is_private_and_tracks_unread(ess_client: TestClient, storage, settings, staff, hr_user):
    notify.push_employee(storage, settings, staff[0]["id"], "Mine", "for me", "/me")
    notify.push_employee(storage, settings, staff[1]["id"], "Not a login", "dropped")
    notify.push_hr(storage, settings, "HR only", "for hr")
    data = ess_client.get("/api/inbox").json()
    assert [item["title"] for item in data["items"]] == ["Mine"] and data["unread"] == 1

    token = csrf_of(ess_client, "/me")
    latest = data["items"][0]["id"]
    assert ess_client.post("/api/inbox/seen", json={"last_id": latest}, headers={"X-CSRF-Token": token}).status_code == 200
    assert ess_client.get("/api/inbox").json()["unread"] == 0


def test_inbox_writes_need_the_csrf_token(ess_client: TestClient):
    assert ess_client.post("/api/inbox/seen", json={"last_id": 1}).status_code == 403
    assert ess_client.post("/api/push/subscribe", json=GOOD_DEVICE).status_code == 403


def test_inbox_is_closed_to_the_kiosk_and_strangers(client: TestClient, hr_user, kiosk_user):
    assert client.get("/api/inbox").status_code == 401
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    assert client.get("/api/inbox").status_code == 403


def test_bell_shows_for_people_with_a_login(hr_client: TestClient):
    assert 'id="bellBtn"' in hr_client.get("/dashboard").text


# ---------------------------------------------------------------------- web push
@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/x",                   # not https
    "https://evil.example.com/x",                    # not a push service
    "https://fcm.googleapis.com.evil.example.com/x", # a lookalike host
    "https://169.254.169.254/latest/meta-data",      # the server's own network
])
def test_only_real_push_services_are_accepted(endpoint):
    assert push.valid_subscription({**GOOD_DEVICE, "endpoint": endpoint}) is False


def test_a_device_can_subscribe_and_unsubscribe(ess_client: TestClient, storage, ess_credentials):
    headers = {"X-CSRF-Token": csrf_of(ess_client, "/me")}
    user_id = ess_credentials["user"]["id"]
    assert ess_client.post("/api/push/subscribe", json={**GOOD_DEVICE, "endpoint": "https://evil.example.com/x"},
                           headers=headers).status_code == 422
    assert ess_client.post("/api/push/subscribe", json=GOOD_DEVICE, headers=headers).status_code == 200
    assert ess_client.post("/api/push/subscribe", json=GOOD_DEVICE, headers=headers).status_code == 200
    assert push.subscriptions(storage, user_id) == [GOOD_DEVICE]
    ess_client.post("/api/push/unsubscribe", json={"endpoint": GOOD_DEVICE["endpoint"]}, headers=headers)
    assert push.subscriptions(storage, user_id) == []


def test_public_key_is_stable_and_private_key_stays_private(ess_client: TestClient, settings):
    first = push.public_key(settings)
    assert first and first == push.public_key(settings)
    assert (settings.data_dir / "vapid_private.pem").stat().st_mode & 0o077 == 0
    assert ess_client.get("/api/inbox").json()["push_key"] == first


def test_push_reaches_devices_and_forgets_expired_ones(storage, settings, ess_credentials, staff, monkeypatch):
    user_id = ess_credentials["user"]["id"]
    gone = {**GOOD_DEVICE, "endpoint": "https://fcm.googleapis.com/fcm/send/gone"}
    push.subscribe(storage, user_id, GOOD_DEVICE)
    push.subscribe(storage, user_id, gone)
    sent = []

    def fake_send(_settings, device, payload):
        if device["endpoint"] == gone["endpoint"]:
            raise push.PushGone()
        sent.append((device["endpoint"], payload))

    monkeypatch.setattr(push, "send", fake_send)
    notify.push_employee(storage, settings, staff[0]["id"], "Hello", "World", "/me/leave")
    results = notify.dispatch(storage, settings)
    assert results[0]["ok"] and "1 device" in results[0]["receipt"]
    assert sent[0][1]["title"] == "Hello" and sent[0][1]["url"] == "/me/leave"
    assert push.subscriptions(storage, user_id) == [GOOD_DEVICE]


def test_push_without_a_device_is_not_a_failure(storage, settings, staff, ess_credentials):
    notify.push_employee(storage, settings, staff[0]["id"], "Hello", "World")
    assert notify.dispatch(storage, settings)[0]["ok"] is True


def test_a_disabled_login_stops_receiving_push(storage, settings, ess_credentials, staff, monkeypatch):
    user_id = ess_credentials["user"]["id"]
    push.subscribe(storage, user_id, GOOD_DEVICE)
    notify.push_employee(storage, settings, staff[0]["id"], "Hello", "World")
    storage.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    monkeypatch.setattr(push, "send", lambda *a: pytest.fail("must not send to a disabled login"))
    assert notify.dispatch(storage, settings)[0]["receipt"] == "login is not active"


def test_a_failed_push_is_retried(storage, settings, ess_credentials, staff, monkeypatch):
    push.subscribe(storage, ess_credentials["user"]["id"], GOOD_DEVICE)
    notify.push_employee(storage, settings, staff[0]["id"], "Hello", "World")

    def fail(*_args):
        raise RuntimeError("push service answered 503")

    monkeypatch.setattr(push, "send", fail)
    assert notify.dispatch(storage, settings)[0]["will_retry"] is True


def test_payslip_push_never_shows_an_amount(hr_client: TestClient, storage, settings, staff, ess_credentials):
    from app.services import payroll

    payroll.generate_run(storage, settings, "2026-01")
    token = csrf_of(hr_client, "/payroll")
    hr_client.post("/payroll/status", data={"_csrf": token, "period": "2026-01", "status": "finalized"})
    rows = _rows(storage, channel="push", category="payslip")
    assert rows and all("₹" not in r["body"] and "Rs" not in r["body"] for r in rows)
    assert str(int(staff[0]["monthly_gross"])) not in rows[0]["body"]


def test_housekeeping_retention_policy(storage, settings):
    import time
    from datetime import date, datetime, timedelta, timezone
    from app.services import alerts

    today = date(2026, 11, 12)

    # 1. Notifications: 1 old (>30d), 1 recent (<30d)
    storage.execute(
        "INSERT INTO notifications (created_at, channel, recipient, subject, body, status) VALUES (?,?,?,?,?,?)",
        ((today - timedelta(days=35)).isoformat(), "whatsapp", "123", "Old", "Old WA", "sent"),
    )
    storage.execute(
        "INSERT INTO notifications (created_at, channel, recipient, subject, body, status) VALUES (?,?,?,?,?,?)",
        ((today - timedelta(days=10)).isoformat(), "whatsapp", "123", "Recent", "Recent WA", "sent"),
    )

    # 2. Audit logs: 1 old (>120d), 1 recent (<120d)
    storage.execute(
        "INSERT INTO audit_log (at, action, entity) VALUES (?,?,?)",
        ((today - timedelta(days=130)).isoformat(), "old_action", "system"),
    )
    storage.execute(
        "INSERT INTO audit_log (at, action, entity) VALUES (?,?,?)",
        ((today - timedelta(days=60)).isoformat(), "recent_action", "system"),
    )

    # 3. Password tokens: 1 expired, 1 valid, 1 used
    now_epoch = time.time()
    storage.execute(
        "INSERT INTO users (username, password_hash, salt, role, created_at) VALUES ('temp_user', 'hash', 'salt', 'employee', '2026-11-12T00:00:00')"
    )
    user_id = storage.execute("SELECT id FROM users WHERE username = 'temp_user'").fetchone()[0]
    storage.execute(
        "INSERT INTO password_tokens (user_id, token_hash, created_at, expires_at) VALUES (?,?,?,?)",
        (user_id, "expired_token", now_epoch - 7200, now_epoch - 3600),
    )
    storage.execute(
        "INSERT INTO password_tokens (user_id, token_hash, created_at, expires_at) VALUES (?,?,?,?)",
        (user_id, "valid_token", now_epoch, now_epoch + 3600),
    )
    storage.execute(
        "INSERT INTO password_tokens (user_id, token_hash, created_at, expires_at, used_at) VALUES (?,?,?,?,?)",
        (user_id, "used_token", now_epoch, now_epoch + 3600, now_epoch),
    )

    # Run housekeeping
    purged = alerts._housekeeping(storage, today)
    assert purged["notifications"] >= 1
    assert purged["audit_log"] >= 1
    assert purged["password_tokens"] >= 2  # expired + used

    # Verify rows in DB
    remaining_notifs = storage.query("SELECT * FROM notifications")
    assert all("Old" not in (n.get("subject") or "") for n in remaining_notifs)
    assert any("Recent" in (n.get("subject") or "") for n in remaining_notifs)

    remaining_audits = storage.query("SELECT * FROM audit_log")
    assert all(a["action"] != "old_action" for a in remaining_audits)
    assert any(a["action"] == "recent_action" for a in remaining_audits)

    remaining_tokens = storage.query("SELECT * FROM password_tokens WHERE user_id = ?", (user_id,))
    assert len(remaining_tokens) == 1
    assert remaining_tokens[0]["token_hash"] == "valid_token"


def test_manual_housekeeping_route(hr_client):
    from tests.helpers import csrf_of
    token = csrf_of(hr_client, "/settings")
    res = hr_client.post("/settings/housekeeping/run", data={"_csrf": token}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/settings#server-notes")

