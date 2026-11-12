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

CHANNELS = ("email", "whatsapp", "sms", "push")


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
        subject = f"Welcome to {settings.org_name} — Your Employee Portal Credentials"
        emp_code = escape(ctx.get('code') or '')
        body = (
            f"<p>Hi <b>{escape(ctx['name'])}</b>,</p>"
            f"<p>Welcome to <b>{org}</b>! Your official employee self-service portal account is now active.</p>"
            f"<p>You can use your portal to track daily attendance, punch check-ins, apply for leave, and view or download your monthly payslips.</p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin:18px 0;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 18px;width:100%;max-width:520px'>"
            f"<tr><td colspan='2' style='padding-bottom:10px;font-size:12px;font-weight:700;color:{BRAND['muted']};text-transform:uppercase;letter-spacing:0.5px'>Login Credentials</td></tr>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']};width:130px'>Portal URL</td>"
            f"<td style='padding:5px 12px;font-family:monospace'><a href='{base}/login' style='color:{BRAND['primary']};font-weight:bold;text-decoration:none'>{base}/login</a></td></tr>"
            + (
                f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Employee Code</td>"
                f"<td style='padding:5px 12px;font-family:monospace'><b>{emp_code}</b></td></tr>"
                if emp_code else ""
            )
            + f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Username</td>"
            f"<td style='padding:5px 12px;font-family:monospace'><b>{escape(ctx['username'])}</b></td></tr>"
            + (
                f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Login Email</td>"
                f"<td style='padding:5px 12px;font-family:monospace'><b>{escape(ctx['email'])}</b></td></tr>"
                if ctx.get("email") else ""
            )
            + (
                f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Login Mobile</td>"
                f"<td style='padding:5px 12px;font-family:monospace'><b>{escape(ctx['phone'])}</b></td></tr>"
                if ctx.get("phone") else ""
            )
            + (
                f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Password</td>"
                f"<td style='padding:5px 12px;font-family:monospace;font-size:15px;color:{BRAND['primary']}'><b>{escape(ctx['password'])}</b></td></tr>"
                if ctx.get("password") else ""
            )
            + "</table>"
            + (
                f"<p><a href='{base}{escape(ctx['link'])}' style='display:inline-block;background:{BRAND['primary']};"
                f"color:#fff;padding:12px 22px;border-radius:8px;font-weight:600;text-decoration:none'>Set my password</a></p>"
                f"<p style='color:{BRAND['muted']};font-size:12px'>The link works once and expires in "
                f"{escape(str(ctx.get('hours', 72)))} hours.</p>"
                if ctx.get("link") else
                f"<p><a href='{base}/login' style='display:inline-block;background:{BRAND['primary']};color:#fff;"
                f"padding:12px 24px;border-radius:8px;font-weight:600;text-decoration:none'>Open Employee Portal →</a></p>"
                f"<p style='color:{BRAND['muted']};font-size:12px;line-height:1.5'>You can sign in using your <b>Email</b>, <b>Mobile Number</b>, or <b>Username</b> with the password above.<br>"
                f"For security, please change your password after logging in from your portal profile.</p>"
            )
        )
        footer = "If you have questions, reply directly to this email or contact HR at hr@fakhriitservices.com."

    elif template == "leave_requested":
        subject = f"Leave Request: {ctx['name']} ({ctx['code']}) — {ctx['days']} day(s) {ctx['leave_type']}"
        reason_text = ctx.get('reason') or 'No details provided'
        reason_html = escape(reason_text).replace('\n', '<br>')
        attachments_list = ctx.get('attachments') or ctx.get('file_attachments') or []
        att_html = ""
        if attachments_list:
            items = []
            for a in attachments_list:
                if isinstance(a, tuple):
                    fname = escape(a[0])
                elif isinstance(a, dict):
                    fname = escape(a.get("original_name") or a.get("filename") or "Document")
                else:
                    fname = escape(str(a))
                items.append(f"<li style='margin-bottom:4px'>📎 <b>{fname}</b></li>")
            att_html = (
                f"<div style='margin-top:16px;padding:12px 14px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px'>"
                f"<div style='font-size:11px;font-weight:700;color:{BRAND['muted']};text-transform:uppercase;margin-bottom:6px;letter-spacing:0.5px'>Attached Supporting Documents ({len(attachments_list)})</div>"
                f"<ul style='margin:0;padding-left:18px;font-size:13px;color:{BRAND['ink']}'>{''.join(items)}</ul>"
                f"</div>"
            )

        manager_str = f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Reporting Manager</td><td style='padding:5px 12px;font-weight:600'>{escape(ctx.get('manager_name') or 'None')}</td></tr>" if ctx.get('manager_name') else ""
        dept_str = f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Department</td><td style='padding:5px 12px;font-weight:600'>{escape(ctx.get('department') or '—')}</td></tr>" if ctx.get('department') else ""

        body = (
            f"<div style='margin-bottom:14px'><span style='display:inline-block;padding:3px 10px;background:#F1F5F9;border-radius:12px;font-size:12px;font-weight:600;color:{BRAND['primary']}'>New Leave Application</span></div>"
            f"<p style='margin:0 0 12px;font-size:15px'><b>{escape(ctx['name'])}</b> has applied for leave.</p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='width:100%;margin-bottom:16px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;padding:12px 16px;font-size:13px'>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']};width:140px'>Employee</td><td style='padding:5px 12px'><b>{escape(ctx['name'])}</b> ({escape(ctx['code'])})</td></tr>"
            f"{dept_str}"
            f"{manager_str}"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Leave Type</td><td style='padding:5px 12px'><b style='color:{BRAND['primary']}'>{escape(ctx['leave_type'])}</b></td></tr>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Dates Requested</td><td style='padding:5px 12px'><b>{escape(ctx['from_date'])}</b> → <b>{escape(ctx['to_date'])}</b></td></tr>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Total Days</td><td style='padding:5px 12px'><b>{escape(str(ctx['days']))} day(s)</b></td></tr>"
            f"</table>"
            f"<div style='margin-bottom:16px;padding:14px;background:#ffffff;border-left:4px solid {BRAND['primary']};border-top:1px solid #E2E8F0;border-right:1px solid #E2E8F0;border-bottom:1px solid #E2E8F0;border-radius:0 8px 8px 0'>"
            f"<div style='font-size:11px;font-weight:700;color:{BRAND['muted']};text-transform:uppercase;margin-bottom:6px;letter-spacing:0.5px'>Reason / Application Content</div>"
            f"<div style='font-size:13px;color:{BRAND['ink']};line-height:1.6'>{reason_html}</div>"
            f"</div>"
            f"{att_html}"
            f"<div style='margin-top:20px'>"
            f"<a href='{base}/leave' style='display:inline-block;background:{BRAND['primary']};color:#ffffff;padding:11px 22px;border-radius:8px;font-weight:600;text-decoration:none;font-size:14px'>Open HRMS to Review &amp; Decide →</a>"
            f"</div>"
        )
        footer = "HR receives this request with Reporting Manager in CC. Supporting documents are attached if uploaded."

    elif template == "leave_applied_confirmation":
        subject = f"Leave Request Submitted ({ctx['days']} day(s) {ctx['leave_type']})"
        body = (
            f"<div style='margin-bottom:14px'><span style='display:inline-block;padding:3px 10px;background:#F0FDF4;border:1px solid #BBF7D0;border-radius:12px;font-size:12px;font-weight:600;color:{BRAND['success']}'>Request Submitted</span></div>"
            f"<p>Hi <b>{escape(ctx['name'])}</b>,</p>"
            f"<p>Your leave request has been submitted to HR and your reporting manager for review.</p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='width:100%;margin-bottom:16px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;padding:12px 16px;font-size:13px'>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']};width:130px'>Leave Type</td><td style='padding:5px 12px'><b>{escape(ctx['leave_type'])}</b></td></tr>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Dates</td><td style='padding:5px 12px'><b>{escape(ctx['from_date'])}</b> → <b>{escape(ctx['to_date'])}</b></td></tr>"
            f"<tr><td style='padding:5px 0;color:{BRAND['muted']}'>Days</td><td style='padding:5px 12px'><b>{escape(str(ctx['days']))} day(s)</b></td></tr>"
            f"</table>"
            f"<p><a href='{base}/me/leave' style='display:inline-block;background:{BRAND['primary']};color:#fff;padding:10px 18px;border-radius:8px;font-weight:600;text-decoration:none'>View My Requests →</a></p>"
        )
        footer = "You will receive an email once your leave is reviewed and decided."

    elif template == "leave_decided":
        decision = ctx["decision"]
        is_approved = decision == "approved"
        action_word = "Approved" if is_approved else "Declined"
        ltype = ctx.get('leave_type') or 'leave'
        subject = f"Leave Request {action_word}: {ctx['name']} ({ctx['days']} day(s) {ltype})"
        colour = BRAND["success"] if is_approved else BRAND["danger"]
        bg_col = "#F0FDF4" if is_approved else "#FEF2F2"
        border_col = "#BBF7D0" if is_approved else "#FECACA"
        badge_text = "✓ Leave Approved" if is_approved else "✕ Leave Request Declined"

        note_section = ""
        if ctx.get("note"):
            note_section = (
                f"<div style='margin:16px 0;padding:12px 16px;background:{bg_col};border:1px solid {border_col};border-radius:8px'>"
                f"<div style='font-size:11px;font-weight:700;color:{colour};text-transform:uppercase;margin-bottom:4px'>{'Approval Note' if is_approved else 'Reason for Declining'}</div>"
                f"<div style='font-size:13px;color:{BRAND['ink']};line-height:1.5'><b>{escape(ctx['note'])}</b></div>"
                f"</div>"
            )

        body = (
            f"<div style='margin-bottom:14px'><span style='display:inline-block;padding:4px 12px;background:{bg_col};border:1px solid {border_col};border-radius:12px;font-size:12px;font-weight:700;color:{colour}'>{badge_text}</span></div>"
            f"<p style='margin:0 0 12px;font-size:15px'>Hi <b>{escape(ctx['name'])}</b>,</p>"
            f"<p style='margin:0 0 14px'>Your request for <b>{escape(ltype)}</b> ({escape(ctx['from_date'])} → {escape(ctx['to_date'])}, {escape(str(ctx['days']))} day(s)) has been <b style='color:{colour}'>{escape(decision)}</b> by {escape(ctx.get('decided_by') or 'HR/Management')}.</p>"
            f"{note_section}"
            f"<p style='margin-top:16px'><a href='{base}/me/leave' style='display:inline-block;background:{BRAND['primary']};color:#fff;padding:10px 20px;border-radius:8px;font-weight:600;text-decoration:none'>View My Leave Status →</a></p>"
        )
        footer = "Reporting Manager and HR are copied on this notification."

    elif template == "leave_cancelled":
        ltype = ctx.get('leave_type') or 'leave'
        emp_code = f" ({escape(ctx['code'])})" if ctx.get('code') else ""
        subject = f"Leave Request Withdrawn: {ctx['name']}{emp_code} — {ctx['days']} day(s) {ltype}"
        body = (
            f"<div style='margin-bottom:14px'><span style='display:inline-block;padding:3px 10px;background:#F1F5F9;border-radius:12px;font-size:12px;font-weight:600;color:{BRAND['muted']}'>Leave Request Cancelled</span></div>"
            f"<p><b>{escape(ctx['name'])}</b>{emp_code} has withdrawn their leave request for <b>{escape(ltype)}</b> ({escape(ctx['from_date'])} → {escape(ctx['to_date'])}, {escape(str(ctx['days']))} day(s)).</p>"
            f"<p style='color:{BRAND['muted']};font-size:13px'>No action is required.</p>"
            f"<p><a href='{base}/leave' style='display:inline-block;background:{BRAND['primary']};color:#fff;padding:10px 18px;border-radius:8px;font-weight:600;text-decoration:none'>Open Leave Console</a></p>"
        )
        footer = "HR and Reporting Manager have been notified of this cancellation."

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

    elif template in ("birthday", "anniversary", "work_anniversary"):
        subject = {
            "birthday": f"Happy Birthday, {ctx['name']}! 🎂",
            "anniversary": f"Happy Anniversary, {ctx['name']}! 💐",
            "work_anniversary": f"{ctx['title']} 🎉",
        }[template]
        lead = ctx.get("opener") or (
            f"Today marks {ctx['years']} year{'' if ctx.get('years') == 1 else 's'} since you joined {settings.org_name}."
            if template == "work_anniversary" else "Today is a day worth celebrating."
        )
        closing = ctx.get("closer") or "Thank you for being such a valued part of our team."
        body = (
            f"<div style='text-align:center;padding:6px 0 14px'>"
            f"<div style='font-size:44px;line-height:1'>{ {'birthday': '🎂', 'anniversary': '💐'}.get(template, '🎉') }</div>"
            f"<h1 style='margin:12px 0 0;font-size:24px;line-height:1.25;color:{BRAND['primary']}'>{escape(ctx['title'])}</h1>"
            f"</div>"
            f"<p style='font-size:15px'>Dear <b>{escape(ctx['name'])}</b>,</p>"
            f"<p style='font-size:15px'>{escape(lead)}</p>"
            f"<blockquote style='margin:18px 0;padding:14px 18px;background:#fbf3f3;border-left:4px solid {BRAND['primary']};"
            f"border-radius:6px;font-size:16px;line-height:1.5;color:{BRAND['ink']}'>{escape(ctx['wish'])}</blockquote>"
            f"<p style='font-size:15px'>{escape(closing)}</p>"
            f"<p style='font-size:15px;margin-bottom:0'>With warm wishes,<br><b>Team HR</b><br>{org}</p>"
        )
        ctx.setdefault("footer", f"Sent with best wishes by the HR team at {org}.")

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
def hr_emails(storage: Storage, settings: Settings | None = None) -> list[str]:
    """Every active HR login with an email address (set under Settings → Logins)."""
    emails = [
        row["email"] for row in storage.query(
            "SELECT DISTINCT email FROM users WHERE role = 'hr' AND active = 1 AND email IS NOT NULL AND email != ''"
        )
    ]
    if not emails and settings and settings.smtp_from:
        clean = _clean_email_address(settings.smtp_from)
        if clean:
            emails = [clean]
    if not emails:
        emails = ["hr@fakhriitservices.com"]
    return emails


