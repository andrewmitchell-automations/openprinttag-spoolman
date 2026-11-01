import base64
import json
import os
import stat
import time

import httpx
import pytest
import respx

from bridge.config import ConfigError, PrinterConfig, Settings, load_settings, update_config
from bridge.connect import ConnectAuthError, ConnectClient, TokenStore, Tokens, jwt_expiry, parse_login_json
from bridge.spoolman import SpoolmanClient, extra_value, tls_verify
from bridge.sync import (
    Bridge,
    State,
    color_hex,
    consumption_step,
    index_spools,
    parse_loaded_spools,
    rank_spools_for_link,
)

SPOOLMAN = "https://spoolman.test"
GRAPHQL = "https://connect.test/graphql"
ACCOUNT = "https://account.test"
UUID = "2ed54568-7913-5aa0-9965-77da40c0af29"
MATERIAL_UUID = "517f24cc-16a4-5091-8325-5f6873e9dfcb"


def make_jwt(exp: float) -> str:
    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': exp})}.sig"


def settings(tmp_path, dry_run=False) -> Settings:
    return Settings(
        spoolman_url=SPOOLMAN,
        dry_run=dry_run,
        data_dir=tmp_path,
        account_url=ACCOUNT,
        account_client_id="client-1",
        graphql_url=GRAPHQL,
    )


PRINTER = PrinterConfig(uuid="printer-1", organization_id="org-1", name="Core", location="Core One")


def printer(consumed=0, uuid=UUID, from_nfc=True):
    return {
        "__typename": "Printer",
        "id": "printer-1",
        "name": "Core",
        "state": "IDLE",
        "fff": {
            "tools": [
                {
                    "id": "0",
                    "materialPackageInstance": {
                        "__typename": "FFFToolMaterialPackageInstance",
                        "uuid": uuid,
                        "actualNettoFullWeight": 959,
                        "isFromNfc": from_nfc,
                        "dynamicData": {"consumedWeight": consumed},
                        "package": {
                            "nominalNettoFullWeight": 1000,
                            "filamentDiameter": 1.75,
                            "container": {"emptyWeight": 277},
                            "material": {
                                "uuid": MATERIAL_UUID,
                                "name": "PC Blend Jet Black",
                                "abbreviation": "PC",
                                "brand": {"name": "Prusament"},
                                "primaryColor": {"colorRgba": "#212929"},
                                "type": {"abbreviation": "PC"},
                                "properties": {"density": 1.22},
                            },
                        },
                    },
                }
            ]
        },
    }


class FakeConnect:
    def __init__(self, data):
        self.data = data

    def fetch_printer(self, organization_id, printer_uuid):
        return self.data


def existing_spool(location="Core One", extra=None):
    return {
        "id": 42,
        "location": location,
        "archived": False,
        "extra": extra if extra is not None else {"opt_instance_uuid": json.dumps(UUID)},
    }


def mock_fields(mock, present=True):
    fields = [{"key": "opt_instance_uuid"}] if present else []
    filament_fields = [{"key": "opt_material_uuid"}] if present else []
    mock.get(f"{SPOOLMAN}/api/v1/field/spool").respond(json=fields)
    mock.get(f"{SPOOLMAN}/api/v1/field/filament").respond(json=filament_fields)


class Poller:
    def __init__(self, bridge):
        self.bridge = bridge

    def poll(self):
        return self.bridge.poll(PRINTER)


def make_bridge(tmp_path, data, dry_run=False, auto_create=True):
    s = settings(tmp_path, dry_run)
    return Poller(Bridge(FakeConnect(data), SpoolmanClient(s, httpx.Client()), State(s.state_path, dry_run), auto_create))


def writes(mock):
    return [c.request for c in mock.calls if c.request.method != "GET"]


# Consumption


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        (None, 0, ("baseline", 0.0)),
        (None, 50, ("baseline", 0.0)),
        (10, 10, ("none", 0.0)),
        (10, 12.5, ("use", 2.5)),
        (10, 4, ("rebase", 0.0)),
    ],
)
def test_consumption_step(previous, current, expected):
    assert consumption_step(previous, current) == expected


