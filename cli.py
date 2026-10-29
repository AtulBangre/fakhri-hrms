#!/usr/bin/env python3
"""Fakhri HRMS command line helper.

    ./.venv/bin/python3 cli.py doctor                 # environment + service check
    ./.venv/bin/python3 cli.py init                   # create the database and logins
    ./.venv/bin/python3 cli.py users list
    ./.venv/bin/python3 cli.py users password hr
    ./.venv/bin/python3 cli.py serve --port 8100      # run the app
    ./.venv/bin/python3 cli.py seed-demo --employees 12
    ./.venv/bin/python3 cli.py stats
    ./.venv/bin/python3 cli.py ess --missing-only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from app import __version__, auth
from app.config import BRAND, Settings, get_settings
from app.db import Storage
from app.face import FaceService
from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import leave as leave_service
from app.services import orgtime, payroll

GREEN, YELLOW, RED, DIM, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def ok(message: str) -> None:
    print(f"{colour('✓', GREEN)} {message}")


def warn(message: str) -> None:
    print(f"{colour('!', YELLOW)} {message}")


def fail(message: str) -> None:
    print(f"{colour('✗', RED)} {message}", file=sys.stderr)


def table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print(colour("  (none)", DIM))
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    print("  ".join(colour(header.ljust(widths[i]), BOLD) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def open_storage(settings: Settings) -> Storage:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return Storage(settings)


# ------------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    print(colour(f"{settings.app_name} v{__version__} — environment check", BOLD))
    print(f"  data dir    : {settings.data_dir}")
    print(f"  database    : {settings.db_path}")
    print(f"  timezone    : {settings.timezone}")
    problems = 0

    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        import jinja2
        import PIL

        ok(f"fastapi / httpx / jinja2 {jinja2.__version__} / pillow {PIL.__version__}")
    except Exception as exc:  # noqa: BLE001
        fail(f"dependency import failed: {exc}")
        problems += 1

    templates_dir = settings.data_dir.parent / "app" / "templates"
    static_dir = settings.data_dir.parent / "app" / "static"
    for label, path in (("templates", templates_dir), ("static assets", static_dir)):
        if path.exists():
            count = len(list(path.rglob("*")))
            ok(f"{label} present ({count} entries under {path.name})")
        else:
            fail(f"{label} missing: {path}")
            problems += 1

    for icon in ("icon-192.png", "icon-512.png", "maskable-512.png"):
        path = static_dir / "img" / icon
        if not path.exists():
            warn(f"missing PWA icon {icon} — run `python cli.py make-icons`")

    try:
        storage = open_storage(settings)
        stats = employees_service.stats(storage)
        ok(f"database reachable ({stats['total']} employees, {stats['active']} active)")
        users = auth.list_users(storage)
        summary = ", ".join(f"{user['username']}:{user['role']}" for user in users) or "none yet"
        print(f"    logins: {len(users)} ({summary})")
        storage.close()
    except Exception as exc:  # noqa: BLE001
        fail(f"database error: {exc}")
        problems += 1

    face = FaceService(settings)
    health = face.health()
    if health.get("ok"):
        ok(f"face service reachable at {settings.faceapi_url} ({health.get('auth_mode')})")
        try:
            collection = face.collection_stats()
            print(f"    collection '{settings.face_collection}': {collection.get('face_count', 0)} templates")
        except Exception as exc:  # noqa: BLE001
            warn(f"could not read the collection: {exc}")
    else:
        warn(f"face service NOT reachable ({health.get('detail')}) — kiosk scanning will be disabled, manual attendance still works")

    print()
    print(f"  HR console  : {settings.public_base_url.rstrip('/')}/dashboard")
    print(f"  Gate kiosk  : {settings.public_base_url.rstrip('/')}/kiosk")
    print(f"  brand       : primary {BRAND['primary']} · surface {BRAND['surface']} · ink {BRAND['ink']}")

    if problems:
        fail(f"{problems} problem(s) found")
        return 1
    ok("looks good")
    return 0


def cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    storage = open_storage(settings)
    created = auth.ensure_default_users(storage, settings)
    stats = employees_service.stats(storage)
    storage.close()

    print(colour(f"{settings.app_name} initialised", BOLD))
    print(f"  database : {settings.db_path}")
    print(f"  employees: {stats['total']}")
    if created:
        print()
        print(colour("  Logins created (change the passwords after first sign-in):", BOLD))
        for role, username, password in created:
            print(f"    {role:5s}  {username:12s}  {colour(password, GREEN)}")
        print(f"\n  Also saved to {settings.data_dir / auth.CREDENTIALS_FILE}")
    else:
        ok("logins already exist — nothing to create")
    print(f"\n  Start the app: ./.venv/bin/python3 cli.py serve")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    storage = open_storage(settings)
    created = auth.ensure_default_users(storage, settings)
    storage.close()

    import socket
    lan_ip = None
    try:
        s_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_sock.connect(("8.8.8.8", 80))
        lan_ip = s_sock.getsockname()[0]
        s_sock.close()
    except Exception:
        pass

    ssl_keyfile = getattr(args, "ssl_keyfile", None)
    ssl_certfile = getattr(args, "ssl_certfile", None)
    if getattr(args, "ssl", False) and not (ssl_keyfile and ssl_certfile):
        cert_dir = Path(settings.data_dir)
        cert_dir.mkdir(parents=True, exist_ok=True)
        key_path = cert_dir / "key.pem"
        cert_path = cert_dir / "cert.pem"
        if not (key_path.exists() and cert_path.exists()):
            import subprocess
            alt = f"IP:{lan_ip},IP:127.0.0.1,DNS:localhost" if lan_ip else "IP:127.0.0.1,DNS:localhost"
            subprocess.run([
                "openssl", "req", "-x509", "-nodes", "-days", "365",
                "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-subj", f"/CN={lan_ip or 'localhost'}",
                "-addext", f"subjectAltName={alt}",
            ], check=True, capture_output=True)
        ssl_keyfile = str(key_path)
        ssl_certfile = str(cert_path)

    proto = "https" if ssl_keyfile and ssl_certfile else "http"
    host_display = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
    print(colour(f"{settings.app_name} v{__version__}", BOLD))
    print(f"  Local (this Mac) : {proto}://{host_display}:{args.port}/dashboard")
    if lan_ip and lan_ip != "127.0.0.1":
        print(f"  Mobile / Laptop  : {proto}://{lan_ip}:{args.port}/dashboard")
        print(f"  Gate Tablet      : {proto}://{lan_ip}:{args.port}/kiosk")
        print(f"  Employee Portal  : {proto}://{lan_ip}:{args.port}/me")
    else:
        print(f"  Gate kiosk       : {proto}://{host_display}:{args.port}/kiosk")
    for role, username, password in created:
        print(colour(f"  NEW {role.upper()} LOGIN: {username} / {password}", GREEN))
    print(colour("  Ctrl+C to stop", DIM))

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        log_level=settings.log_level.lower(),
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )
    return 0


def cmd_users(args: argparse.Namespace, settings: Settings) -> int:
    storage = open_storage(settings)
    try:
        if args.action == "list":
            rows = [
                [
                    user["username"],
                    user["role"],
                    user["display_name"] or "",
                    user["device_label"] or "",
                    "yes" if user["active"] else colour("no", RED),
                    (user["last_login_at"] or "never")[:16],
                ]
                for user in auth.list_users(storage)
            ]
            table(["username", "role", "name", "device", "active", "last login"], rows)
            return 0

        if args.action == "add":
            if auth.get_user_by_username(storage, args.username):
                fail(f"user '{args.username}' already exists")
                return 1
            password = args.password or auth.generate_password()
            auth.create_user(
                storage, username=args.username, password=password, role=args.role,
                display_name=args.name or args.username, device_label=args.device,
            )
            ok(f"created {args.role} login '{args.username}'")
            print(f"    password: {colour(password, GREEN)}")
            return 0

        if args.action == "password":
            user = auth.get_user_by_username(storage, args.username)
            if user is None:
                fail(f"user '{args.username}' not found")
                return 1
            password = args.password or auth.generate_password()
            auth.change_password(storage, user["id"], password)
            ok(f"password changed for '{args.username}'")
            print(f"    new password: {colour(password, GREEN)}")
            return 0

        if args.action == "disable":
            user = auth.get_user_by_username(storage, args.username)
            if user is None:
                fail(f"user '{args.username}' not found")
                return 1
            auth.set_user_active(storage, user["id"], False)
            ok(f"disabled '{args.username}'")
            return 0

        if args.action == "delete":
            user = auth.get_user_by_username(storage, args.username)
            if user is None:
                fail(f"user '{args.username}' not found")
                return 1
            auth.delete_user(storage, user["id"])
            ok(f"deleted '{args.username}'")
            return 0
    finally:
        storage.close()

    fail("unknown users action")
    return 2


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    storage = open_storage(settings)
    try:
        board = attendance_service.dashboard(storage, settings)
        counts = board["counts"]
        stats = employees_service.stats(storage)
        period = orgtime.period_of(orgtime.today(settings))
        run = payroll.get_run(storage, period)

        print(colour(f"{settings.app_name} — {board['day_label']}", BOLD))
        print(
            f"  headcount {counts['total']} · present {counts['present']} · half day {counts['half_day']} · "
            f"leave {counts['leave']} · absent {counts['absent']} · not marked {counts['not_marked']} · "
            f"late {counts['late']}"
        )
        print(f"  attendance {counts['attendance_percent']}% · faces enrolled {stats['face_enrolled']}/{stats['active']}")
        print(f"  monthly gross {stats['monthly_payroll']:,.0f}")
        if run:
            print(
                f"  payroll {period}: {run['status']} · {run['total_net']:,.0f} net across "
                f"{storage.scalar('SELECT COUNT(*) AS n FROM payslips WHERE run_id = ?', (run['id'],))} payslips"
            )
        else:
            print(f"  payroll {period}: not generated yet")

        if args.json:
            print(json.dumps({"counts": counts, "employees": stats, "run": run}, indent=2, default=str))
        return 0
    finally:
        storage.close()


def cmd_ess(args: argparse.Namespace, settings: Settings) -> int:
    """Create or reset employee self-service logins."""
    storage = open_storage(settings)
    try:
        if args.employee:
            employee = employees_service.get_employee_by_code(storage, args.employee.upper())
            if employee is None:
                employee = employees_service.get_employee(storage, int(args.employee)) if args.employee.isdigit() else None
            if employee is None:
                fail(f"employee '{args.employee}' not found")
                return 1
            people = [employee]
        else:
            people = employees_service.list_employees(storage, status="active", limit=1000)
            people = [p for p in people if not args.missing_only or not auth.get_user_for_employee(storage, p["id"])]

        if not people:
            ok("nothing to do")
            return 0

        rows = []
        for employee in people:
            if args.disable:
                disabled = auth.disable_employee_login(storage, employee["id"])
                rows.append([employee["code"], employee["name"], "disabled" if disabled else "no login"])
                continue
            try:
                portal_user, password = auth.ensure_employee_login(
                    storage, employee, password=args.password
                )
            except ValueError as exc:
                rows.append([employee["code"], employee["name"], colour(str(exc), RED)])
                continue
            storage.audit(actor="cli", action="ess_login", entity="employee", entity_id=employee["id"],
                          detail=f"username={portal_user['username']}")
            rows.append([employee["code"], employee["name"], colour(password, GREEN)])

        print(colour("Employee self-service logins", BOLD))
        table(["code", "name", "password (share privately, shown once)"], rows)
        print()
        print("  Portal: " + colour(f"{settings.public_base_url.rstrip('/')}/me", BOLD))
        if not args.disable:
            print(colour("  Passwords are not stored in readable form — save them now.", DIM))
        return 0
    finally:
        storage.close()


def cmd_make_icons(args: argparse.Namespace, settings: Settings) -> int:
    from scripts.make_icons import main as build_icons

    return build_icons()


def cmd_seed_demo(args: argparse.Namespace, settings: Settings) -> int:
    from scripts.seed_demo import seed

    return seed(settings, employees=args.employees, enrol=args.enrol, reset=args.reset)


def cmd_sync_supabase(args: argparse.Namespace, settings: Settings) -> int:
    from app.services.sync_supabase import sync_all

    storage = Storage(settings)
    print("Syncing SQLite database to Supabase Cloud...")
    res = sync_all(storage, settings)
    if not res:
        warn("No tables synced. Check Supabase connection or credentials.")
        return 1
    total = sum(res.values())
    ok(f"Synced {total} rows across {len(res)} tables to Supabase Cloud.")
    return 0


def cmd_production_reset(args: argparse.Namespace, settings: Settings) -> int:
    from scripts.production_reset import reset_production
    reset_production()
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Fakhri HRMS helper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"{get_settings().app_name} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check dependencies, database and the face service")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="create the database and the first logins")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="run the HRMS")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    serve.add_argument("--workers", type=int, default=1)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--ssl", action="store_true", help="run with HTTPS using local SSL certificate")
    serve.add_argument("--ssl-keyfile", default=None, help="SSL private key file")
    serve.add_argument("--ssl-certfile", default=None, help="SSL certificate file")
    serve.set_defaults(func=cmd_serve)

    users = sub.add_parser("users", help="manage logins")
    users_sub = users.add_subparsers(dest="action", required=True)
    users_sub.add_parser("list", help="list logins")
    add = users_sub.add_parser("add", help="create a login")
    add.add_argument("username")
    add.add_argument("--role", choices=["hr", "kiosk"], default="kiosk")
    add.add_argument("--password")
    add.add_argument("--name")
    add.add_argument("--device", help="device label recorded on scans")
    reset = users_sub.add_parser("password", help="set a new password")
    reset.add_argument("username")
    reset.add_argument("--password")
    disable = users_sub.add_parser("disable", help="disable a login")
    disable.add_argument("username")
    remove = users_sub.add_parser("delete", help="delete a login")
    remove.add_argument("username")
    users.set_defaults(func=cmd_users)

    stats = sub.add_parser("stats", help="today's numbers")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    ess = sub.add_parser("ess", help="create or reset employee portal logins")
    ess.add_argument("--employee", help="employee code (FITS001) or id; omit for everyone")
    ess.add_argument("--password", help="use this password instead of a generated one")
    ess.add_argument("--missing-only", action="store_true", help="only employees without a login yet")
    ess.add_argument("--disable", action="store_true", help="disable the portal logins instead")
    ess.set_defaults(func=cmd_ess)

    icons = sub.add_parser("make-icons", help="regenerate the PWA icons")
    icons.set_defaults(func=cmd_make_icons)

    seed = sub.add_parser("seed-demo", help="fill the database with demo people and attendance")
    seed.add_argument("--employees", type=int, default=10)
    seed.add_argument("--enrol", action="store_true", help="also enrol faces via the face service")
    seed.add_argument("--reset", action="store_true", help="delete existing demo employees first")
    seed.set_defaults(func=cmd_seed_demo)

    sync_sb = sub.add_parser("sync-supabase", help="replicate local SQLite database to Supabase Cloud")
    sync_sb.set_defaults(func=cmd_sync_supabase)

    prod_reset = sub.add_parser("production-reset", help="reset database to clean state with single HR admin account")
    prod_reset.set_defaults(func=cmd_production_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