def notify_hr(
    storage: Storage,
    settings: Settings,
    template: str,
    *,
    category: str,
    cc: list[str] | str | None = None,
    reply_to: str | None = None,
    file_attachments: list[dict[str, Any]] | None = None,
    **ctx: Any,
) -> int:
    """Queue one message per HR address. Returns how many were queued."""
    queued = 0
    for address in hr_emails(storage, settings):
        if queue_email(
            storage, address, template, settings=settings, category=category,
            cc=cc, reply_to=reply_to, file_attachments=file_attachments, **ctx
        ):
            queued += 1
    subject, _html, _text = render(template, settings, **ctx)
    if template == "leave_requested":
        message, link = f"{ctx['name']}: {ctx['days']} day(s), {ctx['from_date']} to {ctx['to_date']}.", "/leave"
    elif template == "profile_changed":
        message, link = f"{ctx['name']}: {ctx['fields']}.", "/profile-requests"
    else:
        message, link = ctx.get("message") or "", ctx.get("link") or "/dashboard"
    push_hr(storage, settings, subject, message, link, category=category)
    return queued


# ------------------------------------------------------------- bell and web push
def push_user(storage: Storage, settings: Settings, user_id: int, title: str, body: str, url: str = "/",
              *, category: str = "system") -> int | None:
    """One notification for one login: it appears under the bell and goes to their devices.

    Never put passwords, codes or salary figures here: this text shows on lock screens.
    """
    if not settings.notify_push:
        return None
    return queue(storage, channel="push", recipient=f"user:{int(user_id)}", subject=title[:160],
                 body=(body or "")[:500], category=category, meta={"url": url}, settings=settings)


