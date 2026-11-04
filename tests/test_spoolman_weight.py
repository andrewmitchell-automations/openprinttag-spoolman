import json

import pytest
import respx

from tests.test_bridge import GRAPHQL, SPOOLMAN, UUID, mock_fields
from tests.test_web import configured, graphql, signed_in


def dashboard(respx_mock, tmp_path, remaining):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    spool = {"id": 52, "location": "Core One", "extra": {"opt_instance_uuid": json.dumps(UUID), "opt_consumed_weight": "0"}}
    if remaining is not None:
        spool["remaining_weight"] = remaining
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    service = configured(tmp_path, LOCATION_NAME="Core One")
    assert service.poll_once() is True
    return signed_in(service).get("/").text


@respx.mock(assert_all_called=False)
def test_a_linked_spool_shows_what_spoolman_has_left(respx_mock, tmp_path):
    # Connect reports 959 g left for the default test spool.
    page = dashboard(respx_mock, tmp_path, 955)
    assert "Spoolman: <strong>955 g</strong> left" in page
    assert "than Connect" not in page


@pytest.mark.parametrize("remaining,text", [(853, "106 g less than Connect"), (1000, "41 g more than Connect")])
@respx.mock(assert_all_called=False)
def test_a_difference_is_pointed_out(respx_mock, tmp_path, remaining, text):
    page = dashboard(respx_mock, tmp_path, remaining)
    assert text in page
    assert "Spoolman and Connect disagree" in page


@respx.mock(assert_all_called=False)
def test_nothing_is_shown_without_a_spoolman_weight(respx_mock, tmp_path):
    page = dashboard(respx_mock, tmp_path, None)
    assert "Spoolman:" not in page
