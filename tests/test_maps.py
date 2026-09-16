from copy import deepcopy

import pytest
from conftest import geocode_result

from voice_agent.agent.services.maps import AddressError, Maps

QUERY = "12 Oak St, Denver, 80202, USA"


@pytest.mark.parametrize(
    "response,reason",
    [
        ([], "no address"),
        ([geocode_result(), geocode_result()], "more than one"),
        ([{**geocode_result(), "partial_match": True}], "only part"),
        ([geocode_result(city="Boston")], "city does not match"),
        ([geocode_result(zip_code="80203")], "ZIP code does not match"),
        ([geocode_result(address="13 Oak St")], "different street address"),
        ([geocode_result(address="12 Pine St")], "different street address"),
    ],
)
def test_unresolved_or_conflicting_addresses_are_not_accepted(maps, response, reason):
    maps.responses[QUERY] = response
    with pytest.raises(AddressError, match=reason):
        maps.validate("12 Oak St", "Denver", "80202")
    assert "12 oak st, denver, 80202" not in maps.known


def test_street_abbreviation_uses_matching_components_and_is_cached(maps):
    response = deepcopy(geocode_result())
    response["address_components"][1]["long_name"] = "Oak Street"
    maps.responses[QUERY] = [response]
    place = maps.validate("12 Oak St", "Denver", "80202")
    assert place.address == "12 Oak Street"
    assert maps.locate("12 Oak Street, Denver, 80202") is place
    assert maps.locate("12 Oak St, Denver, 80202") is place
    assert maps.requests == [QUERY]


def test_zip_leading_zero_is_preserved(maps):
    maps.responses["12 Oak St, Boston, 02108, USA"] = [geocode_result(city="Boston", zip_code="02108")]
    assert maps.validate("12 Oak St", "Boston", "02108").zip_code == "02108"


def test_zip_plus_four_must_match_suffix(maps):
    response = geocode_result()
    response["address_components"].append({"long_name": "1234", "short_name": "1234", "types": ["postal_code_suffix"]})
    maps.responses["12 Oak St, Denver, 80202-1234, USA"] = [response]
    assert maps.validate("12 Oak St", "Denver", "80202-1234").zip_code == "80202-1234"


def test_postal_area_without_street_number_is_not_an_address(maps):
    response = geocode_result()
    response["address_components"] = response["address_components"][1:]
    maps.responses[QUERY] = [response]
    with pytest.raises(AddressError, match="could not locate the street number"):
        maps.validate("12 Oak St", "Denver", "80202")


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"status": "ZERO_RESULTS", "results": []}, "no address"),
        ({"status": "REQUEST_DENIED", "results": []}, "temporarily unavailable"),
        ({"successful": True}, "temporarily unavailable"),
    ],
)
def test_google_status_is_not_confused_with_missing_address(payload, reason):
    class Provider:
        def execute(self, slug, arguments):
            return {"response_data": payload}

    maps = Maps(Provider())
    with pytest.raises(AddressError, match=reason):
        maps.validate("12 Oak St", "Denver", "80202")


def test_malformed_provider_result_is_a_lookup_failure(maps):
    maps.responses[QUERY] = ["unexpected payload"]
    with pytest.raises(AddressError, match="temporarily unavailable"):
        maps.validate("12 Oak St", "Denver", "80202")