def push_employee(storage: Storage, settings: Settings, employee_id: int, title: str, body: str, url: str = "/me",
                  *, category: str = "system") -> int | None:
    """Notify an employee, if they have an active portal login."""
    row = storage.query_one("SELECT id FROM users WHERE employee_id = ? AND active = 1", (employee_id,))
    return push_user(storage, settings, row["id"], title, body, url, category=category) if row else None


def push_hr(storage: Storage, settings: Settings, title: str, body: str, url: str = "/dashboard",
            *, category: str = "system") -> int:
    """Notify every active HR login."""
    rows = storage.query("SELECT id FROM users WHERE role = 'hr' AND active = 1")
    return sum(1 for row in rows if push_user(storage, settings, row["id"], title, body, url, category=category))


def push_punch(storage: Storage, settings: Settings, employee: dict[str, Any], outcome: Any) -> None:
    """Confirm a completed check-in or check-out to the employee."""
    if outcome.action == "check_in":
        title = f"Checked in at {(outcome.check_in or '')[:5]}"
        body = f"You are {outcome.late_minutes} minutes late." if outcome.late_minutes else "Have a good day at work."
    elif outcome.action == "check_out":
        title = f"Checked out at {(outcome.check_out or '')[:5]}"
        body = "Attendance for today is complete."
        if outcome.status == "half_day":
            body += " It is marked as a half day."
    else:
        return
    push_employee(storage, settings, employee["id"], title, body, "/me", category=f"attendance_{outcome.action}")


