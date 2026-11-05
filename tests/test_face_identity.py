"""The kiosk must show the right person, and must never treat 'unknown' as 'live'."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.face import FaceService, IdentificationResult
from app.services import employees as employees_service
from scripts.face_gallery_report import analyse
from tests.conftest import KIOSK_PASSWORD, KIOSK_USERNAME, FakeFaceService


def _key(employee):
    return f"emp-{employee['id']}"


def _candidate(employee_or_key, face_id, similarity):
    key = employee_or_key if isinstance(employee_or_key, str) else _key(employee_or_key)
    return {"external_id": key, "face_id": face_id, "similarity": similarity}


def _result(*candidates, threshold=0.5, margin=0.08, **extra):
    top = candidates[0] if candidates else {}
    return IdentificationResult(
        matched=bool(candidates),
        external_id=top.get("external_id"),
        similarity=top.get("similarity"),
        face_id=top.get("face_id"),
        threshold=threshold,
        margin=margin,
        faces_in_probe=1,
        gallery_size=len(candidates),
        candidates=list(candidates),
        **extra,
    )


def _set_face(storage, employee, face_id):
    storage.execute("UPDATE employees SET face_id = ? WHERE id = ?", (face_id, employee["id"]))
    return {**employee, "face_id": face_id}


# ------------------------------------------------------------ the resolver
def test_old_template_cannot_show_a_new_employee(storage, staff):
    """A reused row id: the template says emp-1, but emp-1 is now someone else."""
    employee = _set_face(storage, staff[0], "new-template")
    outcome = employees_service.resolve_face_match(storage, _result(_candidate(employee, "old-template", 0.9)))
    assert outcome.employee is None and outcome.reason == "stale_only"


def test_current_template_matches(storage, staff):
    employee = _set_face(storage, staff[0], "current")
    outcome = employees_service.resolve_face_match(storage, _result(_candidate(employee, "current", 0.9)))
    assert outcome.employee["id"] == employee["id"] and outcome.similarity == 0.9


def test_stale_rows_are_seen_past_and_are_not_rivals(storage, staff):
    employee = _set_face(storage, staff[0], "current")
    result = _result(
        _candidate("emp-9999", "orphan", 0.92),
        _candidate(employee, "old", 0.91),
        _candidate(employee, "current", 0.90),
    )
    assert employees_service.resolve_face_match(storage, result).employee["id"] == employee["id"]


def test_close_rivals_mean_nobody(storage, staff):
    first, second = _set_face(storage, staff[0], "a"), _set_face(storage, staff[1], "b")
    result = _result(_candidate(first, "a", 0.70), _candidate(second, "b", 0.66))
    outcome = employees_service.resolve_face_match(storage, result)
    assert outcome.employee is None and outcome.reason == "ambiguous"


def test_below_threshold_means_nobody(storage, staff):
    employee = _set_face(storage, staff[0], "a")
    outcome = employees_service.resolve_face_match(storage, _result(_candidate(employee, "a", 0.4)))
    assert outcome.employee is None and outcome.reason == "below_threshold"


def test_employee_with_no_face_id_is_not_matched(storage, staff):
    outcome = employees_service.resolve_face_match(storage, _result(_candidate(staff[0], "anything", 0.9)))
    assert outcome.employee is None and outcome.reason == "stale_only"


# ----------------------------------------------------- the duplicate check
def test_stale_template_does_not_block_a_valid_enrolment(storage, staff):
    fake = FakeFaceService()
    target = _set_face(storage, staff[1], "second")
    _set_face(storage, staff[0], "first-current")
    fake.identify_result = _result(_candidate(staff[0], "first-old", 0.9))
    assert employees_service.face_owner_conflict(storage, fake, target["id"], b"photo") is None


def test_current_template_of_someone_else_still_blocks(storage, staff):
    fake = FakeFaceService()
    first = _set_face(storage, staff[0], "first")
    fake.identify_result = _result(_candidate(first, "first", 0.9))
    assert employees_service.face_owner_conflict(storage, fake, staff[1]["id"], b"photo")["id"] == first["id"]


# ------------------------------------------------------------ the kiosk route
@pytest.fixture
def kiosk(client, app, storage, staff, kiosk_user):
    fake = FakeFaceService()
    app.state.face = fake
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    return client, fake, app


def _scan(client):
    return client.post("/api/kiosk/scan", files={"image": ("f.jpg", b"jpeg", "image/jpeg")})


def _audit(storage, action):
    return storage.query("SELECT detail FROM audit_log WHERE action = ?", (action,))


def test_enforce_blocks_a_stale_template(kiosk, storage, staff):
    client, fake, app = kiosk
    app.state.settings.face_template_check = "enforce"
    _set_face(storage, staff[0], "current")
    fake.identify_result = _result(_candidate(staff[0], "old", 0.9))
    response = _scan(client)
    assert response.status_code == 409 and response.json()["action"] == "unknown_employee"
    assert _audit(storage, "kiosk_template_mismatch")
    assert storage.scalar("SELECT COUNT(*) AS n FROM attendance") == 0


def test_enforce_marks_the_right_person_past_stale_rows(kiosk, storage, staff):
    client, fake, app = kiosk
    app.state.settings.face_template_check = "enforce"
    _set_face(storage, staff[0], "current")
    fake.identify_result = _result(_candidate("emp-9999", "orphan", 0.95), _candidate(staff[0], "current", 0.9))
    body = _scan(client).json()
    assert body["action"] == "check_in" and body["card"]["code"] == staff[0]["code"]


def test_log_mode_keeps_working_but_records_the_difference(kiosk, storage, staff):
    client, fake, app = kiosk
    assert app.state.settings.face_template_check == "log"
    _set_face(storage, staff[0], "current")
    fake.identify_result = _result(_candidate(staff[0], "old", 0.9))
    assert _scan(client).json()["action"] == "check_in"
    assert "would_block=stale_only" in _audit(storage, "kiosk_template_mismatch")[0]["detail"]


def test_kiosk_asks_for_checks_on_a_scan(kiosk, storage, staff):
    client, fake, _ = kiosk
    fake.identify_result = _result()
    _scan(client)
    assert fake.identify_checks == [True]


def test_spoof_marks_nothing(kiosk, storage, staff):
    client, fake, _ = kiosk
    fake.identify_result = _result(decision="spoof", is_live=False, liveness_score=0.1, spoof_reason="liveness_failed")
    assert _scan(client).json()["action"] == "spoof_detected"
    assert storage.scalar("SELECT COUNT(*) AS n FROM attendance") == 0


def test_null_liveness_is_not_a_spoof(kiosk, storage, staff):
    client, fake, _ = kiosk
    employee = _set_face(storage, staff[0], "current")
    fake.identify_result = _result(_candidate(employee, "current", 0.9), is_live=None, liveness_score=None)
    assert _scan(client).json()["action"] == "check_in"


def test_retry_shows_the_reason_and_marks_nothing(kiosk, storage, staff):
    client, fake, _ = kiosk
    fake.identify_result = _result(decision="retry", retry_reason="too_dark")
    body = _scan(client).json()
    assert body["action"] == "retry" and "dark" in body["message"]
    assert storage.scalar("SELECT COUNT(*) AS n FROM attendance") == 0


def test_repeated_spoofs_pause_the_device(kiosk):
    client, fake, _ = kiosk
    fake.identify_result = _result(decision="spoof", is_live=False, spoof_reason="liveness_failed")
    for _ in range(5):
        assert _scan(client).json()["action"] == "spoof_detected"
    response = _scan(client)
    assert response.status_code == 429 and response.json()["action"] == "spoof_lockout"


def test_head_turn_scan_sends_all_frames_and_keeps_one_photo(kiosk, storage, staff):
    client, fake, _ = kiosk
    employee = _set_face(storage, staff[0], "current")
    fake.identify_result = _result(_candidate(employee, "current", 0.9), decision="match", is_live=True)
    challenge = client.post("/api/kiosk/challenge").json()
    files = [("frames", (f"{i}.jpg", b"jpeg", "image/jpeg")) for i in range(5)]
    body = client.post("/api/kiosk/scan", data={"challenge_token": challenge["challenge_token"]}, files=files).json()
    assert body["action"] == "check_in"
    assert fake.secure_calls == [(5, "token-1")]


def test_too_many_frame_bytes_are_refused(kiosk):
    client, _, _ = kiosk
    files = [("frames", (f"{i}.jpg", b"x" * (3 * 1024 * 1024), "image/jpeg")) for i in range(5)]
    response = client.post("/api/kiosk/scan", data={"challenge_token": "t"}, files=files)
    assert response.status_code == 413


def test_debounced_check_out_saves_no_photo(kiosk, app, settings, storage, staff, monkeypatch):
    client, fake, app_ = kiosk
    import app.uploads as uploads

    employee = _set_face(storage, staff[0], "current")
    fake.identify_result = _result(_candidate(employee, "current", 0.9))
    assert _scan(client).json()["action"] == "check_in"
    saved = []
    real = uploads.save_upload
    monkeypatch.setattr(uploads, "save_upload", lambda *a, **k: saved.append(1) or real(*a, **k))
    assert _scan(client).json()["action"] == "duplicate"
    assert saved == []


# ------------------------------------------------------------ the HTTP client
def _service(handler) -> FaceService:
    service = FaceService(Settings(faceapi_url="http://faceapi.test", faceapi_key="secret"))
    service._collection_ready = True
    service._pool = httpx.Client(base_url="http://faceapi.test", transport=httpx.MockTransport(handler))
    return service


def _identify_body(**extra):
    return {
        "match": False, "threshold": 0.5, "margin": 0.08, "decision": "no_match", "candidates": [],
        "faces_in_probe": 1, "gallery_size": 1, **extra,
    }


@pytest.mark.parametrize("liveness", [{"is_live": None, "liveness_score": None}, {}])
def test_null_or_missing_liveness_does_not_crash_and_is_not_live(liveness):
    service = _service(lambda request: httpx.Response(200, json=_identify_body(**liveness)))
    result = service.identify(b"jpeg")
    assert result.is_live is None and result.liveness_score is None
    assert result.margin == 0.08


def test_client_asks_for_ten_candidates_and_checks():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=_identify_body())

    _service(handler).identify(b"jpeg", checks=True)
    assert seen["top_k"] == "10" and seen["checks"] == "true"


def test_secure_call_sends_frames_and_token():
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, request.read()
        return httpx.Response(
            200, json=_identify_body(decision="retry", reason="too_far", is_live=None, liveness_score=None)
        )

    result = _service(handler).identify_secure([b"one", b"two"], challenge_token="tok")
    assert seen["path"] == "/v1/identify/secure"
    assert b'name="frames"' in seen["body"] and b"tok" in seen["body"]
    assert result.decision == "retry" and result.retry_reason == "too_far"


# ------------------------------------------------------------ the report
def test_report_finds_every_kind_of_problem():
    employees = [
        {"id": 1, "code": "E1", "status": "active", "face_id": "good"},
        {"id": 2, "code": "E2", "status": "active", "face_id": "missing"},
        {"id": 3, "code": "E3", "status": "active", "face_id": None},
        {"id": 4, "code": "E4", "status": "active", "face_id": "tiny"},
    ]
    faces = [
        {"face_id": "good", "external_id": "emp-1", "det_score": 0.95, "box": [0, 0, 120, 120]},
        {"face_id": "old", "external_id": "emp-1", "det_score": 0.95, "box": [0, 0, 120, 120]},
        {"face_id": "orphan", "external_id": "emp-99", "det_score": 0.95, "box": [0, 0, 120, 120]},
        {"face_id": "tiny", "external_id": "emp-4", "det_score": 0.95, "box": [0, 0, 31, 40]},
    ]
    report = analyse(employees, faces)
    assert report["orphan_templates"] == ["orphan"]
    assert report["stale_templates"] == [{"face_id": "old", "employee_code": "E1"}]
    assert report["low_quality_templates"][0]["employee_code"] == "E4"
    reasons = {row["employee_code"]: row["reason"] for row in report["active_without_matching_template"]}
    assert reasons == {"E2": "template missing", "E3": "not enrolled"}
