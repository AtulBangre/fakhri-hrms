"""Shared fixtures: a throwaway database, brand settings and a fake face service."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Storage
from app.face import EnrolmentResult, IdentificationResult
from app.services import employees as employees_service
from app.services import leave as leave_service

HR_USERNAME = "hr"
HR_PASSWORD = "changeme123"
KIOSK_USERNAME = "gate"
KIOSK_PASSWORD = "gatepass123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = Settings(
        data_dir=tmp_path,
        timezone="Asia/Kolkata",
        kiosk_debounce_seconds=45,
        log_level="WARNING",
        default_hr_username=HR_USERNAME,
        default_hr_password=HR_PASSWORD,
        default_kiosk_username=KIOSK_USERNAME,
        default_kiosk_password=KIOSK_PASSWORD,
    )
    config.load_or_create_secret()
    return config


@pytest.fixture
def storage(settings: Settings):
    store = Storage(settings)
    yield store
    store.close()


def _get_or_create_user(storage: Storage, *, username: str, password: str, role: str,
                        display_name: str, device_label: str | None = None) -> dict:
    """The app itself creates the first logins at startup, so be idempotent here."""
    from app import auth

    user = auth.get_user_by_username(storage, username)
    if user is None:
        user = auth.create_user(
            storage, username=username, password=password, role=role,
            display_name=display_name, device_label=device_label,
        )
    else:
        # Pin the password so the tests can always sign in.
        auth.change_password(storage, user["id"], password)
    return user


@pytest.fixture
def hr_user(storage: Storage):
    from app import auth

    return _get_or_create_user(
        storage, username=HR_USERNAME, password=HR_PASSWORD, role=auth.ROLE_HR, display_name="HR Admin"
    )


@pytest.fixture
def kiosk_user(storage: Storage):
    from app import auth

    return _get_or_create_user(
        storage, username=KIOSK_USERNAME, password=KIOSK_PASSWORD, role=auth.ROLE_KIOSK,
        display_name="Gate Scanner", device_label="Main Gate",
    )


class FakeFaceService:
    """In-memory stand-in for the Face API, so tests need no model or network."""

    def __init__(self, *, fail_health: bool = False, identify_result: IdentificationResult | None = None):
        self.settings = None
        self.collection = "employees"
        self.enrolled: dict[str, str] = {}  # external_id -> face_id
        self.deleted: list[str] = []
        self.identify_result = identify_result
        self.fail_health = fail_health
        self._counter = 0

    # API used by the services -------------------------------------------
    def ensure_collection(self) -> None:
        return None

    def collection_stats(self) -> dict:
        return {
            "name": self.collection,
            "face_count": len(self.enrolled),
            "exists": True,
        }

    def health(self) -> dict:
        if self.fail_health:
            return {"ok": False, "detail": "cannot reach http://127.0.0.1:8000"}
        return {"ok": True, "detail": "connected", "auth_mode": "api_key", "collection": self.collection}

    def enrol_face(self, image_bytes: bytes, *, external_id: str, filename: str = "face.jpg") -> EnrolmentResult:
        if not image_bytes:
            raise ValueError("empty image")
        self._counter += 1
        face_id = f"face_{self._counter:04d}"
        self.enrolled[external_id] = face_id
        return EnrolmentResult(face_id=face_id, det_score=0.93, box={"x": 10, "y": 10, "width": 100, "height": 120})

    def delete_face(self, face_id: str) -> bool:
        self.deleted.append(face_id)
        for external_id, stored in list(self.enrolled.items()):
            if stored == face_id:
                del self.enrolled[external_id]
        return True

    def list_faces(self, *, limit: int = 500) -> list[dict]:
        return [{"face_id": face_id, "external_id": external_id} for external_id, face_id in self.enrolled.items()]

    def identify(self, image_bytes: bytes, *, filename: str = "scan.jpg") -> IdentificationResult:
        if self.identify_result is not None:
            return self.identify_result
        return IdentificationResult(
            matched=False, external_id=None, similarity=None, face_id=None, threshold=0.363,
            faces_in_probe=0, gallery_size=len(self.enrolled), message="No face detected in the frame.",
        )


@pytest.fixture
def fake_face() -> FakeFaceService:
    return FakeFaceService()


#: The HRMS reuses the sample portraits that ship with the Face API project, so
#: there is only one copy of the test imagery in the repository.
FACEAPI_ASSETS = Path(__file__).resolve().parent.parent.parent / "faceapi" / "tests" / "assets"


@pytest.fixture(scope="session")
def assets() -> dict[str, bytes]:
    files = {
        "person_a_1": "person_a_1.jpg",
        "person_a_2": "person_a_2.jpg",
        "person_b_1": "person_b_1.jpg",
        "no_face": "no_face.png",
    }
    missing = [name for name in files.values() if not (FACEAPI_ASSETS / name).exists()]
    if missing:
        pytest.skip(f"faceapi test assets missing: {', '.join(missing)}")
    return {key: (FACEAPI_ASSETS / name).read_bytes() for key, name in files.items()}


@pytest.fixture
def staff(storage: Storage) -> list[dict]:
    """Three employees with predictable codes and salaries."""
    created = []
    specs = [
        ("Aarav Sharma", "Engineering", "Backend Developer", "2025-04-01", 60000.0),
        ("Isha Verma", "Marketing", "Content Lead", "2025-06-15", 45000.0),
        ("Rohan Khan", "Operations", "Fulfilment Executive", "2026-01-05", 28000.0),
    ]
    for name, department, designation, joined, gross in specs:
        employee_id = employees_service.create_employee(
            storage,
            {
                "name": name,
                "department": department,
                "designation": designation,
                "joined_on": joined,
                "monthly_gross": gross,
                "employment_type": "full_time",
                "status": "active",
            },
        )
        employee = employees_service.get_employee(storage, employee_id)
        assert employee is not None
        created.append(employee)
    return created


@pytest.fixture
def aarav(staff: list[dict]) -> dict:
    return staff[0]


@pytest.fixture
def isha(staff: list[dict]) -> dict:
    return staff[1]


def at(hour: int, minute: int, day: date = date(2026, 1, 5)) -> datetime:
    """Deterministic naive local timestamp for attendance tests."""
    return datetime(day.year, day.month, day.day, hour, minute, 0)


@pytest.fixture
def apply_leave(storage: Storage, settings: Settings):
    def _apply(employee_id: int, **kwargs):
        return leave_service.apply_leave(storage, settings, employee_id=employee_id, **kwargs)

    return _apply