INBOX_SIZE = 30


def inbox_rows(storage: Storage, user_id: int) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT id, subject, body, meta, created_at FROM notifications "
        "WHERE channel = 'push' AND recipient = ? ORDER BY id DESC LIMIT ?",
        (f"user:{int(user_id)}", INBOX_SIZE),
    )


def inbox_seen(storage: Storage, user_id: int) -> int:
    try:
        return int(storage.get_setting(f"inbox_seen:{int(user_id)}", "0") or 0)
    except ValueError:
        return 0


def mark_inbox_seen(storage: Storage, user_id: int, last_id: int) -> None:
    """Record what this user has read. It only moves forward."""
    if last_id > inbox_seen(storage, user_id):
        storage.set_setting(f"inbox_seen:{int(user_id)}", str(int(last_id)))


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
    cc: list[str] | str | None = None,
    reply_to: str | None = None,
    file_attachments: list[dict[str, Any]] | None = None,
    **ctx: Any,
) -> int | None:
    if file_attachments and "file_attachments" not in ctx:
        ctx["file_attachments"] = file_attachments
    if attachments and "attachments" not in ctx:
        ctx["attachments"] = attachments
    subject, html, _text = render(template, settings, **ctx)
    meta: dict[str, Any] = {"template": template}
    if cc:
        meta["cc"] = cc
    if reply_to:
        meta["reply_to"] = reply_to
    if file_attachments:
        meta["file_attachments"] = file_attachments
    if attachments:
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
    if template == "leave_applied_confirmation":
        return (
            f"*{settings.org_name}* — Your leave request for {ctx['days']} day(s) "
            f"({ctx['from_date']} → {ctx['to_date']}) has been submitted to HR."
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
        name = ctx.get("name") or "there"
        return f"*{settings.org_name}* — Hi {name}, your payslip for {ctx['period']} is generated. Downloadable PDF document is attached below."
    if template == "otp":
        return f"*{settings.org_name}* — password reset code: {ctx['code']} (valid {ctx['minutes']} min)."
    if template == "welcome":
        base = settings.public_base_url.rstrip("/")
        parts = [f"*{settings.org_name}* — Welcome {ctx['name']}! Your employee portal login is ready.\n"]
        parts.append(f"Portal: {base}/login")
        if ctx.get("code"):
            parts.append(f"Employee Code: {ctx['code']}")
        parts.append(f"Username: {ctx['username']}")
        if ctx.get("email"):
            parts.append(f"Email: {ctx['email']}")
        if ctx.get("phone"):
            parts.append(f"Mobile: {ctx['phone']}")
        if ctx.get("link"):
            parts.append(f"Set your password: {base}{ctx['link']}")
            parts.append(f"\nThe link works once and expires in {ctx.get('hours', 72)} hours.")
        else:
            parts.append("\nAsk HR for your password. Please change it after you sign in.")
        return "\n".join(parts)
    return _sanitise(ctx.get("message") or "Notification from HRMS")


# --------------------------------------------------------------------- sending
class SendError(RuntimeError):
    pass


def _attachments_for(storage: Storage, settings: Settings, row: dict[str, Any]) -> list[tuple[str, bytes, str, str]]:
    """Regenerate any attachments this message refers to.

    Payslip emails carry the PDF as a real attachment rather than a link, because
    an attachment survives forwarding and archiving — which is what an employee
    actually needs when a landlord or a bank asks for proof of salary.
    """
    try:
        meta = json.loads(row.get("meta") or "{}")
    except json.JSONDecodeError:
        return []

    # File attachments saved on disk (e.g. leave application supporting documents)
    disk_attachments = meta.get("file_attachments") or []
    results: list[tuple[str, bytes, str, str]] = []
    if disk_attachments:
        import mimetypes
        from .uploads import path_for
        for item in disk_attachments:
            try:
                kind = item.get("kind", "leave")
                stored = item.get("stored_name")
                filename = item.get("filename") or stored
                p = path_for(settings, kind, stored)
                if p.exists():
                    data = p.read_bytes()
                    mime = item.get("mime") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    maintype, subtype = mime.split("/", 1) if "/" in mime else ("application", "octet-stream")
                    results.append((filename, data, maintype, subtype))
            except Exception as exc:
                logger.warning("Failed to load attachment %s: %s", item, exc)
        if results:
            return results

    exit_id = meta.get("exit_id")
    if exit_id:
        row_exit = storage.query_one("SELECT * FROM employee_exits WHERE id = ?", (int(exit_id),))
        if not row_exit or not row_exit.get("fnf_snapshot"):
            return []
        from .pdf import render_fnf

        fnf = json.loads(row_exit["fnf_snapshot"])
        code = (fnf.get("employee") or {}).get("code") or exit_id
        data = render_fnf(fnf, settings=settings, org_name=storage.get_setting("org_name", settings.org_name),
                          ref=f"FNF-{exit_id}")
        return [(f"full-and-final-{code}.pdf", data, "application", "pdf")]

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
    return [(filename_for(slip), data, "application", "pdf")]


def _clean_email_address(raw: str | None) -> str:
    """Extract a pure email address from strings like 'Name <email@dom.com>' or '\"email@dom.com\"'."""
    if not raw:
        return ""
    import email.utils
    clean_raw = raw.strip().strip('"').strip("'")
    _, addr = email.utils.parseaddr(clean_raw)
    return (addr or clean_raw).strip().strip('"').strip("'")


def _send_email(storage: Storage, settings: Settings, row: dict[str, Any]) -> str:
    """Deliver one email. Returns a short driver receipt for the log."""
    to = row["recipient"]
    attachments = _attachments_for(storage, settings, row)
    reply_to_addr = "hr@fakhriitservices.com"
    org_display = settings.org_name or "Fakhri IT Services"

    meta: dict[str, Any] = {}
    try:
        meta = json.loads(row.get("meta") or "{}")
    except Exception:
        pass

    cc_val = meta.get("cc")
    reply_to_val = _clean_email_address(meta.get("reply_to")) or reply_to_addr

    if not settings.smtp_host:
        # console driver: keep a real .eml on disk so it can be inspected
        outbox = Path(settings.data_dir) / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", to)[:40]
        from_addr = _clean_email_address(settings.smtp_from) or f"hrms@{org_display.lower().replace(' ', '')}.local"
        message = EmailMessage()
        message["From"] = formataddr((org_display, from_addr))
        message["To"] = to
        if cc_val:
            if isinstance(cc_val, list):
                valid_ccs = [_clean_email_address(c) for c in cc_val if _clean_email_address(c)]
                if valid_ccs:
                    message["Cc"] = ", ".join(valid_ccs)
            elif isinstance(cc_val, str) and _clean_email_address(cc_val):
                message["Cc"] = _clean_email_address(cc_val)
        message["Reply-To"] = formataddr((f"{org_display} HR", reply_to_val))
        if to.strip().lower() != reply_to_addr.lower():
            message["Bcc"] = reply_to_addr
        message["Subject"] = row["subject"] or "(no subject)"
        message["Message-ID"] = make_msgid(domain="hrms.local")
        message.set_content(_plain(row["body"]))
        message.add_alternative(row["body"], subtype="html")
        for att in attachments:
            fname, payload, maintype, subtype = att
            message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=fname)
        path = outbox / f"{stamp}-{safe}-{row['id']}.eml"
        path.write_bytes(bytes(message))
        return f"written to {path.relative_to(settings.data_dir)}"

    from_addr = _clean_email_address(settings.smtp_from or settings.smtp_user)
    message = EmailMessage()
    message["From"] = formataddr((org_display, from_addr))
    message["To"] = to
    if cc_val:
        if isinstance(cc_val, list):
            valid_ccs = [_clean_email_address(c) for c in cc_val if _clean_email_address(c)]
            if valid_ccs:
                message["Cc"] = ", ".join(valid_ccs)
        elif isinstance(cc_val, str) and _clean_email_address(cc_val):
            message["Cc"] = _clean_email_address(cc_val)
    message["Reply-To"] = formataddr((f"{org_display} HR", reply_to_val))
    if to.strip().lower() != reply_to_addr.lower():
        message["Bcc"] = reply_to_addr
    message["Subject"] = row["subject"] or "(no subject)"
    message["Message-ID"] = make_msgid()
    message.set_content(_plain(row["body"]))
    message.add_alternative(row["body"], subtype="html")
    for att in attachments:
        fname, payload, maintype, subtype = att
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=fname)

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


