"""Small helpers shared by the web test modules."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


def csrf_of(client: TestClient, path: str = "/dashboard") -> str:
    """Pull the session CSRF token out of a rendered page."""
    page = client.get(path)
    match = CSRF_RE.search(page.text)
    assert match, f"no CSRF token found in {path} (HTTP {page.status_code})"
    return match.group(1)
