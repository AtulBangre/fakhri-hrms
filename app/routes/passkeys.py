"""Sign in with a fingerprint, face or device PIN (WebAuthn passkeys).

A passkey adds a way to sign in. It does not replace the password, and it never
sends anything biometric to the server: the phone checks the finger or face itself
and only signs a one-time challenge.

Passkeys only work on the app's own https address, or on ``localhost`` for testing.
Kiosk logins cannot use them. Credentials live in ``app_settings``, one entry per
user, so no table is added.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .. import auth
from ..auth import SessionUser, _sign
from ..config import Settings
from ..deps import get_session, get_settings_dep, get_storage, redirect, render
from ..services import alerts, orgtime
from .auth_routes import _home_for, is_safe_redirect

router = APIRouter(tags=["passkeys"])

COOKIE = "hrms_passkey_challenge"
CHALLENGE_SECONDS = 180
MAX_PASSKEYS = 5
FAILURE_NAME = "passkey"


# ---------------------------------------------------------------- relying party
def _relying_party(request: Request, settings: Settings) -> tuple[str, str] | None:
    """``(rp_id, origin)`` when this request came through the app's real address."""
    base = urlsplit(settings.public_base_url)
    host = base.hostname
    if not host or request.url.hostname != host:
        return None
    if base.scheme != "https" and host != "localhost":
        return None
    return host, f"{base.scheme}://{base.netloc}"


def _required_party(request: Request, settings: Settings) -> tuple[str, str]:
    party = _relying_party(request, settings)
    if party is None:
        raise HTTPException(status_code=404, detail="Passkeys are not available at this address.")
    return party


# ------------------------------------------------------------------ one-time challenge
def _challenge_cookie(response: Response, challenge: bytes, settings: Settings) -> None:
    """Keep the challenge in a signed, short-lived cookie, so a stranger writes nothing here."""
    payload = f"{bytes_to_base64url(challenge)}.{int(time.time()) + CHALLENGE_SECONDS}"
    signed = f"{payload}.{_sign(payload.encode().hex(), settings.load_or_create_secret())}"
    response.set_cookie(COOKIE, signed, max_age=CHALLENGE_SECONDS, httponly=True, samesite="strict",
                        secure=settings.secure_cookies, path="/api/passkey")


def _read_challenge(request: Request, storage, settings: Settings) -> bytes:
    raw = request.cookies.get(COOKIE) or ""
    parts = raw.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="Please try again.")
    payload = f"{parts[0]}.{parts[1]}"
    expected = _sign(payload.encode().hex(), settings.load_or_create_secret())
    if not hmac.compare_digest(expected, parts[2]) or int(parts[1]) < time.time():
        raise HTTPException(status_code=400, detail="That took too long. Please try again.")
    # Each challenge works once, even if an answer to it is captured and replayed.
    fingerprint = hashlib.sha256(parts[0].encode()).hexdigest()[:32]
    if not alerts.claim(storage, f"passkey:{fingerprint}", orgtime.today(settings)):
        raise HTTPException(status_code=400, detail="Please try again.")
    return base64url_to_bytes(parts[0])


# ------------------------------------------------------------------ stored passkeys
def _list_key(user_id: int) -> str:
    return f"passkeys:{int(user_id)}"


def _index_key(credential_id: str) -> str:
    return f"passkey_owner:{credential_id}"