def format_whatsapp_chat_id(recipient: str | None) -> str:
    """Format an Indian or international mobile number to WAHA chat ID format (e.g. 919876543210@c.us)."""
    raw = (recipient or "").strip()
    if "@c.us" in raw or "@g.us" in raw:
        return raw
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        raise ValueError(f"Invalid phone number: {recipient}")
    # 10 digits Indian mobile number (e.g. 9876543210)
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    # 11 digits starting with 0 (e.g. 09876543210)
    elif len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        digits = "91" + digits[1:]
    return f"{digits}@c.us"


def _waha_headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if getattr(settings, "waha_api_key", None):
        headers["X-Api-Key"] = settings.waha_api_key
    return headers


def _send_waha_whatsapp(storage: Storage, settings: Settings, row: dict[str, Any]) -> str:
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    chat_id = format_whatsapp_chat_id(row["recipient"])
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")

    # Check if this notification has file attachments (like a payslip PDF)
    attachments = _attachments_for(storage, settings, row)
    pdf_attachment = next((att for att in attachments if att[2] == "application" and att[3] == "pdf"), None)

    if pdf_attachment:
        import base64
        filename, data_bytes, maintype, subtype = pdf_attachment
        b64 = base64.b64encode(data_bytes).decode("ascii")
        data_uri = f"data:application/pdf;base64,{b64}"
        caption = row.get("body") or f"*{settings.org_name}* — Your {filename}"

        payload = {
            "session": session_name,
            "chatId": chat_id,
            "file": {
                "mimetype": "application/pdf",
                "filename": filename,
                "url": data_uri,
            },
            "caption": caption,
        }
        try:
            res = httpx.post(f"{base_url}/api/sendFile", json=payload, headers=headers, timeout=30.0)
            if res.status_code >= 400:
                raise SendError(f"waha sendFile error {res.status_code}: {res.text[:200]}")
            return f"waha doc sent ({filename})"
        except httpx.HTTPError as exc:
            raise SendError(f"waha sendFile connection error: {exc}") from exc
    else:
        payload = {
            "session": session_name,
            "chatId": chat_id,
            "text": row["body"],
        }
        try:
            res = httpx.post(f"{base_url}/api/sendText", json=payload, headers=headers, timeout=20.0)
            if res.status_code >= 400:
                raise SendError(f"waha sendText error {res.status_code}: {res.text[:200]}")
            return "waha message sent"
        except httpx.HTTPError as exc:
            raise SendError(f"waha sendText connection error: {exc}") from exc


