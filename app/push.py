"""Web Push: notifications that reach a phone or computer while the app is closed.

The signing key lives in the data directory next to ``secret_key.txt``. Device
subscriptions are stored per user in ``app_settings``, so no table is added.

The server posts to whatever address a browser subscribes with, so only the known
browser push services are accepted. Payloads are encrypted for that one device: the
push service carries them but cannot read them.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .db import Storage

logger = logging.getLogger("hrms.push")

MAX_DEVICES_PER_USER = 6
ALLOWED_HOST_SUFFIXES = (
    "fcm.googleapis.com",          # Chrome, Edge on Android, Samsung Internet
    "push.apple.com",              # Safari on iPhone, iPad and Mac
    "push.services.mozilla.com",   # Firefox
    "notify.windows.com",          # Edge on Windows
)


class PushGone(Exception):
    """The browser has dropped this subscription, so it should be forgotten."""


def _vapid(settings: Settings):
    """The signing key, created on first use. Creation is safe across workers."""
    from py_vapid import Vapid01

    path = Path(settings.data_dir) / "vapid_private.pem"
    if not path.exists():
        vapid = Vapid01()
        vapid.generate_keys()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another worker just created it, so use that one
        else:
            with os.fdopen(handle, "wb") as stream:
                stream.write(vapid.private_pem())
    return Vapid01.from_file(str(path))


def public_key(settings: Settings) -> str | None:
    """The key browsers subscribe with, or ``None`` when push cannot run here."""
    try:
        from cryptography.hazmat.primitives import serialization

        raw = _vapid(settings).public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    except Exception as exc:  # a missing library must not break the rest of the app
        logger.warning("web push is unavailable: %s", exc)
        return None
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def valid_subscription(subscription: Any) -> bool:
    if not isinstance(subscription, dict):
        return False
    keys = subscription.get("keys")
    endpoint = subscription.get("endpoint")
    if not isinstance(endpoint, str) or not isinstance(keys, dict) or len(endpoint) > 1000:
        return False
    if not (isinstance(keys.get("p256dh"), str) and isinstance(keys.get("auth"), str)):
        return False
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES
    )


def _key(user_id: int) -> str:
    return f"push_subs:{int(user_id)}"


def subscriptions(storage: Storage, user_id: int) -> list[dict[str, Any]]:
    try:
        rows = json.loads(storage.get_setting(_key(user_id), "[]") or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def subscribe(storage: Storage, user_id: int, subscription: dict[str, Any]) -> None:
    """Remember a device. The newest devices are kept when there are too many."""
    clean = {
        "endpoint": subscription["endpoint"],
        "keys": {"p256dh": subscription["keys"]["p256dh"], "auth": subscription["keys"]["auth"]},
    }
    others = [row for row in subscriptions(storage, user_id) if row.get("endpoint") != clean["endpoint"]]
    storage.set_setting(_key(user_id), json.dumps((others + [clean])[-MAX_DEVICES_PER_USER:]))


def unsubscribe(storage: Storage, user_id: int, endpoint: str) -> None:
    rows = subscriptions(storage, user_id)
    kept = [row for row in rows if row.get("endpoint") != endpoint]
    if len(kept) != len(rows):
        storage.set_setting(_key(user_id), json.dumps(kept))


def send(settings: Settings, subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    """Deliver to one device. Raises ``PushGone`` when the subscription has expired."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=_vapid(settings),
            vapid_claims={"sub": settings.push_subject},
            ttl=86400,
            timeout=15,
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            raise PushGone() from exc
        raise RuntimeError(f"push service answered {status}") from exc
