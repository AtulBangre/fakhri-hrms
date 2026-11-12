"""Tests for Supabase Cloud Database Sync Engine and Settings Integration."""

from unittest.mock import MagicMock, patch

from app.services import sync_supabase
from tests.helpers import csrf_of


def test_map_type():
    assert sync_supabase.map_type("INTEGER") == "BIGINT"
    assert sync_supabase.map_type("INT") == "BIGINT"
    assert sync_supabase.map_type("REAL") == "DOUBLE PRECISION"
    assert sync_supabase.map_type("FLOAT") == "DOUBLE PRECISION"
    assert sync_supabase.map_type("BLOB") == "BYTEA"
    assert sync_supabase.map_type("TEXT") == "TEXT"
    assert sync_supabase.map_type("VARCHAR(255)") == "TEXT"
    assert sync_supabase.map_type("") == "TEXT"


def test_mask_url():
    url = "postgresql://postgres:mysecretpassword@db.xyz.supabase.co:5432/postgres"
    masked = sync_supabase.mask_url(url)
    assert "mysecretpassword" not in masked
    assert "••••••••" in masked
    assert "db.xyz.supabase.co" in masked
    assert sync_supabase.mask_url("") == ""


def test_get_effective_config_defaults(storage, settings):
    cfg = sync_supabase.get_effective_config(storage, settings)
    assert cfg["enabled"] is False
    assert cfg["db_url"] == ""
    assert cfg["interval"] >= 15


def test_get_effective_config_overrides(storage, settings):
    storage.set_setting("supabase_sync_enabled", "1")
    storage.set_setting("supabase_db_url", "postgresql://test:pass@localhost:5432/test")
    storage.set_setting("supabase_sync_interval", "120")

    cfg = sync_supabase.get_effective_config(storage, settings)
    assert cfg["enabled"] is True
    assert cfg["db_url"] == "postgresql://test:pass@localhost:5432/test"
    assert cfg["interval"] == 120


def test_test_connection_validation():
    ok, msg = sync_supabase.test_connection("")
    assert not ok
    assert "cannot be empty" in msg


def test_test_connection_failure():
    ok, msg = sync_supabase.test_connection("postgresql://invalid:nopass@127.0.0.1:59999/dummy")
    assert not ok
    assert "Connection failed" in msg


def test_get_sync_status(storage, settings):
    status = sync_supabase.get_sync_status(storage, settings)
    assert "has_psycopg2" in status
    assert "enabled" in status
    assert "masked_db_url" in status
    assert "is_running" in status


def test_settings_supabase_routes(hr_client, storage):
    token = csrf_of(hr_client, "/settings")

    # 1. Test saving Supabase config via route
    res = hr_client.post(
        "/settings/supabase/config",
        data={
            "_csrf": token,
            "enabled": "1",
            "db_url": "postgresql://postgres:secret@db.project.supabase.co:5432/postgres",
            "interval": "60",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/settings#supabase")

    assert storage.get_setting("supabase_sync_enabled") == "1"
    assert "postgres:secret" in storage.get_setting("supabase_db_url")
    assert storage.get_setting("supabase_sync_interval") == "60"

    # 2. Test connection route
    res_test = hr_client.post(
        "/settings/supabase/test",
        data={"_csrf": token, "db_url": "postgresql://invalid:nopass@127.0.0.1:59999/dummy"},
        follow_redirects=False,
    )
    assert res_test.status_code == 303
    assert res_test.headers["location"].startswith("/settings#supabase")

    # 3. Test settings page renders Supabase card
    page = hr_client.get("/settings")
    assert page.status_code == 200
    assert "Supabase Cloud Database Mirror" in page.text
    assert "••••••••" in page.text

