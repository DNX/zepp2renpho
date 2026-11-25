"""Tests for the Zepp exporter.

Only the HTTP boundary is faked. Everything else runs on the fixtures in
`tests/fixtures/zepp/`, which mirror the record shape the reference clients
report (AlexxIT/SmartScaleConnect, maoziban/smart-body-scale-android#16).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from zepp2renpho.records import Member
from zepp2renpho.zepp import (
    DebugDump,
    RecordCollector,
    ZeppApiError,
    ZeppAuth,
    ZeppClient,
    apply_baby_flags,
    auth_from_login,
    baby_ids_from_records,
    check_business_code,
    collect_records,
    detect_baby,
    iter_record_pages,
    members_with_owner,
    owner_name_from_user_info,
    parse_members,
    parse_record,
    parse_summary,
    record_timezone,
    redact,
    resolve_api_host,
)

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"


def load(name: str) -> dict:
    """Read one fixture file."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    """The part of a `requests.Response` the client uses."""

    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    """Answers each request from a queue and records what was asked."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def make_client(responses: list[FakeResponse]) -> ZeppClient:
    """Build a client wired to a fake session."""
    auth = ZeppAuth(app_token="token", user_id="1234567890", api_host="api-mifit-de2.zepp.com")
    return ZeppClient(auth, session=FakeSession(responses))


# --- summary parsing --------------------------------------------------------


def test_parse_summary_accepts_a_dict():
    assert parse_summary({"weight": 64.7}) == {"weight": 64.7}


def test_parse_summary_accepts_a_json_string():
    assert parse_summary('{"weight": 8.42}') == {"weight": 8.42}


def test_parse_summary_accepts_a_base64_string():
    blob = base64.b64encode(b'{"weight": 70.5}').decode()

    assert parse_summary(blob) == {"weight": 70.5}


def test_parse_summary_of_an_empty_value_is_empty():
    assert parse_summary(None) == {}
    assert parse_summary("") == {}


def test_parse_summary_rejects_an_unreadable_blob():
    with pytest.raises(ZeppApiError):
        parse_summary("not base64 and not json !!")


# --- units and metric mapping ----------------------------------------------


def test_parse_record_maps_every_metric_with_its_unit():
    item = load("weight_page_1.json")["items"][0]

    record = parse_record(item, "-1")

    assert record.member_id == "-1"
    assert record.timestamp == 1731067200
    assert record.weight_kg == 64.7
    assert record.bmi == 21.8
    assert record.body_fat_pct == 17.01331
    assert record.water_pct == 56.92887
    assert record.bone_kg == 2.7305484
    assert record.protein_pct == 21.837502
    assert record.visceral_fat == 9.0
    assert record.height_cm == 172.0
    assert record.impedance == 451


def test_muscle_rate_is_kilograms_not_percent():
    """64.7 kg at 17.01331% fat minus 2.73 kg of bone is 50.96 kg of muscle."""
    item = load("weight_page_1.json")["items"][0]

    record = parse_record(item, "-1")

    assert record.muscle_kg == 50.961838
    assert record.muscle_kg < record.weight_kg


def test_metabolism_becomes_whole_kilocalories_and_muscle_age_becomes_body_age():
    item = load("weight_page_1.json")["items"][0]

    record = parse_record(item, "-1")

    assert record.bmr_kcal == 1358
    assert isinstance(record.bmr_kcal, int)
    assert record.body_age == 25


def test_a_metric_of_zero_means_not_measured():
    item = {"generatedTime": 1731067200, "summary": {"weight": 64.7, "fatRate": 0, "impedance": 0}}

    record = parse_record(item, "-1")

    assert record.body_fat_pct is None
    assert record.impedance is None


def test_the_whole_original_row_stays_in_raw():
    item = load("weight_page_1.json")["items"][0]

    record = parse_record(item, "-1")

    assert record.raw["deviceId"] == "AABBCCDDEEFF"
    assert record.raw["dataSource"] == 1


def test_a_record_without_its_own_member_id_keeps_the_queried_one():
    item = {"generatedTime": 1731067200, "summary": {"weight": 64.7}}

    record = parse_record(item, "87654321")

    assert record.member_id == "87654321"


# --- invalid rows -----------------------------------------------------------


def test_a_row_with_weight_zero_is_dropped():
    item = load("weight_page_1.json")["items"][2]

    assert parse_record(item, "-1") is None


def test_a_row_without_a_timestamp_is_dropped():
    assert parse_record({"summary": {"weight": 64.7}}, "-1") is None


def test_the_collector_keeps_every_weight_type_and_counts_invalid_rows():
    collector = RecordCollector()

    collector.add_all(load("weight_page_1.json")["items"], "-1")

    assert collector.stats.fetched == 3
    assert collector.stats.skipped_imported == 0
    assert collector.stats.invalid == 1
    assert len(collector.records) == 2