def get_waha_session_info(settings: Settings) -> dict[str, Any]:
    """Get status of the WAHA WhatsApp session."""
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")
    try:
        res = httpx.get(f"{base_url}/api/sessions/{session_name}", headers=headers, timeout=8.0)
        if res.status_code == 200:
            data = res.json()
            return {
                "online": True,
                "status": data.get("status"),
                "me": data.get("me"),
                "engine": data.get("engine", {}).get("engine", "NOWEB"),
            }
        elif res.status_code == 404:
            return {"online": True, "status": "NOT_CREATED", "me": None}
        return {"online": False, "status": f"HTTP {res.status_code}", "error": res.text[:100]}
    except Exception as exc:
        return {"online": False, "status": "OFFLINE", "error": str(exc)}


def start_or_restart_waha(settings: Settings) -> dict[str, Any]:
    """Start or restart the WAHA session to generate a fresh QR code."""
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")
    try:
        check = httpx.get(f"{base_url}/api/sessions/{session_name}", headers=headers, timeout=5.0)
        if check.status_code == 404:
            create_res = httpx.post(f"{base_url}/api/sessions", json={"name": session_name}, headers=headers, timeout=10.0)
            create_res.raise_for_status()
        res = httpx.post(f"{base_url}/api/sessions/{session_name}/restart", headers=headers, timeout=10.0)
        if res.status_code != 200:
            res = httpx.post(f"{base_url}/api/sessions/{session_name}/start", headers=headers, timeout=10.0)
        return {"ok": True, "status": "restarted"}
    except Exception as exc:
        logger.warning("Failed to start/restart waha: %s", exc)
        return {"ok": False, "error": str(exc)}