def _load(storage, user_id: int) -> list[dict]:
    try:
        rows = json.loads(storage.get_setting(_list_key(user_id), "[]") or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def _save(storage, user_id: int, rows: list[dict]) -> None:
    storage.set_setting(_list_key(user_id), json.dumps(rows))


def _member(request: Request) -> SessionUser:
    user = get_session(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    if not (user.is_hr or user.is_employee):
        raise HTTPException(status_code=403, detail="Not available on this login")
    return user


def _member_with_csrf(request: Request, user: SessionUser = Depends(_member)) -> SessionUser:
    if not hmac.compare_digest(request.headers.get("x-csrf-token") or "", user.csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return user


# ------------------------------------------------------------------ page
@router.get("/account/passkeys")
def passkeys_page(request: Request, storage=Depends(get_storage), settings=Depends(get_settings_dep)):
    user = get_session(request)
    if user is None:
        return redirect("/login")
    if not (user.is_hr or user.is_employee):
        return redirect("/kiosk")
    return render(
        request, "passkeys.html", active="me-profile" if user.is_employee else "settings", user=user,
        passkeys=_load(storage, user.id), available=_relying_party(request, settings) is not None,
    )


@router.post("/account/passkeys/remove")
def remove_passkey(
    user: SessionUser = Depends(_member),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    credential_id: str = Form(...),
):
    auth.verify_form_csrf(user, csrf)
    rows = _load(storage, user.id)
    kept = [row for row in rows if row["id"] != credential_id]
    if len(kept) != len(rows):
        _save(storage, user.id, kept)
        storage.execute("DELETE FROM app_settings WHERE key = ?", (_index_key(credential_id),))
        storage.audit(actor=user.username, action="passkey_removed", entity="user", entity_id=user.id)
    return redirect("/account/passkeys", ok="Removed from this account.")


# ------------------------------------------------------------------ adding one
@router.post("/api/passkey/register/options")
def register_options(
    request: Request,
    user: SessionUser = Depends(_member_with_csrf),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    rp_id, _origin = _required_party(request, settings)
    rows = _load(storage, user.id)
    if len(rows) >= MAX_PASSKEYS:
        raise HTTPException(status_code=409, detail=f"An account can have {MAX_PASSKEYS} devices. Remove one first.")
    options = generate_registration_options(
        rp_id=rp_id, rp_name=settings.org_name, user_id=str(user.id).encode(), user_name=user.username,
        user_display_name=user.display_name or user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["id"])) for row in rows],
    )
    response = Response(options_to_json(options), media_type="application/json")
    _challenge_cookie(response, options.challenge, settings)
    return response


@router.post("/api/passkey/register/verify")
async def register_verify(
    request: Request,
    user: SessionUser = Depends(_member_with_csrf),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    rp_id, origin = _required_party(request, settings)
    body = await request.json()
    challenge = _read_challenge(request, storage, settings)
    try:
        verified = verify_registration_response(
            credential=body.get("credential"), expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="This device could not be verified.") from exc
    credential_id = bytes_to_base64url(verified.credential_id)
    rows = _load(storage, user.id)
    if len(rows) >= MAX_PASSKEYS:
        raise HTTPException(status_code=409, detail=f"An account can have {MAX_PASSKEYS} devices.")
    rows.append({
        "id": credential_id,
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
        "name": str(body.get("name") or "This device")[:40],
        "created_at": orgtime.today_str(settings),
    })
    _save(storage, user.id, rows)
    storage.set_setting(_index_key(credential_id), str(user.id))
    storage.audit(actor=user.username, action="passkey_added", entity="user", entity_id=user.id)
    return {"ok": True}


# ------------------------------------------------------------------ signing in
@router.get("/api/passkey/available")
def available(request: Request, settings=Depends(get_settings_dep)):
    return {"available": _relying_party(request, settings) is not None}


@router.post("/api/passkey/login/options")
def login_options(request: Request, settings=Depends(get_settings_dep)):
    rp_id, _origin = _required_party(request, settings)
    options = generate_authentication_options(
        rp_id=rp_id, challenge=secrets.token_bytes(32), user_verification=UserVerificationRequirement.REQUIRED,
    )
    response = Response(options_to_json(options), media_type="application/json")
    _challenge_cookie(response, options.challenge, settings)
    return response


@router.post("/api/passkey/login/verify")
async def login_verify(request: Request, storage=Depends(get_storage), settings=Depends(get_settings_dep)):
    rp_id, origin = _required_party(request, settings)
    ip = request.client.host if request.client else None
    refused = JSONResponse({"detail": "Could not sign in with this device."}, status_code=401)
    if auth.login_locked(storage, FAILURE_NAME, ip):
        return JSONResponse({"detail": "Too many failed sign-ins. Wait 10 minutes and try again."}, status_code=429)
    body = await request.json()
    credential = body.get("credential") if isinstance(body, dict) else None
    challenge = _read_challenge(request, storage, settings)

    credential_id = (credential or {}).get("id") or ""
    owner = storage.get_setting(_index_key(credential_id), "") if credential_id else ""
    user = auth.get_user(storage, int(owner)) if owner.isdigit() else None
    if user is None or not user["active"] or user["role"] == auth.ROLE_KIOSK:
        auth.record_login_failure(storage, FAILURE_NAME, ip)
        return refused
    rows = _load(storage, user["id"])
    stored = next((row for row in rows if row["id"] == credential_id), None)
    if stored is None:
        auth.record_login_failure(storage, FAILURE_NAME, ip)
        return refused
    try:
        verified = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=int(stored.get("sign_count") or 0), require_user_verification=True,
        )
    except Exception:
        auth.record_login_failure(storage, FAILURE_NAME, ip)
        storage.audit(actor=user["username"][:40], action="passkey_login_failed", entity="user", ip=ip)
        return refused

    stored["sign_count"] = verified.new_sign_count
    _save(storage, user["id"], rows)
    auth.clear_login_failures(storage, FAILURE_NAME)
    lifetime = settings.hr_session_hours * 3600
    token, session = auth.create_session_token(user, settings, lifetime_seconds=lifetime)
    storage.audit(actor=user["username"], action="login", entity="user", entity_id=user["id"],
                  detail=f"{user['role']} via passkey", ip=ip)
    next_url = body.get("next")
    response = JSONResponse({"ok": True, "redirect": next_url if is_safe_redirect(next_url) else _home_for(session)})
    auth.set_session_cookie(response, token, settings, max_age=lifetime)
    return response
