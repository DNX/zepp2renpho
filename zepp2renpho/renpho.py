"""RENPHO Health side: read profiles and history, build upload rows, send them.

RENPHO has no import feature. This module drives the same private cloud API the
Android app uses, through the `renpho-py` client (AES-128-ECB encrypted bodies).

The upload route and payload shape come from the only public, proven upload
reference, `dchapman992000/renpho-history-import`, which was verified on an
ES-CS20M (deviceType "00007"). Our destination scale is an ES-32MD, so no device
value is hardcoded here. For every destination profile the newest reading that
already exists in RENPHO is used as a template: its device identity, its history
store and the set of metric fields it carries are copied, and only the metrics
the template already carries are sent. A profile without any reading needs one
weigh-in on the new scale first (for a baby: Baby Weighing mode), or the
`--borrow-owner-template` option, which sends weight only with the owner's scale identity.

Units follow `records.py`: kilograms, percentages 0-100, UTC epoch seconds.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from renpho.constants import APP_VERSION, ENDPOINTS, PLATFORM
from renpho.crypto import decrypt_response, encrypt_empty_object, encrypt_request

from .records import Member, Record

# Trailing slash kept: the proven upload reference posts to this exact path.
UPLOAD_ENDPOINT = "RenphoHealth/scale/uploadMeasureData/"
FAMILY_ENDPOINT = ENDPOINTS["family"]

STORE_SCALE = "scale"
STORE_COMPOSITION = "composition"

DEFAULT_TIMEZONE = "Europe/Rome"
BATCH_SIZE = 10
BATCH_PAUSE_SECONDS = 5.0

# RENPHO payload field -> Record attribute. `sinew` is muscle mass in kg;
# `muscle` (skeletal muscle percentage) and `fatFreeWeight` have no Zepp source.
METRIC_FIELDS = {
    "weight": "weight_kg",
    "bmi": "bmi",
    "bodyfat": "body_fat_pct",
    "water": "water_pct",
    "sinew": "muscle_kg",
    "bone": "bone_kg",
    "protein": "protein_pct",
    "visfat": "visceral_fat",
    "bmr": "bmr_kcal",
    "bodyage": "body_age",
    "resistance": "impedance",
}
INTEGER_FIELDS = frozenset({"bmr", "bodyage", "resistance"})

# Metric fields a template row can carry. The extra names are read to learn what
# the destination scale stores, never written, because Zepp supplies no value.
TEMPLATE_METRICS = frozenset(METRIC_FIELDS) | {
    "muscle",
    "fatFreeWeight",
    "subfat",
    "smmMass",
    "headValue",
}
BABY_METRICS = frozenset({"weight"})

# Device and profile metadata copied verbatim from the template row. `id`,
# timestamps and metrics are deliberately absent: RENPHO assigns the first,
# the record supplies the rest.
IDENTITY_FIELDS = (
    "deviceType",
    "mac",
    "bUserId",
    "userId",
    "subUserId",
    "displayModuleType",
    "isAuto",
    "method",
    "categoryType",
    "personType",
    "gender",
    "height",
    "heightUnit",
    "weightUnit",
    "birthday",
    "scaleType",
    "scaleName",
    "internalModel",
)
REQUIRED_TEMPLATE_FIELDS = ("deviceType", "mac")

# Person fields of a borrowed template. They come from the destination profile,
# never from the owner's reading, so a pet does not inherit the owner's height.
PROFILE_FIELDS = ("categoryType", "personType", "gender", "height", "heightUnit", "birthday")

SUCCESS_CODES = frozenset({0, "0", 101, "101", 200, "200", 20000, "20000"})
COMPARE_TOLERANCE = 0.055
MILLISECOND_THRESHOLD = 100_000_000_000


class RenphoImportError(RuntimeError):
    """A condition the user has to resolve before the import can continue."""


class MissingTemplateError(RenphoImportError):
    """A destination profile has no reading to copy device metadata from."""


@dataclass(frozen=True)
class Profile:
    """One person RENPHO can store readings for."""

    profile_id: str
    name: str
    is_baby: bool
    is_account_owner: bool
    details: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Row:
    """One existing RENPHO reading plus where it was read from."""

    store: str
    table: str
    timestamp: int
    data: dict[str, Any]


@dataclass(frozen=True)
class Template:
    """Destination metadata copied from a profile's newest existing reading."""

    profile_id: str
    is_baby: bool
    device_type: str
    mac: str
    store: str
    table: str
    metrics: frozenset[str]
    identity: dict[str, Any]


