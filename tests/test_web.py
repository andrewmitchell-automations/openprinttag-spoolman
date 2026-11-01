import json
import os
import stat
import time

import pytest
import respx

from bridge.config import read_config
from bridge.connect import Tokens
from bridge.service import Service
from bridge.web import create_app
from tests.test_bridge import GRAPHQL, SPOOLMAN, UUID, make_jwt, mock_fields, printer

PASSWORD = "correct-password"
ME = {"data": {"authorization": {"me": {"email": "a@example.com", "organizations": [{"id": "org-1", "name": "Home"}]}}}}
PRINTERS = {"data": {"printer": {"printers": {"__typename": "PrinterConnection", "edges": [{"node": {"id": "printer-1", "name": "Core", "state": "IDLE"}}]}}}}


def graphql(request):
    body = json.loads(request.content)
    if "BridgeMe" in body["query"]:
        return respx.MockResponse(json=ME)
    if "BridgePrinters" in body["query"]:
        return respx.MockResponse(json=PRINTERS)
    return respx.MockResponse(json={"data": {"printer": {"printer": printer()}}})


def make_service(tmp_path, **env):
    return Service({"DATA_DIR": str(tmp_path), "CONNECT_GRAPHQL_URL": GRAPHQL, **env})


def make_client(service):
    app = create_app(service)
    app.testing = True
    return app.test_client()


def signed_in(service):
    client = make_client(service)
    client.post("/welcome", data={"password": PASSWORD, "confirm": PASSWORD})
    return client


def configured(tmp_path, **env):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN, CONNECT_PRINTER_UUID="printer-1", CONNECT_ORGANIZATION_ID="org-1", **env)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "secret-refresh"))
    return service


def test_healthz_needs_no_login(tmp_path):
    response = make_client(make_service(tmp_path)).get("/healthz")
    assert response.status_code == 200
    assert response.json["status"] == "ok"


def test_first_visit_asks_for_a_password(tmp_path):
    client = make_client(make_service(tmp_path))
    assert "Choose an admin password" in client.get("/welcome").text
    assert client.get("/").headers["Location"].endswith("/welcome")
    assert client.get("/setup/connect").headers["Location"].endswith("/welcome")


@pytest.mark.parametrize("password,confirm", [("short", "short"), (PASSWORD, "something-else")])
def test_bad_new_password_is_refused(tmp_path, password, confirm):
    client = make_client(make_service(tmp_path))
    client.post("/welcome", data={"password": password, "confirm": confirm})
    assert not (tmp_path / "auth.json").exists() or "password_hash" not in json.loads((tmp_path / "auth.json").read_text())


def test_password_is_hashed_and_private(tmp_path):
    signed_in(make_service(tmp_path))
    path = tmp_path / "auth.json"
    assert PASSWORD not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_pages_need_a_login_after_the_password_is_set(tmp_path):
    service = make_service(tmp_path)
    signed_in(service)
    stranger = make_client(service)
    for path in ("/", "/logs", "/setup/connect"):
        assert stranger.get(path).headers["Location"].endswith("/login")
    assert stranger.post("/spool/create", data={"uuid": UUID}).headers["Location"].endswith("/login")
    assert stranger.post("/welcome", data={"password": "new-password-1", "confirm": "new-password-1"}).headers["Location"].endswith("/login")