def logout_waha(settings: Settings) -> dict[str, Any]:
    """Logout / unpair WAHA WhatsApp session."""
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")
    try:
        httpx.post(f"{base_url}/api/sessions/{session_name}/logout", headers=headers, timeout=10.0)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_waha_qr_image(settings: Settings) -> bytes | None:
    """Fetch QR code PNG from WAHA if session is in SCAN_QR_CODE state."""
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")
    try:
        res = httpx.get(f"{base_url}/api/{session_name}/auth/qr", headers=headers, timeout=8.0)
        if res.status_code == 200 and "image" in res.headers.get("content-type", ""):
            return res.content
        return None
    except Exception as exc:
        logger.warning("Failed to fetch waha QR: %s", exc)
        return None


def request_waha_pairing_code(settings: Settings, phone_number: str) -> dict[str, Any]:
    """Request an 8-character pairing code for 'Link with phone number instead'."""
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")
    digits = re.sub(r"\D+", "", phone_number or "")
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]

    if not digits or len(digits) < 10:
        return {"ok": False, "error": "Please enter a valid 10-digit mobile number."}

    # Session must be in SCAN_QR_CODE. If not, restart it first.
    info = get_waha_session_info(settings)
    if info.get("status") not in ("SCAN_QR_CODE", "STARTING"):
        start_or_restart_waha(settings)
        time.sleep(2.5)

    payload = {"phoneNumber": digits}
    try:
        res = httpx.post(f"{base_url}/api/{session_name}/auth/request-code", json=payload, headers=headers, timeout=20.0)
        if res.status_code in (200, 201):
            data = res.json()
            return {"ok": True, "code": data.get("code"), "phone": digits}
        # If it failed due to stale credentials or timeout, reset session and retry once
        logout_waha(settings)
        time.sleep(1.0)
        start_or_restart_waha(settings)
        time.sleep(3.0)
        res2 = httpx.post(f"{base_url}/api/{session_name}/auth/request-code", json=payload, headers=headers, timeout=20.0)
        if res2.status_code in (200, 201):
            data = res2.json()
            return {"ok": True, "code": data.get("code"), "phone": digits}
        return {"ok": False, "error": res2.text, "status_code": res2.status_code}
    except Exception as exc:
        logger.warning("Failed to request waha pairing code: %s", exc)
        return {"ok": False, "error": str(exc)}


