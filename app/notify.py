"""Outbound notifications: email, WhatsApp and SMS.

Everything goes through a **queue in SQLite** rather than being sent inline:

* a kiosk scan must return in milliseconds, not wait for an SMTP handshake
* a provider outage must not fail an attendance mark
* every attempt is recorded, so HR can see exactly what was sent, when, and why
  something failed (``/settings`` → Notification log)

A background thread drains the queue. Three drivers ship with it:

``console``   writes the message to ``data/outbox/`` and marks it sent — the
              default, so the whole system is demonstrable with no credentials
``smtp``      real email (any provider: Gmail app-password, SES, Postmark, …)
``twilio``    WhatsApp or SMS through Twilio's HTTP API
``meta``      WhatsApp through Meta's Cloud API

Switching provider is two settings and a restart; nothing else in the codebase
knows how a message travels.
"""

from __future__ import annotations

import json
import logging
import re
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path
from typing import Any, Iterable

import httpx

from .config import BRAND, Settings
from .db import Storage, utcnow_iso

logger = logging.getLogger("hrms.notify")

CHANNELS = ("email", "whatsapp", "sms")


# --------------------------------------------------------------------- helpers
def _digits(value: str | None) -> str:
    return re.sub(r"[^0-9+]", "", value or "")


def _sanitise(value: str, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())[:limit]


