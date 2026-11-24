"""Tests for the RENPHO importer: payload shape, local time, plan, refusals.

Only the HTTP boundary is replaced. Every other function runs for real on the
fixture rows under `tests/fixtures/renpho/`.
"""
from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zepp2renpho.records import Member, Record
from zepp2renpho.renpho import (
    STORE_SCALE,
    MemberPlan,
    MissingTemplateError,
    Profile,
    RenphoApi,
    RenphoImportError,
    Row,
    borrowed_template,
    build_payload,
    build_payloads,
    build_template,
    classify,
    local_time,
    member_profile,
    resolve_pairs,
    template_from_row,
    upload_failure,
)
from zepp2renpho.renpho_cli import import_member

FIXTURES = Path(__file__).parent / "fixtures" / "renpho"
ROME = ZoneInfo("Europe/Rome")

ADULT = Profile("7001234", "Owner", is_baby=False, is_account_owner=True)
BABY = Profile("7005678", "Bimba Uno", is_baby=True, is_account_owner=False)


def fixture(name: str) -> dict:
    """Read one stored RENPHO reading."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def row(name: str, profile: Profile) -> Row:
    """Wrap a fixture reading as a stored row of one profile."""
    data = fixture(name)
    return Row(store=STORE_SCALE, table="measurements_info_6", timestamp=data["timeStamp"], data=data)


def adult_template():
    """Template taken from the adult fixture reading."""
    return template_from_row(ADULT, row("adult_row", ADULT))


def baby_template():
    """Template taken from the baby fixture reading."""
    return template_from_row(BABY, row("baby_row", BABY))


def adult_record(timestamp: int = 1760000000) -> Record:
    """A full Zepp reading of the account owner."""
    return Record(
        member_id="zepp-owner",
        timestamp=timestamp,
        weight_kg=80.4,
        bmi=25.4,
        body_fat_pct=20.8,
        water_pct=56.1,
        muscle_kg=60.5,
        bone_kg=3.2,
        bmr_kcal=1760,
        visceral_fat=7.0,
        protein_pct=17.1,
        body_age=35,
        impedance=498,
        height_cm=178.0,
    )


def baby_record(timestamp: int = 1760000000) -> Record:
    """A baby weighing from Zepp, with stray adult metrics that must not be sent."""
    return Record(
        member_id="zepp-baby-1",
        timestamp=timestamp,
        weight_kg=9.6,
        bmi=16.1,
        body_fat_pct=18.0,
    )


class TestPayloadMapping:
    """Record to RENPHO payload conversion against a profile template."""

    def test_adult_payload_carries_template_identity_and_metrics(self):
        payload = build_payload(adult_record(), adult_template(), ROME)

        assert payload["deviceType"] == "00038"
        assert payload["mac"] == "C8:3E:99:11:22:33"
        assert payload["bUserId"] == 7001234
        assert payload["userId"] == 7001234
        assert payload["displayModuleType"] == 0
        assert payload["isAuto"] == 2
        assert payload["method"] == 5
        assert payload["weight"] == 80.4
        assert payload["bmi"] == 25.4
        assert payload["bodyfat"] == 20.8
        assert payload["water"] == 56.1
        assert payload["sinew"] == 60.5
        assert payload["bone"] == 3.2
        assert payload["bmr"] == 1760
        assert payload["visfat"] == 7.0
        assert payload["protein"] == 17.1
        assert payload["bodyage"] == 35
        assert payload["resistance"] == 498

    def test_adult_payload_omits_fields_zepp_cannot_supply(self):
        payload = build_payload(adult_record(), adult_template(), ROME)

        assert "muscle" not in payload
        assert "fatFreeWeight" not in payload
        assert "subfat" not in payload
        assert "id" not in payload
        assert "createdAt" not in payload

    def test_baby_payload_carries_weight_only(self):
        payload = build_payload(baby_record(), baby_template(), ROME)

        assert payload["weight"] == 9.6
        assert payload["categoryType"] == 1
        assert payload["bUserId"] == 7005678
        assert "bmi" not in payload
        assert "bodyfat" not in payload
        assert "headValue" not in payload

    def test_record_without_weight_is_rejected(self):
        broken = Record(member_id="zepp-owner", timestamp=1760000000, weight_kg=0.0)

        with pytest.raises(RenphoImportError, match="no usable weight"):
            build_payload(broken, adult_template(), ROME)


class TestLocalTime:
    """Historical daylight saving time of the Europe/Rome readings."""

    def test_winter_reading_is_central_european_time(self):
        # 2025-01-15 08:30:00 +01:00
        created_at, offset = local_time(1736926200, ROME)

        assert created_at == "2025-01-15 08:30:00"
        assert offset == "GMT+01:00"

    def test_summer_reading_is_central_european_summer_time(self):
        # 2025-07-15 08:30:00 +02:00
        created_at, offset = local_time(1752561000, ROME)

        assert created_at == "2025-07-15 08:30:00"
        assert offset == "GMT+02:00"

    def test_record_timezone_wins_over_the_default(self):
        record = Record(
            member_id="zepp-owner",
            timestamp=1752561000,
            weight_kg=80.4,
            timezone="UTC",
        )

        payload = build_payload(record, adult_template(), ROME)

        assert payload["localCreatedAt"] == "2025-07-15 06:30:00"
        assert payload["timeZone"] == "GMT+00:00"


class TestPlan:
    """Classification of exported readings against the RENPHO history."""

    def stored(self, payload: dict, **overrides) -> Row:
        """Build a stored row that looks like an uploaded payload."""
        data = {**fixture("adult_row"), **payload, **overrides}
        return Row(STORE_SCALE, "measurements_info_6", payload["timeStamp"], data)

    def test_counts_match_missing_and_conflict(self):
        template = adult_template()
        records = [adult_record(1_760_000_000), adult_record(1_760_086_400), adult_record(1_760_172_800)]
        payloads = build_payloads(records, template, ROME)
        history = {
            payloads[0]["timeStamp"]: [self.stored(payloads[0])],
            payloads[1]["timeStamp"]: [self.stored(payloads[1], weight=77.0)],
        }

        plan = classify(payloads, history, ADULT, template)

        assert len(plan.matched) == 1
        assert len(plan.conflicts) == 1
        assert plan.conflicts[0][1] == ["weight"]
        assert [p["timeStamp"] for p in plan.pending] == [1_760_172_800]

    def test_other_scale_at_the_same_second_is_a_conflict(self):
        template = adult_template()
        payloads = build_payloads([adult_record()], template, ROME)
        history = {payloads[0]["timeStamp"]: [self.stored(payloads[0], mac="AA:BB:CC:DD:EE:FF")]}

        plan = classify(payloads, history, ADULT, template)

        assert not plan.matched
        assert plan.conflicts[0][1] == ["device type, history store, or scale address differs"]

    def test_two_exported_readings_at_one_second_are_rejected(self):
        heavier = Record(member_id="zepp-owner", timestamp=1_760_000_000, weight_kg=99.0)

        with pytest.raises(RenphoImportError, match="share timestamp"):
            build_payloads([adult_record(), heavier], adult_template(), ROME)


class TestMapping:
    """Pairing exported Zepp members with RENPHO profiles."""

    def members(self) -> list[Member]:
        return [
            Member(member_id="zepp-owner", name="Owner", is_baby=False),
            Member(member_id="zepp-baby-1", name="Bimba Uno", is_baby=True),
            Member(member_id="zepp-baby-2", name="Bimba Due", is_baby=True),
        ]

    def test_unmapped_and_unknown_members_are_skipped(self):
        mapping = {"zepp-owner": "7001234", "zepp-baby-1": "", "zepp-baby-2": "7000000"}

        pairs, skipped = resolve_pairs(mapping, self.members(), [ADULT, BABY])

        assert [(m.member_id, p.profile_id) for m, p in pairs] == [("zepp-owner", "7001234")]
        assert [m.member_id for m, _ in skipped] == ["zepp-baby-1", "zepp-baby-2"]
        assert "mapping file" in skipped[0][1]
        assert "not on this account" in skipped[1][1]

    def test_family_entry_becomes_a_baby_profile(self):
        entries = json.loads((FIXTURES / "family_list.json").read_text(encoding="utf-8"))

        profiles = [member_profile(entry) for entry in entries]

        assert profiles[0] == Profile("7005678", "Bimba Uno", True, False)
        assert profiles[1] == Profile("7009999", "Partner", False, False)


class TestTemplate:
    """Template discovery and the errors it raises."""

    def test_missing_template_names_the_person_and_the_baby_mode(self):
        with pytest.raises(MissingTemplateError, match="Baby Weighing mode"):
            build_template(BABY, [])

    def test_newest_reading_becomes_the_template(self):
        old = row("adult_row", ADULT)
        newer_data = dict(old.data, timeStamp=old.timestamp + 86400, deviceType="00051")
        newer = Row(STORE_SCALE, old.table, newer_data["timeStamp"], newer_data)

        template = build_template(ADULT, [old, newer])

        assert template.device_type == "00051"

    def test_reading_of_another_profile_is_refused(self):
        stranger = Row(STORE_SCALE, "measurements_info_6", 1, dict(fixture("adult_row"), bUserId=42))

        with pytest.raises(RenphoImportError, match="belongs to profile 42"):
            build_template(ADULT, [stranger])


class TestUploadGuards:
    """Refusals and error codes that protect the account."""

    def plan_without_match(self) -> MemberPlan:
        template = adult_template()
        payloads = build_payloads([adult_record()], template, ROME)
        return MemberPlan(profile=ADULT, template=template, pending=payloads)

    def test_import_refuses_before_a_verified_test_upload(self):
        with pytest.raises(RenphoImportError, match="renpho-import test"):
            import_member(api=None, plan=self.plan_without_match(), zone=ROME, limit=None)

    def test_import_refuses_while_conflicts_remain(self):
        plan = self.plan_without_match()
        plan.conflicts.append((plan.pending[0], ["weight"]))

        with pytest.raises(RenphoImportError, match="conflict"):
            import_member(api=None, plan=plan, zone=ROME, limit=None)

    def test_rate_limit_and_decryption_codes_are_explained(self):
        assert "rate-limited" in upload_failure({"code": 429, "msg": "too many"}, None)
        assert "-114" in upload_failure({"code": "-114", "msg": ""}, None)
        assert upload_failure({"code": 0, "msg": "success"}, None) is None

    def test_error_message_never_shows_the_token(self):
        message = upload_failure({"code": 500, "msg": "bad token secret-abc"}, "secret-abc")

        assert "secret-abc" not in message
        assert "[redacted]" in message


class FakeTransport:
    """Records the single upload call instead of reaching the network."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, endpoint, body, *, headers=None, session=None):
        self.calls.append((endpoint, body, headers or {}))
        return {"code": 0, "msg": "success"}