def test_a_row_copied_from_google_fit_is_not_exported():
    collector = RecordCollector()
    item = {
        "memberId": "-1",
        "generatedTime": 1790000000,
        "weightType": 7,
        "summary": {"weight": 88.0, "thirdAppName": "Fit", "thirdPackage": "com.google.android.apps.fitness"},
    }

    collector.add(item, "-1")

    assert collector.stats.skipped_imported == 1
    assert collector.records == []


# --- timezone ---------------------------------------------------------------


def test_a_named_timezone_is_kept():
    assert record_timezone({"timezone": "Europe/Rome"}, {}) == "Europe/Rome"


def test_a_small_numeric_offset_reads_as_minutes():
    assert record_timezone({"tz": 60}, {}) == "UTC+01:00"


def test_a_large_numeric_offset_reads_as_seconds():
    assert record_timezone({"tz": 7200}, {}) == "UTC+02:00"


def test_a_negative_offset_keeps_its_sign():
    assert record_timezone({"tz": -330}, {}) == "UTC-05:30"


def test_a_record_without_a_timezone_has_none():
    assert record_timezone({"generatedTime": 1}, {"weight": 64.7}) is None


# --- pagination -------------------------------------------------------------


def test_paging_follows_the_next_cursor_until_it_reaches_zero():
    pages = [load("weight_page_1.json"), load("weight_page_2.json")]
    asked: list[int] = []

    def fetch(to_time: int) -> dict:
        asked.append(to_time)
        return pages[len(asked) - 1]

    items = list(iter_record_pages(fetch, 1731100000))

    assert asked == [1731100000, 1730894400]
    assert len(items) == 5


def test_paging_stops_on_an_empty_page():
    items = list(iter_record_pages(lambda _: {"items": [], "next": 1730894400}, 1731100000))

    assert items == []


def test_paging_stops_when_the_cursor_does_not_move_backwards():
    page = {"items": [{"generatedTime": 1}], "next": 1731100000}

    items = list(iter_record_pages(lambda _: page, 1731100000))

    assert len(items) == 1


def test_paging_gives_up_instead_of_looping_forever():
    page = {"items": [{"generatedTime": 1}], "next": 10}

    with pytest.raises(ZeppApiError):
        list(iter_record_pages(lambda to_time: {**page, "next": to_time - 1}, 100, max_pages=3))


# --- deduplication ----------------------------------------------------------


def test_the_same_record_fetched_twice_is_written_once():
    collector = RecordCollector()
    first = load("weight_page_1.json")["items"][0]
    second = load("weight_page_2.json")["items"][0]

    collector.add(first, "-1")
    collector.add(second, "-1")

    assert len(collector.records) == 1
    assert collector.stats.duplicates == 1


def test_two_members_weighed_at_the_same_second_are_both_kept():
    collector = RecordCollector()
    owner = {"memberId": "-1", "generatedTime": 1731067200, "summary": {"weight": 64.7}}
    baby = {"memberId": "87654321", "generatedTime": 1731067200, "summary": {"weight": 8.42}}

    collector.add(owner, "-1")
    collector.add(baby, "-1")

    assert len(collector.records) == 2


def test_the_export_is_ordered_by_member_and_by_time():
    collector = RecordCollector()
    collector.add_all(load("weight_page_1.json")["items"], "-1")
    collector.add_all(load("weight_page_2.json")["items"], "-1")

    keys = [(record.member_id, record.timestamp) for record in collector.sorted_records()]

    assert keys == [("-1", 1709251200), ("-1", 1731067200), ("87654321", 1730980800)]


# --- members ----------------------------------------------------------------


def test_the_owner_is_added_in_front_with_the_id_minus_one():
    members = parse_members(load("family_members.json")["data"]["list"], "Daisy")

    assert members[0] == Member(member_id="-1", name="Daisy", is_baby=None, birthday=None)
    assert [member.member_id for member in members] == ["-1", "87654321", "87654322"]


def test_a_member_in_baby_weighing_mode_is_marked_as_a_baby():
    members = parse_members(load("family_members.json")["data"]["list"], "Daisy")

    assert members[1].name == "Bimbo"
    assert members[1].is_baby is True
    assert members[1].birthday == "2024-05-14"


def test_a_member_in_the_normal_mode_is_marked_as_an_adult():
    members = parse_members(load("family_members.json")["data"]["list"], "Daisy")

    assert members[2].is_baby is False


def test_an_unmarked_member_keeps_the_baby_flag_unknown():
    members = parse_members([{"fuid": 42, "nickname": "Nonna"}], "Daisy")

    assert members[1].is_baby is None


