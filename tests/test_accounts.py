"""Accounts, staff-only pages and request validation (needs pytest-django)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery-9"


@pytest.fixture
def web():
    return Client()


@pytest.fixture
def user():
    return get_user_model().objects.create_user("alice", password=PASSWORD)


@pytest.fixture
def staff():
    return get_user_model().objects.create_user("boss", password=PASSWORD, is_staff=True)


def test_signup_creates_an_account_and_logs_in(web):
    response = web.post(
        "/accounts/signup/",
        {"username": "newuser", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert response.status_code == 302
    assert get_user_model().objects.filter(username="newuser").exists()
    assert "_auth_user_id" in web.session


def test_signup_rejects_a_weak_password(web):
    response = web.post(
        "/accounts/signup/",
        {"username": "weak", "password1": "12345678", "password2": "12345678"},
    )
    assert response.status_code == 400
    assert not get_user_model().objects.filter(username="weak").exists()


def test_signup_can_be_closed(web, settings):
    settings.SIGNUPS_OPEN = False
    assert web.get("/accounts/signup/").status_code == 404


def test_login_and_logout(web, user):
    assert web.post("/accounts/login/", {"username": "alice", "password": PASSWORD}).status_code == 302
    assert "_auth_user_id" in web.session
    # Logging out is a POST (a plain link must not be able to log someone out).
    assert web.get("/accounts/logout/").status_code == 405
    assert web.post("/accounts/logout/").status_code == 302
    assert "_auth_user_id" not in web.session


def test_login_locks_out_after_repeated_failures(web, user):
    from accounts.throttle import login_throttle

    login_throttle.clear("user:alice")
    for _ in range(8):
        assert web.post("/accounts/login/", {"username": "alice", "password": "wrong"}).status_code == 200
    blocked = web.post("/accounts/login/", {"username": "alice", "password": PASSWORD})
    assert blocked.status_code == 429
    login_throttle.clear("user:alice", "ip:127.0.0.1")


def test_settings_api_needs_login(web):
    assert web.get("/api/account/settings").status_code == 401


def test_settings_roundtrip_and_csrf(user):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    # Without the token a PUT is refused.
    assert strict.put("/api/account/settings", json.dumps({"theme": "dark"}), content_type="application/json").status_code == 403
    # The index page sets the csrftoken cookie for signed-in users.
    strict.get("/")
    token = strict.cookies["csrftoken"].value
    ok = strict.put(
        "/api/account/settings",
        json.dumps({"theme": "dark"}),
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert ok.status_code == 200
    assert strict.get("/api/account/settings").json()["data"] == {"theme": "dark"}


def test_settings_are_per_user(user, staff):
    first, second = Client(), Client()
    first.force_login(user)
    second.force_login(staff)
    first.put("/api/account/settings", json.dumps({"a": 1}), content_type="application/json")
    assert second.get("/api/account/settings").json()["data"] == {}


def test_reports_page_is_staff_only(web, user, staff):
    anonymous = web.get("/reports")
    assert anonymous.status_code == 302 and "/accounts/login/" in anonymous["Location"]
    web.force_login(user)
    assert web.get("/reports").status_code == 404
    web.force_login(staff)
    assert web.get("/reports").status_code == 200


def test_report_deletion_requires_staff_and_csrf(user, staff):
    anonymous = Client(enforce_csrf_checks=True)
    assert anonymous.post("/reports/delete?received_at=x").status_code == 403
    member = Client()
    member.force_login(user)
    assert member.post("/reports/delete?received_at=x").status_code == 404
    admin = Client()
    admin.force_login(staff)
    assert admin.post("/reports/delete?received_at=x").status_code == 303


def test_search_log_is_not_public(web, staff):
    assert web.get("/api/search-log").status_code == 403
    web.force_login(staff)
    assert web.get("/api/search-log").status_code == 200


def test_search_validation_matches_the_old_api(web):
    assert web.get("/api/search?q=a&limit=0").status_code == 422
    assert web.get("/api/search?q=a&limit=26").status_code == 422
    assert web.get("/api/search?q=a&offset=-1").status_code == 422
    assert web.get("/api/search?q=a&taiwan=maybe").status_code == 422


def test_annotate_rejects_bad_bodies(web):
    assert web.post("/api/annotate", "not json", content_type="application/json").status_code == 422
    assert web.post("/api/annotate", json.dumps({"text": 5}), content_type="application/json").status_code == 422


def test_report_endpoint_is_throttled(web, tmp_path, monkeypatch):
    from dictionary import views

    monkeypatch.setattr(views, "_REPORTS_PATH", tmp_path / "reports.jsonl")
    views._report_hits.clear()
    body = json.dumps({"kind": "bug", "word": "好", "message": "hi"})
    codes = [web.post("/api/report", body, content_type="application/json").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_repeated_signup_post_logs_in_instead_of_failing(web):
    """The Worker may deliver a signup twice (laptop wrote, then a 502, then tablet)."""
    data = {"username": "twice", "password1": PASSWORD, "password2": PASSWORD}
    assert web.post("/accounts/signup/", data).status_code == 302
    web.post("/accounts/logout/")
    again = web.post("/accounts/signup/", data)
    assert again.status_code == 302
    assert "_auth_user_id" in web.session
    assert get_user_model().objects.filter(username="twice").count() == 1


def test_repeated_signup_with_a_different_password_is_still_refused(web):
    web.post("/accounts/signup/", {"username": "taken", "password1": PASSWORD, "password2": PASSWORD})
    web.post("/accounts/logout/")
    other = "another-long-password-7"
    response = web.post("/accounts/signup/", {"username": "taken", "password1": other, "password2": other})
    assert response.status_code == 400
    assert "_auth_user_id" not in web.session


def test_auth_pages_use_the_site_palette_and_dynamic_requirements(web):
    """The auth pages match the site and only explain rules after a bad input."""
    login = web.get("/accounts/login/").content.decode()
    signup = web.get("/accounts/signup/").content.decode()
    for html in (login, signup):
        assert "/static/variables.css" in html
        assert "/static/auth.css" in html
        assert "/static/auth.js" in html
        assert "#2f6fed" not in html

    root = Path(__file__).resolve().parents[1]
    variables = (root / "yabai_search/static/variables.css").read_text(encoding="utf-8")
    assert "--accent: #ffca28" in variables
    css = (root / "yabai_search/static/auth.css").read_text(encoding="utf-8")
    assert ".helptext" in css and "display: none" in css
    assert ".helptext.show" in css
    js = (root / "yabai_search/static/auth.js").read_text(encoding="utf-8")
    assert 'help.classList.toggle("show"' in js


def test_signup_accepts_a_short_password(web):
    """The old 10-character minimum is gone."""
    response = web.post(
        "/accounts/signup/",
        {"username": "shorty", "password1": "abc", "password2": "abc"},
    )
    assert response.status_code == 302
    assert get_user_model().objects.filter(username="shorty").exists()


def test_signup_accepts_a_common_password(web):
    """The CommonPasswordValidator is gone."""
    response = web.post(
        "/accounts/signup/",
        {"username": "commonuser", "password1": "password", "password2": "password"},
    )
    assert response.status_code == 302
    assert get_user_model().objects.filter(username="commonuser").exists()


def test_logged_in_menu_shows_name_beside_avatar_and_logout_on_right(web, user):
    web.force_login(user)
    html = web.get("/").content.decode()
    assert 'class="menu-account-left"' in html
    assert 'class="menu-account-name">alice<' in html
    assert ">Log out<" in html
    assert "Log out (alice)" not in html


# --- multi-host: only the global writer may write ---------------------------------------


@pytest.fixture
def host(monkeypatch, tmp_path, settings):
    """Pretend to be the host called 'laptop' on a multi-host install."""
    monkeypatch.setenv("YABAI_REPL_NODE", "laptop")
    from django.http import HttpResponse
    from yabai_site import middleware

    gate = middleware.WriterGateMiddleware(lambda request: HttpResponse("ran"))
    gate.epoch_file = tmp_path / "writer-epoch"
    return gate


def _request(path, method="POST", **headers):
    from django.test import RequestFactory

    meta = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()}
    return getattr(RequestFactory(), method.lower())(path, **meta)


def test_gate_refuses_a_write_without_the_worker_headers(host):
    assert host(_request("/accounts/signup/")).status_code == 421


def test_gate_refuses_a_write_meant_for_another_host_or_an_old_epoch(host):
    assert host(_request("/accounts/signup/", x_yabai_writer="tablet", x_yabai_writer_epoch="3")).status_code == 421
    assert host(_request("/accounts/signup/", x_yabai_writer="laptop", x_yabai_writer_epoch="4")).status_code == 200
    assert host(_request("/accounts/signup/", x_yabai_writer="laptop", x_yabai_writer_epoch="3")).status_code == 421


def test_gate_lets_reads_and_logins_through_on_any_host(host):
    assert host(_request("/api/search", "GET")).status_code == 200
    assert host(_request("/accounts/login/")).status_code == 200  # sessions are cookies: no write
    assert host(_request("/api/convert")).status_code == 200


def test_gate_is_off_without_a_node_name(monkeypatch):
    monkeypatch.delenv("YABAI_REPL_NODE", raising=False)
    from django.http import HttpResponse
    from yabai_site import middleware

    gate = middleware.WriterGateMiddleware(lambda request: HttpResponse("ran"))
    assert gate(_request("/accounts/signup/")).status_code == 200


def test_settings_get_does_not_create_a_row(web, user):
    from accounts.models import UserSettings

    web.force_login(user)
    assert web.get("/api/account/settings").json() == {"data": {}, "updated_at": None}
    assert not UserSettings.objects.exists()