# --------------------------------------------------------------------- content
def _layout(settings: Settings, title: str, body_html: str, *, footer: str = "") -> str:
    """Minimal, email-client-safe HTML in the brand colours."""
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:{BRAND['surface']};font-family:Segoe UI,Helvetica,Arial,sans-serif">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:24px 12px">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:560px;background:#ffffff;border:1px solid {BRAND['border']};border-radius:14px;overflow:hidden">
        <tr><td style="background:{BRAND['primary']};color:#ffffff;padding:18px 22px">
          <div style="font-size:18px;font-weight:600">{escape(settings.org_name)}</div>
          <div style="font-size:12px;opacity:.85">HRMS · {escape(title)}</div>
        </td></tr>
        <tr><td style="padding:22px;color:{BRAND['ink']};font-size:14px;line-height:1.55">
          {body_html}
        </td></tr>
        <tr><td style="padding:14px 22px;background:#fcfbfb;border-top:1px solid {BRAND['border']};
                       font-size:11px;color:{BRAND['muted']}">
          {footer or "This is an automated message from your HRMS."}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _plain(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render(template: str, settings: Settings, **ctx: Any) -> tuple[str, str, str]:
    """Return ``(subject, html, text)`` for a known message template."""
    base = settings.public_base_url.rstrip("/")
    org = escape(settings.org_name)

    if template == "welcome":
        subject = f"Welcome to {settings.org_name} — your HR portal is ready"
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>Your employee portal is ready. Use it to see your attendance, download payslips and apply for leave.</p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin:14px 0'>"
            f"<tr><td style='padding:4px 0;color:{BRAND['muted']}'>Username</td>"
            f"<td style='padding:4px 12px;font-family:monospace'><b>{escape(ctx['username'])}</b></td></tr>"
            f"<tr><td style='padding:4px 0;color:{BRAND['muted']}'>Password</td>"
            f"<td style='padding:4px 12px;font-family:monospace'><b>{escape(ctx.get('password') or '(set by HR)')}</b></td></tr>"
            f"</table>"
            f"<p><a href='{base}/login' style='display:inline-block;background:{BRAND['primary']};color:#fff;"
            f"padding:10px 18px;border-radius:8px;text-decoration:none'>Open my portal</a></p>"
            f"<p style='color:{BRAND['muted']};font-size:12px'>Please change the password after your first sign-in.</p>"
        )
        footer = "Ask HR if you did not expect this message."

    elif template == "leave_requested":
        subject = f"Leave request from {ctx['name']} ({ctx['days']} day(s))"
        body = (
            f"<p><b>{escape(ctx['name'])}</b> ({escape(ctx['code'])}) requested leave.</p>"
            f"<ul><li><b>Type:</b> {escape(ctx['leave_type'])}</li>"
            f"<li><b>Dates:</b> {escape(ctx['from_date'])} → {escape(ctx['to_date'])}</li>"
            f"<li><b>Days:</b> {escape(str(ctx['days']))}</li>"
            f"<li><b>Reason:</b> {escape(ctx.get('reason') or '—')}</li></ul>"
            f"<p><a href='{base}/leave' style='display:inline-block;background:{BRAND['primary']};color:#fff;"
            f"padding:10px 18px;border-radius:8px;text-decoration:none'>Review &amp; approve</a></p>"
        )

    elif template == "leave_decided":
        decision = ctx["decision"]
        subject = f"Your leave was {decision}"
        colour = BRAND["success"] if decision == "approved" else BRAND["danger"]
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>Your leave request ({escape(ctx['from_date'])} → {escape(ctx['to_date'])}, "
            f"{escape(str(ctx['days']))} day(s)) was "
            f"<b style='color:{colour}'>{escape(decision)}</b>.</p>"
            + (f"<p style='color:{BRAND['muted']}'>Note: {escape(ctx['note'])}</p>" if ctx.get("note") else "")
            + f"<p><a href='{base}/me/leave'>See my leave</a></p>"
        )

    elif template == "punch_in":
        subject = f"Attendance marked — IN at {ctx['time']}"
        late = ctx.get("late_minutes") or 0
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>You checked in at <b>{escape(ctx['time'])}</b> on {escape(ctx['date'])}.</p>"
            + (f"<p style='color:{BRAND['warning']}'>You are {late} minute(s) late against the {escape(ctx['shift_start'])} shift.</p>"
               if late else "")
            + f"<p style='color:{BRAND['muted']};font-size:12px'>Scan again when you leave to record your check-out.</p>"
        )

    elif template == "check_out":
        subject = f"Attendance marked — OUT at {ctx['time']}"
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>You checked out at <b>{escape(ctx['time'])}</b>. Worked "
            f"<b>{escape(str(ctx.get('worked') or '—'))}</b>.</p>"
        )

    elif template == "payslip":
        subject = f"Payslip for {ctx['period']}"
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>Your payslip for <b>{escape(ctx['period'])}</b> is attached as a PDF.</p>"
            f"<ul><li>Gross: {escape(ctx['gross'])}</li>"
            f"<li>Deductions: {escape(ctx['deductions'])}</li>"
            f"<li><b>Net pay: {escape(ctx['net'])}</b></li></ul>"
            f"<p><a href='{base}/me/payslips'>Open my payslips</a></p>"
        )
        footer = "This payslip is confidential to the recipient."

    elif template == "otp":
        subject = f"Your password reset code: {ctx['code']}"
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>Use this code to reset your password. It expires in "
            f"<b>{escape(str(ctx['minutes']))} minutes</b>.</p>"
            f"<p style='font-size:28px;font-family:monospace;letter-spacing:6px;margin:18px 0'>"
            f"<b>{escape(ctx['code'])}</b></p>"
            f"<p style='color:{BRAND['muted']};font-size:12px'>If you did not ask for this, ignore the email — "
            f"your password stays unchanged.</p>"
        )
        footer = "Never share this code with anyone, including HR."

    elif template == "announcement":
        subject = ctx["title"]
        body = (
            f"<p><b>{escape(ctx['title'])}</b></p>"
            f"<div>{ctx['body_html']}</div>"
            f"<p style='color:{BRAND['muted']};font-size:12px'>Posted by HR on {escape(ctx['date'])}.</p>"
        )

    elif template == "profile_changed":
        subject = f"Profile change request from {ctx['name']}"
        body = (
            f"<p><b>{escape(ctx['name'])}</b> asked to update their profile.</p>"
            f"<p>Fields waiting for your approval: <b>{escape(ctx['fields'])}</b></p>"
            f"<p><a href='{base}/profile-requests' style='display:inline-block;background:{BRAND['primary']};"
            f"color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none'>Review the request</a></p>"
        )

    elif template == "profile_decided":
        decision = ctx["decision"]
        subject = f"Your profile change was {decision}"
        colour = BRAND["success"] if decision == "approved" else BRAND["danger"]
        body = (
            f"<p>Hi {escape(ctx['name'])},</p>"
            f"<p>Your request to change <b>{escape(ctx['field_label'])}</b> was "
            f"<b style='color:{colour}'>{escape(decision)}</b>.</p>"
            + (f"<p style='color:{BRAND['muted']}'>Note: {escape(ctx['note'])}</p>" if ctx.get("note") else "")
            + f"<p><a href='{base}/me/profile'>See my profile</a></p>"
        )

    elif template == "hr_alert":
        subject = ctx["subject"]
        body = f"<p>{escape(ctx['message'])}</p>" + (
            f"<p><a href='{base}{escape(ctx['link'])}'>Open in HRMS</a></p>" if ctx.get("link") else ""
        )

    else:  # pragma: no cover - defensive
        subject = ctx.get("subject", "Notification")
        body = f"<p>{escape(ctx.get('message', ''))}</p>"

    html = _layout(settings, subject, body, footer=ctx.get("footer", ""))
    return subject, html, _plain(html)