@respx.mock(assert_all_called=False)
def test_first_sighting_of_existing_spool_only_sets_baseline(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    make_bridge(tmp_path, printer(consumed=100)).poll()
    assert writes(respx_mock) == []
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 100


@respx.mock(assert_all_called=False)
def test_increase_deducts_the_delta_and_updates_state(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    use = respx_mock.put(f"{SPOOLMAN}/api/v1/spool/42/use").respond(json={})
    make_bridge(tmp_path, printer(consumed=12.5)).poll()
    assert json.loads(use.calls[0].request.content) == {"use_weight": 2.5}
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 12.5


@respx.mock(assert_all_called=False)
def test_state_is_unchanged_when_the_deduction_fails(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    respx_mock.put(f"{SPOOLMAN}/api/v1/spool/42/use").respond(500, text="boom")
    with pytest.raises(Exception):
        make_bridge(tmp_path, printer(consumed=12.5)).poll()
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 10


@respx.mock(assert_all_called=False)
def test_decrease_rebases_without_a_write(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    make_bridge(tmp_path, printer(consumed=4)).poll()
    assert writes(respx_mock) == []
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 4


# Mapping and parsing


def test_extra_values_are_json_decoded():
    assert extra_value({"extra": {"opt_instance_uuid": json.dumps(UUID)}}, "opt_instance_uuid") == UUID
    assert extra_value({"extra": {}}, "opt_instance_uuid") is None


def test_index_uses_only_the_instance_uuid_and_ignores_card_uids():
    spools = [
        {"id": 1, "extra": {"card_uids": json.dumps(UUID)}},
        {"id": 2, "extra": {"opt_instance_uuid": json.dumps(UUID)}},
        {"id": 3, "extra": {}},
    ]
    assert index_spools(spools)[UUID]["id"] == 2
    assert len(index_spools(spools)) == 1


@pytest.mark.parametrize(
    "rgba,expected",
    [("#212929", "212929"), ("#212929ff", "212929"), ("bad", None), (None, None)],
)
def test_color_hex(rgba, expected):
    assert color_hex(rgba) == expected


def test_empty_tool_gives_no_spool():
    data = {"fff": {"tools": [{"id": "0", "materialPackageInstance": {"__typename": "ToolMaterialPackageInstanceEmpty", "reason": "EMPTY"}}]}}
    assert parse_loaded_spools(data) == []


def test_parse_loaded_spool():
    spool = parse_loaded_spools(printer())[0]
    assert (spool.uuid, spool.brand, spool.material_name) == (UUID, "Prusament", "PC Blend Jet Black")
    assert (spool.actual_weight, spool.empty_weight, spool.nominal_weight) == (959, 277, 1000)
    assert (spool.color_hex, spool.density, spool.filament_diameter) == ("212929", 1.22, 1.75)


# Creating and updating in Spoolman


@respx.mock(assert_all_called=False)
def test_unknown_instance_creates_vendor_filament_and_spool(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    vendor = respx_mock.post(f"{SPOOLMAN}/api/v1/vendor").respond(json={"id": 7})
    filament = respx_mock.post(f"{SPOOLMAN}/api/v1/filament").respond(json={"id": 8})
    spool = respx_mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 9})
    make_bridge(tmp_path, printer(consumed=30)).poll()

    assert json.loads(vendor.calls[0].request.content) == {"name": "Prusament"}
    filament_body = json.loads(filament.calls[0].request.content)
    assert filament_body["vendor_id"] == 7
    assert filament_body["color_hex"] == "212929"
    assert filament_body["weight"] == 1000
    assert filament_body["spool_weight"] == 277
    assert filament_body["extra"] == {"opt_material_uuid": json.dumps(MATERIAL_UUID)}
    spool_body = json.loads(spool.calls[0].request.content)
    assert spool_body["filament_id"] == 8
    assert spool_body["initial_weight"] == 959
    assert spool_body["spool_weight"] == 277
    assert spool_body["used_weight"] == 30
    assert spool_body["location"] == "Core One"
    assert spool_body["extra"] == {"opt_instance_uuid": json.dumps(UUID)}
    assert "card_uids" not in spool_body["extra"]
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 30


@respx.mock(assert_all_called=False)
def test_existing_vendor_and_filament_are_reused(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[{"id": 7, "name": "prusament"}])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(
        json=[{"id": 8, "name": "PC Blend Jet Black", "vendor": {"id": 7}}]
    )
    spool = respx_mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 9})
    make_bridge(tmp_path, printer()).poll()
    assert len(writes(respx_mock)) == 1
    assert json.loads(spool.calls[0].request.content)["filament_id"] == 8


@respx.mock(assert_all_called=False)
def test_location_update_does_not_send_extra_fields(respx_mock, tmp_path):
    mock_fields(respx_mock)
    spool = existing_spool(location="Kjeller", extra={"opt_instance_uuid": json.dumps(UUID), "card_uids": json.dumps("ABC")})
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    make_bridge(tmp_path, printer()).poll()
    assert json.loads(patch.calls[0].request.content) == {"location": "Core One"}


@respx.mock(assert_all_called=False)
def test_missing_extra_fields_are_created(respx_mock, tmp_path):
    mock_fields(respx_mock, present=False)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    spool_field = respx_mock.post(f"{SPOOLMAN}/api/v1/field/spool/opt_instance_uuid").respond(json=[])
    filament_field = respx_mock.post(f"{SPOOLMAN}/api/v1/field/filament/opt_material_uuid").respond(json=[])
    make_bridge(tmp_path, printer()).poll()
    assert json.loads(spool_field.calls[0].request.content)["field_type"] == "text"
    assert filament_field.called


@respx.mock(assert_all_called=False)
def test_non_nfc_instance_is_skipped(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    make_bridge(tmp_path, printer(from_nfc=False)).poll()
    assert writes(respx_mock) == []


# Dry run


@respx.mock(assert_all_called=False)
def test_dry_run_sends_no_writes_and_leaves_no_state(respx_mock, tmp_path, caplog):
    caplog.set_level("INFO")
    mock_fields(respx_mock, present=False)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    make_bridge(tmp_path, printer(), dry_run=True).poll()
    assert writes(respx_mock) == []
    for expected in (
        "POST /field/spool/opt_instance_uuid",
        "POST /field/filament/opt_material_uuid",
        "POST /vendor",
        "POST /filament",
        "POST /spool",
    ):
        assert f"DRY-RUN would call {expected} " in caplog.text


@respx.mock(assert_all_called=False)
def test_dry_run_logs_a_deduction_only_once(respx_mock, tmp_path, caplog):
    caplog.set_level("INFO")
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    bridge = make_bridge(tmp_path, printer(consumed=15), dry_run=True)
    bridge.poll()
    bridge.poll()
    assert writes(respx_mock) == []
    assert caplog.text.count("DRY-RUN would call PUT /spool/42/use") == 1
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 10


# Connect tokens


def connect_client(tmp_path, access, refresh="refresh-1"):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens(access, refresh))
    return ConnectClient(settings(tmp_path), store, httpx.Client()), store


def graphql_ok(data=None):
    return {"json": {"data": {"printer": {"printer": data or printer()}}}}


def test_jwt_expiry():
    assert jwt_expiry(make_jwt(123.5)) == 123.5
    assert jwt_expiry("not-a-jwt") is None


def test_token_file_is_private(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("a", "b"))
    assert stat.S_IMODE(os.stat(tmp_path / "tokens.json").st_mode) == 0o600


@respx.mock(assert_all_called=False)
def test_valid_token_is_used_without_refreshing(respx_mock, tmp_path):
    access = make_jwt(time.time() + 3600)
    client, _ = connect_client(tmp_path, access)
    query = respx_mock.post(GRAPHQL).respond(**graphql_ok())
    refresh = respx_mock.post(f"{ACCOUNT}/o/token/").respond(500)
    assert client.fetch_printer("org-1", "printer-1")["name"] == "Core"
    assert query.calls[0].request.headers["authorization"] == f"Bearer {access}"
    assert not refresh.called


@respx.mock(assert_all_called=False)
def test_expiring_token_is_refreshed_and_rotation_is_saved(respx_mock, tmp_path):
    new_access = make_jwt(time.time() + 3600)
    client, store = connect_client(tmp_path, make_jwt(time.time() + 10))
    refresh = respx_mock.post(f"{ACCOUNT}/o/token/").respond(
        json={"access_token": new_access, "refresh_token": "refresh-2"}
    )
    query = respx_mock.post(GRAPHQL).respond(**graphql_ok())
    client.fetch_printer("org-1", "printer-1")
    form = refresh.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in form and "refresh_token=refresh-1" in form and "client_id=client-1" in form
    assert query.calls[0].request.headers["authorization"] == f"Bearer {new_access}"
    assert store.load() == Tokens(new_access, "refresh-2")


@respx.mock(assert_all_called=False)
def test_rejected_refresh_keeps_the_stored_tokens(respx_mock, tmp_path):
    old_access = make_jwt(time.time() + 10)
    client, store = connect_client(tmp_path, old_access)
    respx_mock.post(f"{ACCOUNT}/o/token/").respond(400, json={"error": "invalid_grant"})
    with pytest.raises(ConnectAuthError, match="invalid_grant"):
        client.fetch_printer("org-1", "printer-1")
    assert store.load() == Tokens(old_access, "refresh-1")


@respx.mock(assert_all_called=False)
def test_401_triggers_one_refresh_and_retry(respx_mock, tmp_path):
    new_access = make_jwt(time.time() + 3600)
    client, _ = connect_client(tmp_path, make_jwt(time.time() + 3600))
    respx_mock.post(f"{ACCOUNT}/o/token/").respond(json={"access_token": new_access})
    query = respx_mock.post(GRAPHQL).mock(
        side_effect=[httpx.Response(401, json={"errors": []}), httpx.Response(200, **graphql_ok())]
    )
    assert client.fetch_printer("org-1", "printer-1")["name"] == "Core"
    assert query.call_count == 2


def test_expired_token_without_refresh_token_asks_for_a_new_login(tmp_path):
    client, _ = connect_client(tmp_path, make_jwt(time.time() - 10), refresh="")
    with pytest.raises(ConnectAuthError, match="log in to Connect again"):
        client.fetch_printer("org-1", "printer-1")


# Settings


def test_dry_run_and_no_auto_create_are_the_defaults(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": "https://s/"})
    assert s.dry_run is True
    assert s.auto_create is False
    assert s.spoolman_verify_tls is True
    assert s.spoolman_url == "https://s"
    assert s.printers == ()


def test_old_env_file_still_describes_one_printer(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "CONNECT_PRINTER_UUID": "p", "CONNECT_ORGANIZATION_ID": "o", "LOCATION_NAME": "Shelf", "DRY_RUN": "false"})
    assert s.printers == (PrinterConfig(uuid="p", organization_id="o", location="Shelf"),)
    assert s.dry_run is False


def test_config_file_wins_over_env(tmp_path):
    update_config(tmp_path, {"dry_run": False, "spoolman_url": "http://saved", "printers": [{"uuid": "a", "organization_id": "b", "name": "Core"}]})
    s = load_settings({"DATA_DIR": str(tmp_path), "DRY_RUN": "true", "SPOOLMAN_URL": "http://env", "POLL_INTERVAL": "30", "CONNECT_PRINTER_UUID": "p", "CONNECT_ORGANIZATION_ID": "o"})
    assert s.dry_run is False
    assert s.spoolman_url == "http://saved"
    assert s.poll_interval == 30
    assert s.printers == (PrinterConfig(uuid="a", organization_id="b", name="Core", location="Core"),)


def test_broken_config_file_is_a_config_error(tmp_path):
    (tmp_path / "config.json").write_text("{nope")
    with pytest.raises(ConfigError, match="config.json"):
        load_settings({"DATA_DIR": str(tmp_path)})


def test_missing_ca_bundle_is_a_config_error(tmp_path):
    s = Settings(spoolman_url=SPOOLMAN, spoolman_ca_bundle=str(tmp_path / "missing.crt"))
    with pytest.raises(ConfigError, match="SPOOLMAN_CA_BUNDLE"):
        tls_verify(s)


def test_plain_http_does_not_need_the_ca_bundle(tmp_path):
    s = Settings(spoolman_url="http://spoolman.test:7912", spoolman_ca_bundle=str(tmp_path / "missing.crt"))
    assert tls_verify(s) is True


def test_login_json_is_parsed():
    assert parse_login_json('{"access": "a", "refresh": "r"}') == ("a", "r")
    assert parse_login_json('{"access": "a", "refresh": null}') == ("a", "")
    assert parse_login_json('"just-a-token"') is None
    assert parse_login_json("just-a-token") is None
    assert parse_login_json('{"access": null, "refresh": "r"}') is None


# Dry run to live


@respx.mock(assert_all_called=False)
def test_going_live_deducts_what_was_used_during_the_dry_run(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    make_bridge(tmp_path, printer(consumed=5), dry_run=True).poll()
    assert writes(respx_mock) == []
    use = respx_mock.put(f"{SPOOLMAN}/api/v1/spool/42/use").respond(json={})
    make_bridge(tmp_path, printer(consumed=20)).poll()
    assert json.loads(use.calls[0].request.content) == {"use_weight": 15}
    make_bridge(tmp_path, printer(consumed=20)).poll()
    assert use.call_count == 1


# Linking


@respx.mock(assert_all_called=False)
def test_unknown_spool_waits_for_a_choice_without_auto_create(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    result = make_bridge(tmp_path, printer(), auto_create=False).poll()
    assert writes(respx_mock) == []
    assert result["tools"][0]["spool_id"] is None


def linker(tmp_path):
    return make_bridge(tmp_path, printer(consumed=30), auto_create=False).bridge


@respx.mock(assert_all_called=False)
def test_link_keeps_other_extra_fields_and_updates_weights(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json={"id": 52, "extra": {"card_uids": json.dumps("ABC")}})
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    loaded = parse_loaded_spools(printer(consumed=30))[0]
    linker(tmp_path).link_spool(loaded, 52, "Core One", update_weights=True)
    body = json.loads(patch.calls[0].request.content)
    assert body["extra"] == {"card_uids": json.dumps("ABC"), "opt_instance_uuid": json.dumps(UUID)}
    assert (body["initial_weight"], body["spool_weight"], body["used_weight"]) == (959, 277, 30)
    assert body["location"] == "Core One"
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 30


@respx.mock(assert_all_called=False)
def test_link_without_weights_leaves_them_alone(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json={"id": 52, "extra": {}})
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    linker(tmp_path).link_spool(parse_loaded_spools(printer())[0], 52, "", update_weights=False)
    assert set(json.loads(patch.calls[0].request.content)) == {"extra"}


@respx.mock(assert_all_called=False)
def test_link_refuses_a_spool_linked_to_another_instance(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json={"id": 52, "extra": {"opt_instance_uuid": json.dumps("other")}})
    with pytest.raises(Exception, match="already linked"):
        linker(tmp_path).link_spool(parse_loaded_spools(printer())[0], 52, "", update_weights=False)
    assert writes(respx_mock) == []


def test_link_candidates_put_matches_first_and_skip_linked_and_archived():
    def spool(id, vendor, material, color, **extra):
        return {"id": id, "remaining_weight": 500, "filament": {"name": "x", "material": material, "color_hex": color, "vendor": {"name": vendor}}, **extra}

    spools = [
        spool(1, "eSun", "PLA", "FF0000"),
        spool(2, "Prusament", "PLA", "FF0000"),
        spool(3, "prusament", "PC", "24292A"),
        spool(4, "Prusament", "PC", "212929", archived=True),
        spool(5, "Prusament", "PC", "212929", extra={"opt_instance_uuid": json.dumps("taken")}),
    ]
    ranked = rank_spools_for_link(parse_loaded_spools(printer())[0], spools)
    assert [s["id"] for s in ranked] == [3, 2, 1]
    assert [s["suggested"] for s in ranked] == [True, True, False]
