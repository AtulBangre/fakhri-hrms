"""Server-side sorting and search, for the lists split across pages."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.services import employees as employees_service
from app.services import records


def _names(page: str) -> list[str]:
    return re.findall(r'<a href="/employees/\d+"><strong>([^<]+)</strong>', page)


def test_employee_search_is_forgiving(storage, staff):
    find = lambda q: [p["name"] for p in employees_service.list_employees(storage, q=q)]
    assert find("  aarav ") == ["Aarav Sharma"]               # spaces and case
    assert find("sharma aarav") == ["Aarav Sharma"]           # any word order
    assert find("marketing") == ["Isha Verma"]                # department too
    assert find("aarav marketing") == []                      # every word must match
    assert employees_service.count_employees(storage, q="sharma aarav") == 1


@pytest.mark.parametrize("q", ["%", "_", "a%", "\\", "'; DROP TABLE employees; --"])
def test_search_treats_special_characters_as_plain_text(storage, staff, q):
    assert employees_service.list_employees(storage, q=q) == []
    assert employees_service.count_employees(storage, q=q) == 0


def test_a_blank_search_shows_everyone(storage, staff):
    assert len(employees_service.list_employees(storage, q="   ")) == len(staff)


def test_assets_search_works_the_same_way(storage, staff):
    records.add_asset(storage, tag="LAP-1", name="Dell Latitude", category="laptop", serial="SN9")
    assert [a["tag"] for a in records.list_assets(storage, q=" latitude dell ")] == ["LAP-1"]
    assert records.list_assets(storage, q="%") == []


@pytest.mark.parametrize("sort,direction,first", [
    ("name", "asc", "Aarav Sharma"), ("name", "desc", "Rohan Khan"),
    ("gross", "asc", "Rohan Khan"), ("gross", "desc", "Aarav Sharma"),
    ("department", "asc", "Aarav Sharma"), ("joined", "desc", "Rohan Khan"),
])
def test_employee_list_sorts_on_the_server(hr_client: TestClient, staff, sort, direction, first):
    page = hr_client.get(f"/employees?sort={sort}&dir={direction}").text
    assert _names(page)[0] == first


def test_sorting_covers_every_page_not_just_the_first(hr_client: TestClient, storage, monkeypatch):
    from app.routes import employees as route

    monkeypatch.setattr(route, "PER_PAGE", 2)
    for index in range(5):
        employees_service.create_employee(storage, {"name": f"Person {index}", "code": f"P{index}"})
    first = _names(hr_client.get("/employees?sort=name&dir=desc").text)
    assert first == ["Person 4", "Person 3"]
    second = hr_client.get("/employees?sort=name&dir=desc&page=2").text
    assert _names(second) == ["Person 2", "Person 1"]
    assert "sort=name" in re.search(r'href="/employees\?[^"]*page=1[^"]*"', second).group(0)


def test_an_unknown_sort_cannot_reach_the_database(hr_client: TestClient, staff):
    page = hr_client.get("/employees?sort=name;DROP TABLE employees&dir=sideways")
    assert page.status_code == 200 and _names(page.text)[0] == "Aarav Sharma"


def test_sort_links_flip_direction_and_keep_the_filters(hr_client: TestClient, staff):
    page = hr_client.get("/employees?q=a&sort=name&dir=asc").text
    link = re.search(r'<a class="th-sort" href="([^"]+)"[^>]*>\s*Employee', page).group(1).replace("&amp;", "&")
    assert "dir=desc" in link and "q=a" in link and "page" not in link
    assert 'aria-sort="ascending"' in page


def test_audit_log_sorts_and_searches(hr_client: TestClient, storage):
    storage.audit(actor="zed", action="alpha_action", detail="needle here")
    storage.audit(actor="amy", action="beta_action", detail="other")
    assert "needle here" in hr_client.get("/audit?q= NEEDLE ").text
    page = hr_client.get("/audit?sort=actor&dir=desc").text
    assert page.index(">zed<") < page.index(">amy<")
    assert hr_client.get("/audit?sort=nonsense&dir=x").status_code == 200
    assert hr_client.get("/audit?q=%25").status_code == 200