@dataclass
class MemberPlan:
    """What an import would do for one destination profile."""

    profile: Profile
    template: Template
    matched: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)


# ----- time -----


def local_time(stamp: int, zone: ZoneInfo) -> tuple[str, str]:
    """Return the local time text and the GMT offset label RENPHO stores."""
    instant = datetime.fromtimestamp(stamp, zone)
    offset = instant.strftime("%z")
    return instant.strftime("%Y-%m-%d %H:%M:%S"), f"GMT{offset[:3]}:{offset[3:]}"


def record_zone(record: Record, default: ZoneInfo) -> ZoneInfo:
    """Use the zone the reading was taken in, or the configured default."""
    name = (record.timezone or "").strip()
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default


def header_offset(zone: ZoneInfo) -> str:
    """Build the short offset header the app sends, for example `+1` or `+5:30`."""
    offset = datetime.now(zone).strftime("%z")
    minutes = "" if offset[3:] == "00" else ":" + offset[3:]
    return f"{offset[0]}{int(offset[1:3])}{minutes}"


# ----- template -----


def normalize_mac(value: Any) -> str:
    """Compare scale addresses without separators or case."""
    return str(value or "").replace(":", "").replace("-", "").strip().lower()


def is_number(value: Any) -> bool:
    """True for a real numeric metric value, excluding booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def template_owner(data: dict[str, Any]) -> str:
    """Return the profile a reading belongs to, new field first."""
    return str(data.get("bUserId") or data.get("userId") or "")


def build_template(profile: Profile, rows: Sequence[Row]) -> Template:
    """Copy device metadata and the carried metric set from the newest reading."""
    if not rows:
        raise MissingTemplateError(
            f"RENPHO profile {profile.profile_id} ({profile.name}) has no reading to copy. "
            "Weigh this person once on the new scale"
            + (" in Baby Weighing mode" if profile.is_baby else "")
            + ", let the app sync, then run this command again."
        )
    newest = max(rows, key=lambda row: row.timestamp)
    return template_from_row(profile, newest)


def template_from_row(profile: Profile, row: Row) -> Template:
    """Turn one existing reading into the template used for every upload."""
    data = row.data
    missing = [name for name in REQUIRED_TEMPLATE_FIELDS if not data.get(name)]
    if missing:
        raise RenphoImportError(
            f"The newest reading of profile {profile.profile_id} lacks {', '.join(missing)}. "
            "Weigh this person again on the new scale and retry."
        )
    if template_owner(data) != profile.profile_id:
        raise RenphoImportError(
            f"The template reading belongs to profile {template_owner(data)}, "
            f"not {profile.profile_id}. No upload sent."
        )
    metrics = frozenset(name for name in TEMPLATE_METRICS if is_number(data.get(name)))
    if "weight" not in metrics:
        raise RenphoImportError(
            f"The newest reading of profile {profile.profile_id} carries no weight. "
            "Weigh this person again on the new scale and retry."
        )
    identity = {name: data[name] for name in IDENTITY_FIELDS if data.get(name) is not None}
    return Template(
        profile_id=profile.profile_id,
        is_baby=profile.is_baby,
        device_type=str(data["deviceType"]),
        mac=str(data["mac"]),
        store=row.store,
        table=row.table,
        metrics=metrics,
        identity=identity,
    )


def allowed_metrics(template: Template) -> frozenset[str]:
    """Baby readings carry weight only; adults carry whatever the scale stores."""
    if template.is_baby:
        return template.metrics & BABY_METRICS
    return template.metrics


def borrowed_template(profile: Profile, owner: Template) -> Template:
    """Build a weight-only template for a profile that has no reading yet.

    The scale identity comes from the owner's template and the person fields
    from the profile entry. This shape is unproven, so only `test` can confirm it.
    """
    identity = {
        name: value
        for name, value in owner.identity.items()
        if name not in PROFILE_FIELDS and name not in ("bUserId", "subUserId")
    }
    identity.update(
        {name: profile.details[name] for name in PROFILE_FIELDS if profile.details.get(name) is not None}
    )
    as_number = isinstance(owner.identity.get("bUserId"), int)
    identity["bUserId"] = int(profile.profile_id) if as_number else profile.profile_id
    return Template(
        profile_id=profile.profile_id,
        is_baby=profile.is_baby,
        device_type=owner.device_type,
        mac=owner.mac,
        store=owner.store,
        table=owner.table,
        metrics=frozenset({"weight"}),
        identity=identity,
    )


# ----- payload -----


def metric_values(record: Record, template: Template) -> dict[str, Any]:
    """Map a record onto the metric fields the destination template carries."""
    fields = allowed_metrics(template)
    values: dict[str, Any] = {}
    for name, attribute in METRIC_FIELDS.items():
        value = getattr(record, attribute) if name in fields else None
        if value is None:
            continue
        values[name] = round(value) if name in INTEGER_FIELDS else round(float(value), 4)
    weight = values.get("weight")
    if not weight or not math.isfinite(weight) or weight <= 0:
        raise RenphoImportError(
            f"Record at {record.timestamp} for member {record.member_id} has no usable weight."
        )
    return values


def build_payload(record: Record, template: Template, zone: ZoneInfo) -> dict[str, Any]:
    """Build the upload row for one record, in the destination profile's shape."""
    stamp = int(record.timestamp)
    created_at, offset_label = local_time(stamp, record_zone(record, zone))
    payload = dict(template.identity)
    payload.update(metric_values(record, template))
    payload.update(timeStamp=stamp, localCreatedAt=created_at, timeZone=offset_label)
    return payload