def test_a_record_marked_as_baby_weighing_names_its_member():
    items = [
        {"memberId": "99", "isBaby": True, "generatedTime": 1, "summary": {"weight": 8.0}},
        {"memberId": "-1", "generatedTime": 2, "summary": {"weight": 64.0}},
    ]

    assert baby_ids_from_records(items, "-1") == {"99"}


def test_a_record_without_a_baby_marker_decides_nothing():
    assert detect_baby({"memberId": "-1", "summary": {"weight": 64.0}}) is None


def test_the_baby_override_wins_over_an_unmarked_member():
    members = [Member(member_id="-1", name="Daisy"), Member(member_id="42", name="Nonna")]

    updated = apply_baby_flags(members, ["42"])

    assert updated[0].is_baby is None
    assert updated[1].is_baby is True


def test_the_owner_name_comes_from_the_user_profile():
    assert owner_name_from_user_info({"data": {"nickname": "Daisy"}}) == "Daisy"
    assert owner_name_from_user_info({"data": {}}) == "owner"


# --- login and hosts --------------------------------------------------------


def test_the_regional_host_comes_from_the_login_answer():
    assert resolve_api_host(load("login.json")) == "api-mifit-de2.zepp.com"


def test_an_account_without_a_host_mapping_keeps_the_generic_host():
    assert resolve_api_host({"token_info": {}}) == "api-mifit.zepp.com"


def test_the_credentials_come_out_of_the_login_answer():
    auth = auth_from_login(load("login.json"))

    assert auth.user_id == "1234567890"
    assert auth.app_token == "very-secret-app-token"
    assert auth.api_host == "api-mifit-de2.zepp.com"


def test_a_failed_login_is_reported():
    with pytest.raises(ZeppApiError):
        auth_from_login({"error_code": "0100", "result": "error"})


def test_the_debug_dump_hides_every_token(tmp_path):
    dump = DebugDump(tmp_path / "debug")

    path = dump.write("login", load("login.json"))

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["token_info"]["app_token"] == "[REDACTED]"
    assert written["token_info"]["login_token"] == "[REDACTED]"
    assert written["token_info"]["user_id"] == "1234567890"


def test_redaction_reaches_into_lists():
    assert redact([{"password": "hunter2"}]) == [{"password": "[REDACTED]"}]


# --- HTTP boundary ----------------------------------------------------------


def test_the_family_member_call_reads_the_list_out_of_the_answer():
    client = make_client([FakeResponse(load("family_members.json"))])

    entries = client.fetch_family_members()

    method, url, kwargs = client.session.calls[0]
    assert method == "POST"
    assert url.endswith("/huami.health.scale.familymember.get.json")
    assert kwargs["data"] == {"fuid": "all", "userid": "1234567890"}
    assert [entry["nickname"] for entry in entries] == ["Bimbo", "Partner"]


def test_the_weight_call_asks_the_member_route_with_the_cursor():
    client = make_client([FakeResponse(load("weight_page_2.json"))])

    client.fetch_weight_page("87654321", 1731100000)

    _, url, kwargs = client.session.calls[0]
    assert url.endswith("/users/1234567890/members/87654321/weightRecords")
    assert kwargs["params"] == {"limit": 200, "toTime": 1731100000}
    assert kwargs["headers"]["apptoken"] == "token"


def test_a_retired_profile_route_still_lists_the_owner():
    client = make_client([
        FakeResponse(load("family_members.json")),
        FakeResponse({"code": 404, "message": "routes not found"}),
    ])

    members = members_with_owner(client)

    assert members[0] == Member(member_id="-1", name="owner")
    assert [member.name for member in members[1:]] == ["Bimbo", "Partner"]


def test_an_expired_token_is_reported_as_such():
    client = make_client([FakeResponse({"message": "invalid token"}, status_code=401)])

    with pytest.raises(ZeppApiError, match="app token"):
        client.verify_token()


def test_an_error_code_inside_a_200_answer_is_an_error():
    with pytest.raises(ZeppApiError, match="routes not found"):
        check_business_code({"code": 404, "message": "routes not found"}, "/whatever")


def test_a_success_code_passes():
    check_business_code({"code": 1, "message": "success"}, "/whatever")
    check_business_code({"items": [], "next": 0}, "/whatever")


def test_every_member_is_fetched_and_the_overlap_is_dropped():
    responses = [
        FakeResponse(load("weight_page_1.json")),
        FakeResponse(load("weight_page_2.json")),
        FakeResponse({"items": load("weight_page_1.json")["items"][1:2], "next": 0}),
    ]
    client = make_client(responses)
    members = [Member(member_id="-1", name="Daisy"), Member(member_id="87654321", name="Bimbo")]

    collector, baby_ids = collect_records(client, members)

    assert baby_ids == set()
    assert collector.stats.duplicates == 2
    assert len(collector.records) == 3
