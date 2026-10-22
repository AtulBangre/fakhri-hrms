"""Safe handling of uploaded files.

Three rules, because uploads are the classic way into a server:

1. **Never trust the filename.** The stored name is a random hex string plus a
   whitelisted extension; the original name is kept only as a display label.
2. **Sniff the bytes.** The declared content type is ignored; the magic number
   decides whether a file is a PDF, JPEG, PNG or WebP.
3. **Cap the size** before writing anything, and keep everything under
   ``data/uploads/<kind>/`` — no user-controlled path ever reaches ``open()``.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

#: Allowed types by magic number → (mime, extension)
ALLOWED = {
    "pdf": (b"%PDF-", "application/pdf", ".pdf"),
    "jpeg": (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    "png": (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    "webp": (b"RIFF", "image/webp", ".webp"),
}

MAX_BYTES = 8 * 1024 * 1024  # 8 MB


class UploadError(ValueError):
    """Rejected upload (too big, wrong type, empty)."""


@dataclass(slots=True)
class StoredFile:
    stored_name: str
    filename: str
    mime: str
    size: int
    sha256: str


def sniff(data: bytes) -> tuple[str, str] | None:
    """Return ``(mime, extension)`` for an allowed payload, else None."""
    if not data:
        return None
    for _key, (magic, mime, extension) in ALLOWED.items():
        if data.startswith(magic):
            if magic == b"RIFF" and not data[8:12] == b"WEBP":
                continue
            return mime, extension
    return None


def upload_dir(settings: Settings, kind: str) -> Path:
    """``data/uploads/<kind>/`` — ``kind`` is a fixed word chosen by our code."""
    if kind not in ("documents", "receipts", "selfies"):
        raise ValueError("unknown upload kind")
    path = Path(settings.data_dir) / "uploads" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_upload(settings: Settings, kind: str, data: bytes, *, original_name: str | None = None) -> StoredFile:
    """Validate and write one file. Raises :class:`UploadError`."""
    if not data:
        raise UploadError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise UploadError(f"The file is too large ({len(data) / 1_048_576:.1f} MB); the limit is 8 MB.")

    detected = sniff(data)
    if detected is None:
        raise UploadError("Only PDF, JPEG, PNG or WebP files are accepted.")

    mime, extension = detected
    stored_name = secrets.token_hex(16) + extension
    target = upload_dir(settings, kind) / stored_name
    target.write_bytes(data)

    clean = (original_name or f"file{extension}").strip()
    clean = clean.replace("/", "_").replace("\\", "_")[:120] or f"file{extension}"
    return StoredFile(
        stored_name=stored_name,
        filename=clean,
        mime=mime,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def path_for(settings: Settings, kind: str, stored_name: str) -> Path:
    """Resolve a stored file, refusing anything that is not a plain filename."""
    if not stored_name or "/" in stored_name or "\\" in stored_name or stored_name.startswith("."):
        raise UploadError("Invalid file reference.")
    path = upload_dir(settings, kind) / stored_name
    if not path.exists():
        raise UploadError("That file is no longer on the server.")
    return path


def delete_file(settings: Settings, kind: str, stored_name: str | None) -> None:
    if not stored_name:
        return
    try:
        path_for(settings, kind, stored_name).unlink()
    except (UploadError, OSError):
        pass


def human_size(size: int | None) -> str:
    if not size:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1_048_576:.1f} MB"
