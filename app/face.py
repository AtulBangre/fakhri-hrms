"""Client for the Face API service (the sibling ``faceapi`` project).

The HRMS deliberately owns **no face model**: detection and 1:N matching happen
in the Face API service, which keeps this app small, keeps the biometric maths
in one audited place, and means the HRMS is a real consumer of that API.

Every employee is enrolled into one collection (``HRMS_FACE_COLLECTION``) using
the employee **code** as the Face API ``external_id``. That single decision makes
matching trivial: whichever code comes back from ``/v1/identify`` is the person
standing at the gate.

If the face service is down, the HRMS keeps working — HR can still mark
attendance manually, and the kiosk shows a clear "scanner offline" state instead
of pretending to work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from .config import Settings

logger = logging.getLogger("hrms.face")


class FaceServiceError(RuntimeError):
    """Raised when the face service rejects a request."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class FaceServiceUnavailable(FaceServiceError):
    """Raised when the face service cannot be reached at all."""


@dataclass(slots=True)
class IdentificationResult:
    matched: bool
    external_id: str | None
    similarity: float | None
    face_id: str | None
    threshold: float | None
    faces_in_probe: int = 0
    gallery_size: int = 0
    candidates: list[dict] = field(default_factory=list)
    message: str | None = None

    @property
    def employee_code(self) -> str | None:
        return self.external_id


@dataclass(slots=True)
class EnrolmentResult:
    face_id: str
    det_score: float | None
    box: dict | None
    faces_in_image: int = 0


class FaceService:
    """Thin, well-behaved HTTP client for the Face API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.faceapi_url.rstrip("/")
        self.collection = settings.face_collection
        self.timeout = settings.face_timeout
        self._collection_ready = False

    # ------------------------------------------------------------- internals
    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.faceapi_key} if self.settings.faceapi_key else {}

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, headers=self._headers, timeout=self.timeout)

    @staticmethod
    def _error_from_response(response: httpx.Response) -> FaceServiceError:
        code = None
        message = f"Face service returned HTTP {response.status_code}"
        try:
            body = response.json()
            error = body.get("error") or {}
            code = error.get("code")
            if error.get("message"):
                message = error["message"]
        except (ValueError, AttributeError):
            pass
        return FaceServiceError(message, status_code=response.status_code, code=code)

    # ---------------------------------------------------------------- health
    def health(self) -> dict:
        """Return ``{"ok": bool, "detail": str, ...}`` — never raises."""
        try:
            with self._client() as client:
                root = client.get("/")
                if root.status_code >= 400:
                    error = self._error_from_response(root)
                    return {"ok": False, "detail": error.message, "status_code": root.status_code}
                info = root.json()
                ready = client.get("/readyz")
                return {
                    "ok": ready.status_code == 200,
                    "detail": "connected",
                    "auth_mode": info.get("auth_mode"),
                    "models": info.get("models", {}),
                    "version": info.get("version"),
                    "collection": self.collection,
                }
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"cannot reach {self.base_url}: {exc.__class__.__name__}"}

    # ----------------------------------------------------------- collections
    def ensure_collection(self) -> None:
        """Create the employee collection if it does not exist yet."""
        if self._collection_ready:
            return
        try:
            with self._client() as client:
                response = client.post(
                    "/v1/collections",
                    json={"name": self.collection, "description": "Fakhri HRMS employee faces"},
                )
                if response.status_code in (200, 201, 409):  # 409 = already exists
                    self._collection_ready = True
                    return
                raise self._error_from_response(response)
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc

    def collection_stats(self) -> dict:
        try:
            with self._client() as client:
                response = client.get(f"/v1/collections/{self.collection}")
                if response.status_code == 404:
                    return {"name": self.collection, "face_count": 0, "exists": False}
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                data = response.json()
                data["exists"] = True
                return data
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc

    def list_faces(self, *, limit: int = 500) -> list[dict]:
        try:
            with self._client() as client:
                response = client.get(f"/v1/collections/{self.collection}/faces", params={"limit": limit})
                if response.status_code == 404:
                    return []
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                return response.json().get("faces", [])
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc

    # -------------------------------------------------------------- enrol
    def enrol_face(self, image_bytes: bytes, *, external_id: str, filename: str = "face.jpg") -> EnrolmentResult:
        self.ensure_collection()
        try:
            with self._client() as client:
                response = client.post(
                    f"/v1/collections/{self.collection}/faces",
                    files={"image": (filename, image_bytes, "image/jpeg")},
                    data={"external_id": external_id, "metadata": '{"source":"hrms"}'},
                )
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                body = response.json()
                return EnrolmentResult(
                    face_id=body["face_id"],
                    det_score=body.get("det_score"),
                    box=body.get("box"),
                    faces_in_image=1,
                )
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc

    def delete_face(self, face_id: str) -> bool:
        try:
            with self._client() as client:
                response = client.delete(f"/v1/collections/{self.collection}/faces/{face_id}")
                if response.status_code == 404:
                    return False
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                return True
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc

    # ------------------------------------------------------------ identify
    def identify(self, image_bytes: bytes, *, filename: str = "scan.jpg") -> IdentificationResult:
        """1:N match one captured frame against the employee collection."""
        self.ensure_collection()
        try:
            with self._client() as client:
                response = client.post(
                    "/v1/identify",
                    params={"collection": self.collection, "top_k": 3},
                    files={"image": (filename, image_bytes, "image/jpeg")},
                )
                if response.status_code == 422:
                    # Most common case by far: no face in the frame.
                    detail = "No face detected in the frame."
                    try:
                        detail = response.json()["error"]["message"]
                    except (ValueError, KeyError):
                        pass
                    return IdentificationResult(
                        matched=False, external_id=None, similarity=None, face_id=None,
                        threshold=None, message=detail,
                    )
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                body = response.json()
                best = body.get("best_match") or {}
                return IdentificationResult(
                    matched=bool(body.get("match")),
                    external_id=best.get("external_id"),
                    similarity=best.get("similarity"),
                    face_id=best.get("face_id"),
                    threshold=body.get("threshold"),
                    faces_in_probe=int(body.get("faces_in_probe") or 0),
                    gallery_size=int(body.get("gallery_size") or 0),
                    candidates=body.get("candidates") or [],
                )
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc

    def embedding(self, image_bytes: bytes, *, filename: str = "face.jpg") -> dict:
        """Utility used by the enrolment preview (score + box, no gallery write)."""
        self.ensure_collection()
        try:
            with self._client() as client:
                response = client.post("/v1/embed", files={"image": (filename, image_bytes, "image/jpeg")})
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                return response.json()
        except httpx.HTTPError as exc:
            raise FaceServiceUnavailable(f"Cannot reach the face service at {self.base_url}.") from exc