def send_test_whatsapp(settings: Settings, to_phone: str, message: str = "") -> dict[str, Any]:
    """Send an immediate test WhatsApp message to verify the connection."""
    base_url = getattr(settings, "waha_api_url", "http://waha:3000").rstrip("/")
    headers = _waha_headers(settings)
    session_name = getattr(settings, "waha_session", "default")
    chat_id = format_whatsapp_chat_id(to_phone)
    msg_text = message.strip() or f"*{settings.org_name}* — Test Notification! 🚀\nYour HRMS WhatsApp Linked Device is successfully connected."
    payload = {
        "session": session_name,
        "chatId": chat_id,
        "text": msg_text,
    }
    try:
        res = httpx.post(f"{base_url}/api/sendText", json=payload, headers=headers, timeout=15.0)
        if res.status_code >= 400:
            return {"ok": False, "error": f"WAHA Error ({res.status_code}): {res.text[:200]}"}
        return {"ok": True, "chat_id": chat_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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


def _send_push(storage: Storage, settings: Settings, row: dict[str, Any]) -> str:
    """Deliver to each device of one login. No device is fine: the bell still has it."""
    from . import push

    user_id = int(row["recipient"].split(":", 1)[1])
    user = storage.query_one("SELECT active FROM users WHERE id = ?", (user_id,))
    if not user or not user["active"]:
        return "login is not active"
    devices = push.subscriptions(storage, user_id)
    if not devices:
        return "saved to inbox; no device registered"
    try:
        url = json.loads(row.get("meta") or "{}").get("url") or "/"
    except ValueError:
        url = "/"
    payload = {"id": row["id"], "title": row["subject"] or "Notification", "body": row["body"], "url": url}
    delivered, errors = 0, []
    for device in devices:
        try:
            push.send(settings, device, payload)
            delivered += 1
        except push.PushGone:
            push.unsubscribe(storage, user_id, device["endpoint"])
        except Exception as exc:
            errors.append(str(exc))
    if errors and not delivered:
        raise SendError(f"push: {errors[0]}")
    return f"pushed to {delivered} device(s)"


def send_one(storage: Storage, settings: Settings, notification: dict[str, Any]) -> dict[str, Any]:
    """Attempt one queued message and update its row."""
    channel = notification["channel"]
    attempts = int(notification["attempts"]) + 1
    try:
        if channel == "email":
            receipt = _send_email(storage, settings, notification)
        elif channel == "whatsapp":
            if settings.whatsapp_provider == "waha":
                receipt = _send_waha_whatsapp(storage, settings, notification)
            elif settings.whatsapp_provider == "twilio":
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
        elif channel == "push":
            receipt = _send_push(storage, settings, notification)
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


#: Four attempts: retry after 1, 5 and 30 minutes, then the message is marked "failed".
RETRY_BACKOFF_MINUTES = (1, 5, 30)
#: A message left in "sending" this long (process crashed mid-send) is retried.
STALE_SENDING_MINUTES = 10
#: How often the worker looks for scheduled alerts that have come due.
ALERT_CHECK_SECONDS = 60


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
        from .services import alerts  # imported here because alerts builds on this module

        next_alerts = 0.0
        while not self._stop.wait(self.interval):
            try:
                if time.monotonic() >= next_alerts:
                    next_alerts = time.monotonic() + ALERT_CHECK_SECONDS
                    alerts.run_due(self.storage, self.settings)
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
