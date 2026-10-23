"""Regression tests for the bugs in plan.md (v3). Each test is named after its bug ID."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import Storage
from app.services import account
from tests.helpers import csrf_of


# ------------------------------------------------------------------ security
def _portal_client(client: TestClient, storage: Storage, employee: dict, password: str = "portal-pass-1"):
    user, _ = auth.ensure_employee_login(storage, employee, password=password)
    response = client.post("/login", data={"username": user["username"], "password": password},
                           follow_redirects=False)
    assert response.status_code == 303
    return user


def test_sec01_disabled_login_is_signed_out(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    assert client.get("/me", follow_redirects=False).status_code == 200
    auth.set_user_active(storage, user["id"], False)
    response = client.get("/me", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_sec01_deleted_login_is_signed_out(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    auth.delete_user(storage, user["id"])
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_sec02_password_change_ends_sessions(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    auth.change_password(storage, user["id"], "a-new-password-1")
    assert client.get("/me", follow_redirects=False).status_code == 303
    # signing in again with the new password works
    client.post("/login", data={"username": user["username"], "password": "a-new-password-1"})
    assert client.get("/me", follow_redirects=False).status_code == 200


def test_sec02_sign_out_everywhere(hr_client: TestClient, storage: Storage, kiosk_user):
    before = auth.get_user(storage, kiosk_user["id"])["session_version"]
    token = csrf_of(hr_client, "/settings")
    hr_client.post(f"/settings/users/{kiosk_user['id']}/signout", data={"_csrf": token})
    assert auth.get_user(storage, kiosk_user["id"])["session_version"] == before + 1


def test_sec03_reset_does_not_reenable_disabled_login(storage: Storage, settings, staff):
    user, _ = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    code = account.issue_otp(storage, settings, user)
    auth.set_user_active(storage, user["id"], False)
    ok, _ = account.complete_reset(storage, settings, username=user["username"], code=code,
                                   new_password="brand-new-pass-1")
    assert not ok
    assert auth.get_user(storage, user["id"])["active"] == 0