def build_payloads(
    records: Iterable[Record], template: Template, zone: ZoneInfo
) -> list[dict[str, Any]]:
    """Build one payload per record, oldest first, rejecting duplicate timestamps."""
    payloads: dict[int, dict[str, Any]] = {}
    for record in records:
        payload = build_payload(record, template, zone)
        stamp = payload["timeStamp"]
        existing = payloads.get(stamp)
        if existing is not None and existing != payload:
            raise RenphoImportError(
                f"Two different exported readings share timestamp {payload['localCreatedAt']} "
                f"for profile {template.profile_id}. Fix the export before uploading."
            )
        payloads[stamp] = payload
    return [payloads[stamp] for stamp in sorted(payloads)]


# ----- comparison -----


def differences(payload: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """List the metric fields the stored reading does not reproduce."""
    names = sorted(set(METRIC_FIELDS) & payload.keys())
    return [
        name
        for name in names
        if not is_number(actual.get(name))
        or not math.isclose(float(actual[name]), float(payload[name]), abs_tol=COMPARE_TOLERANCE)
    ]


def row_matches(payload: dict[str, Any], row: Row, template: Template) -> bool:
    """True when a stored reading is the one this payload would create."""
    data = row.data
    return (
        row.store == template.store
        and str(data.get("deviceType")) == template.device_type
        and normalize_mac(data.get("mac")) == normalize_mac(template.mac)
        and not differences(payload, data)
    )


def best_differences(payload: dict[str, Any], rows: Sequence[Row]) -> list[str]:
    """Describe the closest stored reading at a conflicting timestamp."""
    if not rows:
        return ["no stored reading"]
    fields = min((differences(payload, row.data) for row in rows), key=len)
    return fields or ["device type, history store, or scale address differs"]


def classify(
    payloads: Sequence[dict[str, Any]],
    history: dict[int, list[Row]],
    profile: Profile,
    template: Template,
) -> MemberPlan:
    """Split payloads into already matching, missing, and conflicting readings."""
    plan = MemberPlan(profile=profile, template=template)
    for payload in payloads:
        rows = history.get(payload["timeStamp"], [])
        if not rows:
            plan.pending.append(payload)
        elif any(row_matches(payload, row, template) for row in rows):
            plan.matched.append(payload)
        else:
            plan.conflicts.append((payload, best_differences(payload, rows)))
    return plan


# ----- mapping -----


def mapping_skeleton(members: Iterable[Member]) -> dict[str, str]:
    """Build an empty Zepp-member to RENPHO-profile mapping."""
    return {member.member_id: "" for member in members}


def load_mapping(path: Path) -> dict[str, str]:
    """Read `mapping.json` and reject anything that is not id-to-id text."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RenphoImportError(f"{path} must contain a JSON object of Zepp id to RENPHO id.")
    bad = [key for key, value in data.items() if not isinstance(value, (str, int))]
    if bad:
        raise RenphoImportError(f"{path} has non-text values for: {', '.join(sorted(bad))}")
    return {str(key): str(value).strip() for key, value in data.items()}


def resolve_pairs(
    mapping: dict[str, str], members: Sequence[Member], profiles: Sequence[Profile]
) -> tuple[list[tuple[Member, Profile]], list[tuple[Member, str]]]:
    """Pair exported members with destination profiles and report the skipped ones."""
    by_id = {profile.profile_id: profile for profile in profiles}
    pairs: list[tuple[Member, Profile]] = []
    skipped: list[tuple[Member, str]] = []
    for member in members:
        target = mapping.get(member.member_id, "")
        if not target:
            skipped.append((member, "no RENPHO profile in the mapping file"))
        elif target not in by_id:
            skipped.append((member, f"RENPHO profile {target} is not on this account"))
        else:
            pairs.append((member, by_id[target]))
    return pairs, skipped


# ----- API -----


def redact(value: Any, token: str | None) -> Any:
    """Replace secrets before a debug dump reaches the disk."""
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key in ("token", "password", "encryptedPassword")
            else redact(item, token)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, str) and token and token in value:
        return value.replace(token, "[redacted]")
    return value


def upload_failure(result: dict[str, Any], token: str | None) -> str | None:
    """Return a user-facing message when RENPHO rejected an upload."""
    code = str(result.get("code", "unknown"))
    message = str(result.get("msg") or result.get("message") or "")
    if result.get("code") in SUCCESS_CODES or message.lower() == "success":
        return None
    if code == "429":
        return "RENPHO rate-limited the upload (code 429). Run plan again before resuming."
    if code == "-114":
        return (
            f"RENPHO returned -114 (request decryption failed) with appVersion {APP_VERSION}. "
            "Check RENPHO History for the test reading before retrying."
        )
    detail = " ".join(message.split())[:240]
    if token and detail:
        detail = detail.replace(token, "[redacted]")
    shown = code if code.lstrip("-").isdigit() else "unknown"
    suffix = f". Server message: {detail}" if detail else ". RENPHO supplied no error message."
    return f"RENPHO rejected the upload (code {shown}){suffix}"


class RenphoApi:
    """Thin wrapper over `renpho-py`: one small function per network call."""

    def __init__(self, client, debug_dir: Path | None = None):
        self.client = client
        self.debug_dir = debug_dir
        self._device_info: dict[str, Any] | None = None

    @property
    def account_id(self) -> str:
        """The signed-in RENPHO account id, as text."""
        return str(self.client.user_id)

    def _dump(self, name: str, data: Any) -> None:
        """Write a raw API response to the debug folder, with secrets removed."""
        if self.debug_dir is None:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        path = self.debug_dir / f"{int(time.time() * 1000)}-{name}.json"
        body = redact(data, getattr(self.client, "token", None))
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False, default=str), "utf-8")

    def login(self, expected_account_id: str | None = None) -> None:
        """Sign in and refuse a different account than the one requested."""
        data = self.client.login(expected_user_id=expected_account_id)
        self._dump("login", data)

    def device_info(self) -> dict[str, Any]:
        """Read scale metadata once per run."""
        if self._device_info is None:
            self._device_info = self.client.get_device_info()
            self._dump("device-info", self._device_info)
        return self._device_info

    def family_members(self) -> list[dict[str, Any]]:
        """List the sub-users of the account, baby profiles included."""
        result = self.client._post(FAMILY_ENDPOINT, encrypt_empty_object())
        if not result.get("data"):
            self._dump("family-empty", result)
            return []
        data = decrypt_response(result["data"])
        self._dump("family", data)
        members = data.get("list", data) if isinstance(data, dict) else data
        return [item for item in members if isinstance(item, dict)] if isinstance(members, list) else []

    def profiles(self) -> list[Profile]:
        """Return the account owner first, then every other known profile."""
        owner = owner_profile(self.client.user_info or {}, self.account_id)
        found = {owner.profile_id: owner}
        for item in self.family_members():
            profile = member_profile(item)
            if profile and profile.profile_id not in found:
                found[profile.profile_id] = profile
        for profile_id in self._metadata_profile_ids():
            found.setdefault(profile_id, Profile(profile_id, "(unnamed)", False, False))
        return [owner] + [p for pid, p in sorted(found.items()) if pid != owner.profile_id]

    def _metadata_profile_ids(self) -> list[str]:
        """Profile ids the scale metadata advertises, including unnamed ones."""
        ids: set[str] = set()
        for scale in self.device_info().get("scale", []):
            ids.update(str(uid) for uid in scale.get("userIds", []) if uid)
        return sorted(ids)

    def tables_for(self, profile_id: str) -> list[str]:
        """Find the measurement tables that hold a profile's readings."""
        tables = {
            scale["tableName"]
            for scale in self.device_info().get("scale", [])
            if scale.get("tableName")
            and profile_id in {str(uid) for uid in scale.get("userIds", [])}
        }
        shard = self.client.measurement_table_for(profile_id)
        if shard:
            tables.add(shard)
        if not tables:
            tables.update(self.client.discover_user_tables(profile_id))
        return sorted(tables)

    def history(self, profile_id: str) -> dict[int, list[Row]]:
        """Read every stored reading of one profile, keyed by UTC epoch second."""
        history: dict[int, list[Row]] = {}
        for table in self.tables_for(profile_id):
            for store, rows in self._fetch_table(table, profile_id):
                self._dump(f"history-{profile_id}-{table}-{store}", rows)
                for row in owned_rows(rows, store, table, profile_id):
                    history.setdefault(row.timestamp, []).append(row)
        return history

    def _fetch_table(self, table: str, profile_id: str) -> list[tuple[str, list[dict]]]:
        """Read both history stores of one table; either one can hold the data."""
        return [
            (STORE_SCALE, self.client.get_measurements(table, profile_id)),
            (STORE_COMPOSITION, self.client.get_body_composition_measurements(table, profile_id)),
        ]

    def upload(self, payloads: Sequence[dict[str, Any]], zone: ZoneInfo) -> None:
        """Send one batch of readings and raise on any non-success answer."""
        headers = {
            "token": self.client.token,
            "userId": self.account_id,
            "appVersion": APP_VERSION,
            "platform": PLATFORM,
            "Content-Type": "application/json",
            "timeZone": header_offset(zone),
            "zoneId": zone.key,
        }
        result = self.client._transport.post(
            UPLOAD_ENDPOINT, encrypt_request(list(payloads)), headers=headers
        )
        self._dump("upload-result", result)
        failure = upload_failure(result, self.client.token)
        if failure:
            raise RenphoImportError(failure)


def owned_rows(rows: list[dict], store: str, table: str, profile_id: str) -> list[Row]:
    """Keep the rows of one profile and normalise millisecond timestamps."""
    kept = []
    for data in rows:
        stamp = data.get("timeStamp")
        if template_owner(data) != profile_id or not isinstance(stamp, (int, float)):
            continue
        stamp = int(stamp)
        if abs(stamp) >= MILLISECOND_THRESHOLD:
            stamp //= 1000
        kept.append(Row(store=store, table=table, timestamp=stamp, data=data))
    return kept


def owner_profile(login_info: dict[str, Any], account_id: str) -> Profile:
    """Build the account owner's profile from the login answer."""
    name = str(login_info.get("accountName") or login_info.get("email") or "account owner")
    return Profile(account_id, name, is_baby=is_baby_user(login_info), is_account_owner=True)


def member_profile(item: dict[str, Any]) -> Profile | None:
    """Build a sub-user profile from one family-member entry."""
    profile_id = item.get("id") or item.get("userId") or item.get("bUserId")
    if profile_id in (None, ""):
        return None
    name = str(item.get("accountName") or item.get("nickname") or item.get("name") or "(unnamed)")
    return Profile(
        str(profile_id), name, is_baby=is_baby_user(item), is_account_owner=False, details=item
    )


def is_baby_user(item: dict[str, Any]) -> bool:
    """RENPHO marks a baby profile with categoryType 1."""
    return str(item.get("categoryType", "")) == "1"