# --------------------------------------------------------------- HR alerts
def hr_emails(storage: Storage) -> list[str]:
    """Every active HR login with an email address (set under Settings → Logins)."""
    return [
        row["email"] for row in storage.query(
            "SELECT DISTINCT email FROM users WHERE role = 'hr' AND active = 1 AND email IS NOT NULL AND email != ''"
        )
    ]


def notify_hr(storage: Storage, settings: Settings, template: str, *, category: str, **ctx: Any) -> int:
    """Queue one message per HR address. Returns how many were queued."""
    queued = 0
    for address in hr_emails(storage):
        if queue_email(storage, address, template, settings=settings, category=category, **ctx):
            queued += 1
    return queued


# ----------------------------------------------------------------------- queue
def queue(
    storage: Storage,
    *,
    channel: str,
    recipient: str | None,
    subject: str | None,
    body: str,
    category: str = "system",
    meta: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> int | None:
    """Add one message to the outbox. Returns the row id (``None`` if skipped)."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel '{channel}'")
    address = (recipient or "").strip()
    if not address:
        logger.debug("notification skipped: no recipient for %s", category)
        return None
    cursor = storage.execute(
        """
        INSERT INTO notifications (created_at, channel, recipient, subject, body, category, status, meta)
        VALUES (?,?,?,?,?,?, 'pending', ?)
        """,
        (utcnow_iso(), channel, address, subject, body, category,
         json.dumps(meta) if meta else None),
    )
    return int(cursor.lastrowid)


def queue_email(
    storage: Storage,
    to: str | None,
    template: str,
    *,
    settings: Settings,
    category: str | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
    **ctx: Any,
) -> int | None:
    subject, html, _text = render(template, settings, **ctx)
    meta = {"template": template}
    if attachments:
        # attachments are written next to the message by the console driver and
        # re-generated by the SMTP driver from the payslip id
        meta["attachments"] = [name for name, _ in attachments]
    return queue(
        storage, channel="email", recipient=to, subject=subject, body=html,
        category=category or template, meta=meta, settings=settings,
    )


def queue_whatsapp(
    storage: Storage, to: str | None, template: str, *, settings: Settings, **ctx: Any
) -> int | None:
    body = _whatsapp_text(template, settings, **ctx)
    return queue(
        storage, channel="whatsapp", recipient=_digits(to), subject=None,
        body=body, category=template, settings=settings,
    )


def queue_sms(
    storage: Storage, to: str | None, template: str, *, settings: Settings, **ctx: Any
) -> int | None:
    body = _whatsapp_text(template, settings, **ctx)
    return queue(
        storage, channel="sms", recipient=_digits(to), subject=None,
        body=body[:320], category=template, settings=settings,
    )


def _whatsapp_text(template: str, settings: Settings, **ctx: Any) -> str:
    """Short, plain-language text for WhatsApp/SMS (no HTML)."""
    if template == "punch_in":
        late = ctx.get("late_minutes") or 0
        tail = f" You are {late} min late." if late else ""
        return (
            f"*{settings.org_name}* — attendance marked IN at {ctx['time']} on {ctx['date']}."
            f"{tail} Scan again when you leave."
        )
    if template == "check_out":
        return (
            f"*{settings.org_name}* — attendance marked OUT at {ctx['time']}. "
            f"Worked {ctx.get('worked') or '—'}."
        )
    if template == "leave_decided":
        return (
            f"*{settings.org_name}* — your leave ({ctx['from_date']} → {ctx['to_date']}) "
            f"was {ctx['decision']}."
        )
    if template == "leave_requested":
        return (
            f"*{settings.org_name}* — {ctx['name']} requested {ctx['days']} day(s) leave "
            f"({ctx['from_date']} → {ctx['to_date']}). Approve in the HRMS."
        )
    if template == "payslip":
        return f"*{settings.org_name}* — your payslip for {ctx['period']} has been sent to your email."
    if template == "otp":
        return f"*{settings.org_name}* — password reset code: {ctx['code']} (valid {ctx['minutes']} min)."
    return _sanitise(ctx.get("message") or "Notification from HRMS")


# --------------------------------------------------------------------- sending
class SendError(RuntimeError):
    pass


def _attachments_for(storage: Storage, settings: Settings, row: dict[str, Any]) -> list[tuple[str, bytes]]:
    """Regenerate any attachments this message refers to.

    Payslip emails carry the PDF as a real attachment rather than a link, because
    an attachment survives forwarding and archiving — which is what an employee
    actually needs when a landlord or a bank asks for proof of salary.
    """
    try:
        meta = json.loads(row.get("meta") or "{}")
    except json.JSONDecodeError:
        return []
    slip_id = meta.get("slip_id")
    if not slip_id:
        return []

    slip = storage.query_one(
        """
        SELECT p.*, e.code, e.name, e.department, e.designation, e.joined_on, e.id AS employee_id
        FROM payslips p JOIN employees e ON e.id = p.employee_id WHERE p.id = ?
        """,
        (int(slip_id),),
    )
    if slip is None:
        return []

    from .pdf import filename_for, render_payslip
    from .services import orgtime

    data = render_payslip(
        slip,
        settings=settings,
        org_name=storage.get_setting("org_name", settings.org_name),
        period_label=orgtime.month_label(slip["period"]),
    )
    return [(filename_for(slip), data)]


def _send_email(storage: Storage, settings: Settings, row: dict[str, Any]) -> str:
    """Deliver one email. Returns a short driver receipt for the log."""
    to = row["recipient"]
    attachments = _attachments_for(storage, settings, row)

    if not settings.smtp_host:
        # console driver: keep a real .eml on disk so it can be inspected
        outbox = Path(settings.data_dir) / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", to)[:40]
        message = EmailMessage()
        message["From"] = formataddr((settings.org_name, settings.smtp_from or f"hrms@{settings.org_name.lower().replace(' ', '')}.local"))
        message["To"] = to
        message["Subject"] = row["subject"] or "(no subject)"
        message["Message-ID"] = make_msgid(domain="hrms.local")
        message.set_content(_plain(row["body"]))
        message.add_alternative(row["body"], subtype="html")
        for filename, payload in attachments:
            message.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
        path = outbox / f"{stamp}-{safe}-{row['id']}.eml"
        path.write_bytes(bytes(message))
        return f"written to {path.relative_to(settings.data_dir)}"

    message = EmailMessage()
    message["From"] = formataddr((settings.org_name, settings.smtp_from or settings.smtp_user))
    message["To"] = to
    message["Subject"] = row["subject"] or "(no subject)"
    message["Message-ID"] = make_msgid()
    message.set_content(_plain(row["body"]))
    message.add_alternative(row["body"], subtype="html")
    for filename, payload in attachments:
        message.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)

    try:
        timeout = settings.smtp_timeout
        if settings.smtp_tls.lower() == "ssl":
            server: Any = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        with server:
            server.ehlo()
            if settings.smtp_tls.lower() == "starttls":
                server.starttls()
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(f"smtp: {exc}") from exc
    return "sent via smtp"


def _send_twilio(settings: Settings, row: dict[str, Any], *, whatsapp: bool) -> str:
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        raise SendError("twilio credentials are not configured")
    if not settings.twilio_from:
        raise SendError("twilio sender number is not configured")

    to = row["recipient"] if row["recipient"].startswith("+") else "+" + row["recipient"]
    sender = settings.twilio_from
    body = row["body"]
    if whatsapp:
        to = f"whatsapp:{to}"
        sender = f"whatsapp:{sender}" if not sender.startswith("whatsapp:") else sender
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    try:
        response = httpx.post(
            url,
            data={"From": sender, "To": to, "Body": body},
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise SendError(f"twilio: {exc}") from exc
    if response.status_code >= 400:
        raise SendError(f"twilio {response.status_code}: {response.text[:200]}")
    return f"twilio sid={response.json().get('sid', '?')}"


def _send_meta_whatsapp(settings: Settings, row: dict[str, Any]) -> str:
    if not (settings.meta_phone_id and settings.meta_token):
        raise SendError("meta whatsapp credentials are not configured")
    to = row["recipient"] if row["recipient"].startswith("+") else "+" + row["recipient"]
    url = f"https://graph.facebook.com/v20.0/{settings.meta_phone_id}/messages"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.meta_token}"},
            json={"messaging_product": "whatsapp", "to": to.lstrip("+"),
                  "type": "text", "text": {"body": row["body"]}},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise SendError(f"meta: {exc}") from exc
    if response.status_code >= 400:
        raise SendError(f"meta {response.status_code}: {response.text[:200]}")
    return "sent via meta cloud api"


def send_one(storage: Storage, settings: Settings, notification: dict[str, Any]) -> dict[str, Any]:
    """Attempt one queued message and update its row."""
    channel = notification["channel"]
    attempts = int(notification["attempts"]) + 1
    try:
        if channel == "email":
            receipt = _send_email(storage, settings, notification)
        elif channel == "whatsapp":
            if settings.whatsapp_provider == "twilio":
                receipt = _send_twilio(settings, notification, whatsapp=True)
            elif settings.whatsapp_provider == "meta":
                receipt = _send_meta_whatsapp(settings, notification)
            else:
                receipt = _write_console(settings, notification, "whatsapp")
        elif channel == "sms":
            if settings.sms_provider == "twilio":
                receipt = _send_twilio(settings, notification, whatsapp=False)
            else:
                receipt = _write_console(settings, notification, "sms")
        else:  # pragma: no cover - guarded at queue time
            raise SendError(f"unknown channel '{channel}'")

        storage.execute(
            "UPDATE notifications SET status='sent', attempts=?, sent_at=?, error=NULL, next_attempt_at=NULL "
            "WHERE id=?",
            (attempts, utcnow_iso(), notification["id"]),
        )
        logger.info("notification #%s sent (%s) — %s", notification["id"], channel, receipt)
        return {"ok": True, "receipt": receipt}
    except (SendError, ValueError, OSError) as exc:
        final = attempts >= len(RETRY_BACKOFF_MINUTES) + 1
        retry_at = None if final else (
            datetime.now(timezone.utc) + timedelta(minutes=RETRY_BACKOFF_MINUTES[attempts - 1])
        ).isoformat()
        storage.execute(
            "UPDATE notifications SET status=?, attempts=?, error=?, next_attempt_at=? WHERE id=?",
            ("failed" if final else "pending", attempts, str(exc)[:400], retry_at, notification["id"]),
        )
        logger.warning("notification #%s failed (attempt %s): %s", notification["id"], attempts, exc)
        return {"ok": False, "error": str(exc), "will_retry": not final}


def _write_console(settings: Settings, row: dict[str, Any], channel: str) -> str:
    outbox = Path(settings.data_dir) / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = outbox / f"{stamp}-{channel}-{row['id']}.txt"
    path.write_text(
        f"channel : {channel}\nrecipient: {row['recipient']}\n"
        f"category : {row['category']}\n---\n{row['body']}\n",
        encoding="utf-8",
    )
    return f"written to {path.relative_to(settings.data_dir)}"


#: Three attempts: wait 1 minute, then 5, then the message is marked "failed".
RETRY_BACKOFF_MINUTES = (1, 5)
#: A message left in "sending" this long (process crashed mid-send) is retried.
STALE_SENDING_MINUTES = 10


def claim(storage: Storage, notification_id: int) -> bool:
    """Atomically take one pending message for sending (NTF-01).

    The worker thread and a manual "send now" may look at the same row; only the
    caller whose UPDATE actually flips ``pending → sending`` sends it.
    """
    cursor = storage.execute(
        "UPDATE notifications SET status='sending', claimed_at=? WHERE id=? AND status='pending'",
        (utcnow_iso(), notification_id),
    )
    return (cursor.rowcount or 0) == 1


def dispatch(storage: Storage, settings: Settings, *, limit: int = 25) -> list[dict[str, Any]]:
    """Send up to ``limit`` due messages. Safe to call from anywhere, concurrently."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stale = (now - timedelta(minutes=STALE_SENDING_MINUTES)).isoformat()
    storage.execute(
        "UPDATE notifications SET status='pending' WHERE status='sending' AND claimed_at < ?",
        (stale.replace("+00:00", "Z"),),
    )
    rows = storage.query(
        "SELECT * FROM notifications WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY id LIMIT ?",
        (now.isoformat(), limit),
    )
    results = []
    for row in rows:
        if not claim(storage, row["id"]):
            continue  # someone else is sending it
        results.append({**row, **send_one(storage, settings, row)})
    return results


def outbox_stats(storage: Storage) -> dict[str, int]:
    rows = storage.query("SELECT status, COUNT(*) AS n FROM notifications GROUP BY status")
    stats = {row["status"]: int(row["n"]) for row in rows}
    for key in ("pending", "sent", "failed", "skipped"):
        stats.setdefault(key, 0)
    return stats


def recent(storage: Storage, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    if status and status != "all":
        return storage.query(
            "SELECT * FROM notifications WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
        )
    return storage.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))