class FakeClient:
    """Minimal stand-in for RenphoClient around the upload boundary."""

    def __init__(self):
        self.token = "secret-token"
        self.user_id = 7001234
        self.user_info = {"accountName": "Owner", "categoryType": 0}
        self._transport = FakeTransport()


class TestUploadCall:
    """The one network call that writes."""

    def test_upload_sends_an_encrypted_array_with_app_headers(self):
        from renpho.crypto import decrypt_response

        client = FakeClient()
        api = RenphoApi(client)
        payload = build_payload(adult_record(), adult_template(), ROME)

        api.upload([payload], ROME)

        endpoint, body, headers = client._transport.calls[0]
        assert endpoint == "RenphoHealth/scale/uploadMeasureData/"
        assert decrypt_response(body["encryptData"]) == [payload]
        assert headers["token"] == "secret-token"
        assert headers["userId"] == "7001234"
        assert headers["zoneId"] == "Europe/Rome"
        assert headers["timeZone"] in ("+1", "+2")

    def test_server_rejection_stops_the_run(self, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(
            client._transport, "post", lambda *a, **k: {"code": 429, "msg": "slow down"}
        )

        with pytest.raises(RenphoImportError, match="rate-limited"):
            RenphoApi(client).upload([{"timeStamp": 1}], ROME)


class TestBorrowedTemplate:
    """A profile without readings borrows the owner's scale identity."""

    def test_pet_payload_carries_its_own_person_fields_and_weight_only(self):
        pet = member_profile(
            {"id": 7005678, "accountName": "Pet One", "categoryType": 1, "gender": 1, "birthday": "2019-03-01"}
        )

        payload = build_payload(baby_record(), borrowed_template(pet, adult_template()), ROME)

        assert payload["bUserId"] == 7005678
        assert payload["userId"] == 7001234
        assert payload["deviceType"] == "00038"
        assert payload["mac"] == "C8:3E:99:11:22:33"
        assert payload["birthday"] == "2019-03-01"
        assert payload["categoryType"] == 1
        assert "height" not in payload
        assert payload["weight"] == 9.6
        assert "bmi" not in payload and "bodyfat" not in payload