def test_login_and_logout(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    service = make_service(tmp_path)
    signed_in(service)
    client = make_client(service)
    assert "Log in" in client.get("/login").text
    client.post("/login", data={"password": "wrong-password"})
    assert client.get("/logs").status_code == 302
    client.post("/login", data={"password": PASSWORD})
    assert client.get("/logs").status_code == 200
    client.post("/logout")
    assert client.get("/logs").status_code == 302


def test_post_from_another_site_is_blocked(tmp_path):
    service = make_service(tmp_path)
    client = signed_in(service)
    response = client.post("/logout", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_wizard_starts_at_spoolman_then_connect(tmp_path):
    client = signed_in(make_service(tmp_path))
    assert client.get("/").headers["Location"].endswith("/setup/spoolman")


@respx.mock(assert_all_called=False)
def test_spoolman_test_button_does_not_save(respx_mock, tmp_path):
    respx_mock.get(f"{SPOOLMAN}/api/v1/info").respond(json={"version": "0.27.0"})
    client = signed_in(make_service(tmp_path))
    page = client.post("/setup/spoolman", data={"spoolman_url": SPOOLMAN, "action": "test"}).text
    assert "Found Spoolman 0.27.0" in page
    assert "spoolman_url" not in read_config(tmp_path)


@respx.mock(assert_all_called=False)
def test_spoolman_save_goes_to_the_next_step(respx_mock, tmp_path):
    respx_mock.get(f"{SPOOLMAN}/api/v1/info").respond(json={"version": "0.27.0"})
    service = make_service(tmp_path)
    client = signed_in(service)
    response = client.post("/setup/spoolman", data={"spoolman_url": SPOOLMAN + "/", "action": "save"})
    assert response.headers["Location"].endswith("/setup/connect")
    assert read_config(tmp_path)["spoolman_url"] == SPOOLMAN
    assert service.spoolman is not None


@respx.mock(assert_all_called=False)
def test_ca_upload_must_be_a_certificate(respx_mock, tmp_path):
    from io import BytesIO

    client = signed_in(make_service(tmp_path))
    page = client.post(
        "/setup/spoolman",
        data={"spoolman_url": "https://s.test", "action": "save", "ca_file": (BytesIO(b"not a cert"), "x.crt")},
        content_type="multipart/form-data",
    ).text
    assert "PEM format" in page
    assert not (tmp_path / "spoolman-ca.crt").exists()


@respx.mock(assert_all_called=False)
def test_connect_login_is_stored_but_never_shown(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    client = signed_in(service)
    access = make_jwt(time.time() + 3600)
    response = client.post("/setup/connect", data={"login": json.dumps({"access": access, "refresh": "secret-refresh"})})
    assert response.headers["Location"].endswith("/setup/printers")
    assert service.store.load() == Tokens(access, "secret-refresh")
    assert stat.S_IMODE(os.stat(tmp_path / "tokens.json").st_mode) == 0o600
    for path in ("/setup/connect", "/setup/printers", "/logs"):
        page = client.get(path).text
        assert access not in page and "secret-refresh" not in page
    assert "a@example.com" in client.get("/setup/connect").text


@respx.mock(assert_all_called=False)
def test_rejected_connect_login_keeps_the_old_one(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).respond(500)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    old = Tokens(make_jwt(time.time() + 3600), "old-refresh")
    service.store.save(old)
    client = signed_in(service)
    page = client.post("/setup/connect", data={"login": json.dumps({"access": make_jwt(time.time() + 3600), "refresh": "new"})}).text
    assert "rejected the login" in page
    assert service.store.load() == old


def test_garbage_connect_login_is_refused(tmp_path):
    client = signed_in(make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN))
    assert "not recognized" in client.post("/setup/connect", data={"login": "tull"}).text
    assert not (tmp_path / "tokens.json").exists()


@respx.mock(assert_all_called=False)
def test_choosing_printers_saves_them(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "r"))
    client = signed_in(service)
    assert "Core" in client.get("/setup/printers").text
    client.post("/setup/printers", data={"printer": ["printer-1"], "location_printer-1": "Workshop"})
    saved = read_config(tmp_path)["printers"][0]
    assert (saved["uuid"], saved["organization_id"], saved["location"]) == ("printer-1", "org-1", "Workshop")
    assert service.settings.enabled_printers[0].location == "Workshop"


def test_mode_step_saves_choices(tmp_path):
    service = configured(tmp_path)
    client = signed_in(service)
    client.post("/setup/mode", data={"poll_interval": "30", "auto_create": "on"})
    config = read_config(tmp_path)
    assert (config["dry_run"], config["auto_create"], config["poll_interval"]) == (False, True, 30)
    assert service.settings.dry_run is False


@respx.mock(assert_all_called=False)
def test_dashboard_shows_a_linked_spool(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[{"id": 52, "location": "Core One", "extra": {"opt_instance_uuid": json.dumps(UUID)}}])
    service = configured(tmp_path)
    assert service.poll_once() is True
    page = signed_in(service).get("/").text
    assert "PC Blend Jet Black" in page
    assert f"{SPOOLMAN}/spool/show/52" in page
    assert "#212929" in page
    assert "secret-refresh" not in page


def unlinked_service(respx_mock, tmp_path, **env):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(
        json=[{"id": 52, "remaining_weight": 1000, "filament": {"name": "Blend", "material": "PC", "color_hex": "24292A", "vendor": {"name": "Prusament"}}, "extra": {"card_uids": json.dumps("ABC")}}]
    )
    service = configured(tmp_path, **env)
    assert service.poll_once() is True
    return service


@respx.mock(assert_all_called=False)
def test_dashboard_offers_a_link_for_an_unlinked_spool(respx_mock, tmp_path):
    page = signed_in(unlinked_service(respx_mock, tmp_path)).get("/").text
    assert "not linked to Spoolman" in page
    assert "Suggested" in page
    assert "#52 Prusament Blend" in page
    assert "Best match in Spoolman" in page
    assert "Confirm and link" in page
    assert "same vendor, same material" in page


@respx.mock(assert_all_called=False)
def test_dry_run_refuses_link_and_create(respx_mock, tmp_path):
    service = unlinked_service(respx_mock, tmp_path)
    client = signed_in(service)
    client.post("/spool/link", data={"uuid": UUID, "spool_id": "52"})
    client.post("/spool/create", data={"uuid": UUID})
    assert [c.request.method for c in respx_mock.calls if "spool" in str(c.request.url) and c.request.method != "GET"] == []


@respx.mock(assert_all_called=False)
def test_live_link_from_the_dashboard(respx_mock, tmp_path):
    service = unlinked_service(respx_mock, tmp_path, DRY_RUN="false")
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json={"id": 52, "extra": {"card_uids": json.dumps("ABC")}})
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    client = signed_in(service)
    client.post("/spool/link", data={"uuid": UUID, "spool_id": "52", "update_weights": "on"})
    body = json.loads(patch.calls[0].request.content)
    assert json.loads(body["extra"]["opt_instance_uuid"]) == UUID
    assert body["extra"]["card_uids"] == json.dumps("ABC")
    assert body["initial_weight"] == 959


@respx.mock(assert_all_called=False)
def test_two_printers_are_both_polled(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    (tmp_path / "config.json").write_text(json.dumps({"spoolman_url": SPOOLMAN, "printers": [
        {"uuid": "a", "organization_id": "o", "name": "A"},
        {"uuid": "b", "organization_id": "o", "name": "B", "enabled": False},
        {"uuid": "c", "organization_id": "o", "name": "C"},
    ]}))
    service = make_service(tmp_path)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "r"))
    assert service.poll_once() is True
    assert set(service.status) == {"a", "c"}


@respx.mock(assert_all_called=False)
def test_a_suggestion_writes_nothing_until_it_is_confirmed(respx_mock, tmp_path):
    service = unlinked_service(respx_mock, tmp_path, DRY_RUN="false")
    signed_in(service).get("/")
    assert [c.request.method for c in respx_mock.calls if "/spool" in str(c.request.url) and c.request.method != "GET"] == []
