"""HRMS configuration — brand tokens, paths and environment settings.

Brand palette is taken from the Fakhri IT Services website
(https://www.fakhriitservices.com): the primary colour there is #870808, with a
warm off-white surface (#f6f4f4), near-black ink (#141414), muted grey (#737373)
and warm hairlines (#e8e5e3). Headings use Poppins, body text uses Inter — the
same two families the site loads.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------- brand
BRAND = {
    "org_name": "Fakhri IT Services",
    "product_name": "Fakhri HRMS",
    "tagline": "Attendance, people and payroll — one place",
    # Core palette lifted from fakhriitservices.com
    "primary": "#870808",
    "primary_dark": "#6f0606",
    "primary_light": "#a90a0a",
    "primary_soft": "#fdf2f2",
    "primary_soft_border": "#f3d6d6",
    "ink": "#141414",
    "ink_soft": "#262626",
    "muted": "#737373",
    "surface": "#f6f4f4",
    "card": "#ffffff",
    "border": "#e8e5e3",
    "navy": "#0a192f",
    "navy_soft": "#1c3f60",
    # Functional colours (not on their site — chosen to sit beside the maroon)
    "success": "#0f7b52",
    "success_soft": "#e8f5ee",
    "warning": "#b26a00",
    "warning_soft": "#fdf3e3",
    "danger": "#c91d1d",
    "danger_soft": "#fdecec",
    "info": "#1c3f60",
    "info_soft": "#eaf0f6",
    "font_heading": "'Poppins', 'Inter', system-ui, sans-serif",
    "font_body": "'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Runtime configuration. Every field maps to an ``HRMS_*`` env var."""

    app_name: str = "Fakhri HRMS"
    org_name: str = BRAND["org_name"]
    data_dir: Path = field(default_factory=lambda: Path(_env("HRMS_DATA_DIR", str(BASE_DIR / "data"))))
    db_filename: str = "hrms.db"
    timezone: str = field(default_factory=lambda: _env("HRMS_TZ", "Asia/Kolkata"))

    # --- face recognition service (the Face API project) ------------------
    faceapi_url: str = field(default_factory=lambda: _env("HRMS_FACEAPI_URL", "http://127.0.0.1:8000"))
    faceapi_key: str = field(default_factory=lambda: _env("HRMS_FACEAPI_KEY", ""))
    face_collection: str = field(default_factory=lambda: _env("HRMS_FACE_COLLECTION", "employees"))
    face_timeout: float = field(default_factory=lambda: float(_env("HRMS_FACE_TIMEOUT", "20")))
    #: When true the kiosk only shows the scanner if the face service answered.
    face_required_for_kiosk: bool = field(default_factory=lambda: _env_bool("HRMS_FACE_REQUIRED", False))

    # --- sessions ---------------------------------------------------------
    secret_key: str = field(default_factory=lambda: _env("HRMS_SECRET_KEY", ""))
    session_cookie: str = "hrms_session"
    hr_session_hours: int = field(default_factory=lambda: int(_env("HRMS_SESSION_HOURS", "12")))
    kiosk_session_days: int = field(default_factory=lambda: int(_env("HRMS_KIOSK_SESSION_DAYS", "7")))
    secure_cookies: bool = field(default_factory=lambda: _env_bool("HRMS_SECURE_COOKIES", False))

    # --- misc -------------------------------------------------------------
    public_base_url: str = field(default_factory=lambda: _env("HRMS_PUBLIC_BASE_URL", "http://127.0.0.1:8100"))
    log_level: str = field(default_factory=lambda: _env("HRMS_LOG_LEVEL", "INFO"))
    #: Interactive API docs at /api/docs — off unless explicitly enabled (SEC-07).
    api_docs: bool = field(default_factory=lambda: _env_bool("HRMS_API_DOCS", False))
    default_hr_username: str = field(default_factory=lambda: _env("HRMS_DEFAULT_HR_USER", "hr"))
    default_hr_password: str = field(default_factory=lambda: _env("HRMS_DEFAULT_HR_PASSWORD", ""))
    default_kiosk_username: str = field(default_factory=lambda: _env("HRMS_DEFAULT_KIOSK_USER", "gate"))
    default_kiosk_password: str = field(default_factory=lambda: _env("HRMS_DEFAULT_KIOSK_PASSWORD", ""))
    #: Duplicate kiosk scans from the same face inside this window are ignored.
    kiosk_debounce_seconds: int = field(default_factory=lambda: int(_env("HRMS_KIOSK_DEBOUNCE", "45")))

    # --- notifications ---------------------------------------------------
    #: Email transport. Leave ``smtp_host`` empty to use the console driver,
    #: which writes real .eml files into data/outbox/ (demos and tests).
    smtp_host: str = field(default_factory=lambda: _env("HRMS_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(_env("HRMS_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("HRMS_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("HRMS_SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: _env("HRMS_SMTP_FROM", ""))
    smtp_tls: str = field(default_factory=lambda: _env("HRMS_SMTP_TLS", "starttls"))  # starttls|ssl|none
    smtp_timeout: float = field(default_factory=lambda: float(_env("HRMS_SMTP_TIMEOUT", "20")))

    #: WhatsApp / SMS provider: console | twilio | meta (meta = WhatsApp only)
    whatsapp_provider: str = field(default_factory=lambda: _env("HRMS_WHATSAPP_PROVIDER", "console"))
    sms_provider: str = field(default_factory=lambda: _env("HRMS_SMS_PROVIDER", "console"))
    twilio_account_sid: str = field(default_factory=lambda: _env("HRMS_TWILIO_ACCOUNT_SID", ""))
    twilio_auth_token: str = field(default_factory=lambda: _env("HRMS_TWILIO_AUTH_TOKEN", ""))
    twilio_from: str = field(default_factory=lambda: _env("HRMS_TWILIO_FROM", ""))
    meta_phone_id: str = field(default_factory=lambda: _env("HRMS_META_PHONE_ID", ""))
    meta_token: str = field(default_factory=lambda: _env("HRMS_META_TOKEN", ""))

    #: Master switch for outbound email (the queue still records messages)
    notify_email_enabled: bool = field(default_factory=lambda: _env_bool("HRMS_NOTIFY_EMAIL", True))

    #: Automatic triggers
    notify_punch_in: bool = field(default_factory=lambda: _env_bool("HRMS_NOTIFY_PUNCH_IN", True))
    notify_late: bool = field(default_factory=lambda: _env_bool("HRMS_NOTIFY_LATE", True))
    notify_leave_email: bool = field(default_factory=lambda: _env_bool("HRMS_NOTIFY_LEAVE", True))
    notify_whatsapp_attendance: bool = field(
        default_factory=lambda: _env_bool("HRMS_NOTIFY_WHATSAPP_ATTENDANCE", False)
    )
    #: How often the background thread drains the outbox, in seconds
    notify_interval_seconds: float = field(default_factory=lambda: float(_env("HRMS_NOTIFY_INTERVAL", "20")))

    # --- self-service password reset -------------------------------------
    otp_ttl_minutes: int = field(default_factory=lambda: int(_env("HRMS_OTP_TTL_MINUTES", "10")))
    otp_max_attempts: int = field(default_factory=lambda: int(_env("HRMS_OTP_MAX_ATTEMPTS", "5")))
    #: Minimum seconds between two OTP emails for the same account
    otp_cooldown_seconds: int = field(default_factory=lambda: int(_env("HRMS_OTP_COOLDOWN", "60")))
    #: Show the employee's name on the gate screen. OFF by default: the scanner is
    #: shared, and a queue of colleagues should not see whose attendance was just
    #: marked. Turn it on only if your gate staff need it to verify a match.
    kiosk_show_name: bool = field(default_factory=lambda: _env_bool("HRMS_KIOSK_SHOW_NAME", False))

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - bad env value
            return ZoneInfo("UTC")

    def load_or_create_secret(self) -> str:
        """Signing key for session cookies — generated once, then persisted."""
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / "secret_key.txt"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                self.secret_key = value
                return value
        value = secrets.token_urlsafe(48)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        self.secret_key = value
        return value


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
        _settings.load_or_create_secret()
    return _settings


#: Editable, admin-facing settings stored in the ``app_settings`` table.
#: (key, default value, human label, input type, help text)
SETTING_DEFS: list[tuple[str, str, str, str, str]] = [
    ("org_name", BRAND["org_name"], "Organisation name", "text", "Shown in the header and on payslips."),
    ("shift_start", "09:30", "Shift start", "time", "Scans after start + grace are marked LATE."),
    ("shift_end", "18:30", "Shift end", "time", "Used to compute expected working hours."),
    ("late_grace_minutes", "15", "Late grace (minutes)", "number", "How late someone may be before being marked late."),
    ("half_day_min_minutes", "240", "Half-day threshold (minutes)", "number",
     "Worked less than this on a full day is treated as a half day."),
    ("standard_work_minutes", "480", "Standard work day (minutes)", "number", "Displayed on the dashboard."),
    ("payroll_basis_days", "calendar", "Payroll basis", "select:calendar|26",
     "Divide monthly salary by calendar days in the month, or by a fixed 26."),
    ("payroll_finalise_from_day", "0", "Finalise payroll from day", "number",
     "Earliest day of the month a run may be finalised or paid. 0 = the last day of the month."),
    ("pl_accrual_per_month", "1.5", "Paid leave accrual / month", "number",
     "Leave balance = months of service x this value - paid leave taken."),
    ("week_off_days", "6", "Weekly off days", "weekdays",
     "Tick every day that is always off — e.g. Saturday + Sunday for a 5-day week. Changes apply to draft "
     "and future months; finalised payroll months keep their figures."),
    ("saturday_off_weeks", "", "Alternate Saturdays off", "text",
     "Only if Saturday is a working day: which Saturdays of the month are off, e.g. 2,4. Leave empty for none."),
    ("late_marks_per_half_lop", "3", "Late marks per half-day LOP", "number",
     "0 disables the rule; otherwise every N late marks deduct half a day."),
    ("currency_symbol", "₹", "Currency symbol", "text", "Used on payroll screens and payslips."),
    ("annual_leave_types", "paid,sick,casual,unpaid", "Leave types", "text",
     "Comma-separated leave types offered when applying leave."),
    ("company_notice_footer", "", "Noticeboard footer", "text",
     "Optional line shown under every announcement, e.g. a helpdesk number."),
    ("notice_period_days", "30", "Notice period (days)", "number",
     "Default notice for resignations; a shorter notice is recovered in the F&F unless waived."),
    ("geofence_radius_meters", "200", "GPS attendance radius (m)", "number",
     "How close a field employee must be to an allowed location to mark attendance."),
]