def retry_failed(storage: Storage) -> int:
    cursor = storage.execute(
        "UPDATE notifications SET status='pending', attempts=0, error=NULL, next_attempt_at=NULL WHERE status='failed'"
    )
    return cursor.rowcount or 0


# ---------------------------------------------------------------------- worker
class NotificationWorker:
    """Drains the outbox on a timer, in a daemon thread.

    Deliberately simple: one thread, one query, no broker. For a 50-person HRMS
    this is the right size, and it keeps the deployment to a single container.
    """

    def __init__(self, storage: Storage, settings: Settings, *, interval: float | None = None) -> None:
        self.storage = storage
        self.settings = settings
        self.interval = float(interval if interval is not None else settings.notify_interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.failed = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="hrms-notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                results = dispatch(self.storage, self.settings, limit=50)
                self.sent += sum(1 for r in results if r.get("ok"))
                self.failed += sum(1 for r in results if not r.get("ok"))
            except Exception:  # pragma: no cover - the worker must never die
                logger.exception("notification worker iteration failed")


def queue_many(
    storage: Storage, items: Iterable[tuple[str, str | None, str, str]], *, settings: Settings
) -> list[int]:
    """Bulk insert ``(channel, recipient, subject, body)`` tuples."""
    ids: list[int] = []
    for channel, recipient, subject, body in items:
        row_id = queue(
            storage, channel=channel, recipient=recipient, subject=subject, body=body,
            category="bulk", settings=settings,
        )
        if row_id:
            ids.append(row_id)
    return ids
