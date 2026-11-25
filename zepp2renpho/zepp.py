"""Unofficial client for the Zepp (Huami / Amazfit) cloud scale API.

Zepp publishes no documented API. Everything here comes from these reference
implementations, and every assumption is marked in the code:

* argrento/huami-token         - email + password login, regional hosts
* micw/hacking-mifit-api       - the two-step login, documented field by field
* bentasker/zepp_to_influxdb   - the same login plus `apptoken` header use
* m4ary/zepp-health-cli        - GET /users/{id}/members/-1/weightRecords
* AlexxIT/SmartScaleConnect    - record shape, `next` paging, family members
* maoziban/smart-body-scale-android#16 - record and summary field names

Member ids
----------
The account owner is the Zepp "main user" and its member id is the literal
string ``-1``: the scale API addresses the owner as ``members/-1`` and stamps
``memberId: "-1"`` on the owner's records. Family members use their numeric
``fuid`` from ``huami.health.scale.familymember.get.json``.

Units
-----
The ``summary`` object uses the device units, not the field names:

* ``weight``, ``boneMass``, ``standBodyWeight`` - kilograms
* ``muscleRate``        - KILOGRAMS, in spite of the name. The Mi Body
  Composition Scale 2 sample in SmartScaleConnect reads weight 64.7,
  fatRate 17.01, boneMass 2.73, muscleRate 50.96, and
  64.7 * (1 - 0.1701) - 2.73 = 50.96, so the value is fat free mass minus
  bone mass in kg.
* ``fatRate``, ``bodyWaterRate``, ``proteinRatio`` - percent, 0-100
* ``metabolism``        - kcal per day
* ``height``            - centimetres
* ``muscleAge``         - years, the "body age" the app shows
* ``impedance``         - ohm
* ``visceralFat``       - index, not a unit

A metric of 0 means "not measured": the Zepp app omits those fields when it
writes a record, so this module maps 0 to ``None``.
"""
from __future__ import annotations

import base64
import binascii
import json
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import requests

from .records import Member, Record

OWNER_MEMBER_ID = "-1"
"""Zepp member id of the account owner (the "main user")."""

DEFAULT_API_HOST = "api-mifit.zepp.com"
"""Host used when the login response names no regional host. EU accounts are
usually redirected to `api-mifit-de2.zepp.com`."""

TOKEN_URL = "https://api-user.huami.com/registrations/{email}/tokens"
LOGIN_URL = "https://account.huami.com/v2/client/login"

PAGE_LIMIT = 200
"""Largest page the weightRecords endpoint accepts (SmartScaleConnect)."""

MAX_PAGES = 1000
"""Guard against a server that never stops handing out a `next` cursor."""

REDACTED = "[REDACTED]"
SECRET_KEYS = frozenset(
    {
        "access",
        "app_token",
        "apptoken",
        "code",
        "login_token",
        "password",
        "refresh",
        "refresh_token",
        "access_token",
        "token",
    }
)

IMPORTED_MARKER_KEY = "thirdPackage"
"""Zepp sets this summary key on a row it copied from another app, for example
Google Fit (`weightType` 7). Such a row can be a RENPHO reading that came back
through Health Connect, so it is never exported."""

BABY_FLAG_KEYS = ("isBaby", "is_baby", "babyMode", "baby_mode", "baby")
"""Keys that, when truthy, mark a member or a record as baby weighing."""

BABY_MEASURE_MODE = 1
MEASURE_MODE_KEYS = ("measure_mode", "measureMode")
"""Family member weighing mode. 1 is the baby / hug mode of the Zepp app."""

MAX_OFFSET_MINUTES = 840
"""+14:00, the largest real UTC offset. Above it a numeric `tz` is seconds."""


class ZeppApiError(RuntimeError):
    """The Zepp API answered with an error, or with something unusable."""


@dataclass(frozen=True)
class ZeppAuth:
    """Everything a data call needs: the app token, the user id and the host."""

    app_token: str
    user_id: str
    api_host: str = DEFAULT_API_HOST


@dataclass
class ExportStats:
    """Counters the CLI prints after an export."""

    fetched: int = 0
    invalid: int = 0
    duplicates: int = 0
    skipped_imported: int = 0


def redact(payload: Any) -> Any:
    """Copy a response with every credential value replaced by a placeholder."""
    if isinstance(payload, dict):
        return {
            key: REDACTED if key.lower() in SECRET_KEYS else redact(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


class DebugDump:
    """Writes raw API responses to a folder so field mappings can be fixed."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._index = 0

    def write(self, label: str, payload: Any) -> Path:
        """Write one redacted response and return the file it went to."""
        self._index += 1
        path = self.directory / f"{self._index:03d}-{label}.json"
        text = json.dumps(redact(payload), ensure_ascii=False, indent=2)
        path.write_text(text + "\n", encoding="utf-8")
        return path


def _to_float(value: Any) -> float | None:
    """Return a float, or None when the API left the field empty or unusable."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> float | None:
    """Return a float above zero. Zepp writes 0 for a metric it did not measure."""
    number = _to_float(value)
    if number is None or number <= 0:
        return None
    return number


def _positive_int(value: Any) -> int | None:
    """Return a whole number above zero, or None."""
    number = _positive_float(value)
    if number is None:
        return None
    return round(number)


SUMMARY_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "bmi": ("bmi", _positive_float),
    "body_fat_pct": ("fatRate", _positive_float),
    "water_pct": ("bodyWaterRate", _positive_float),
    "muscle_kg": ("muscleRate", _positive_float),
    "bone_kg": ("boneMass", _positive_float),
    "bmr_kcal": ("metabolism", _positive_int),
    "visceral_fat": ("visceralFat", _positive_float),
    "protein_pct": ("proteinRatio", _positive_float),
    "body_age": ("muscleAge", _positive_int),
    "impedance": ("impedance", _positive_int),
    "height_cm": ("height", _positive_float),
}
"""Record field -> (Zepp summary field, converter)."""


def parse_summary(value: Any) -> dict[str, Any]:
    """Return the record summary as a dict.

    The API sends it as an object, as a JSON string, or - on the band
    endpoints - as base64 of that JSON string.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    text = value.strip()
    if not text.startswith("{"):
        text = _decode_base64(text)
    return json.loads(text)


def _decode_base64(text: str) -> str:
    """Decode a base64 summary blob, or raise with the offending text."""
    try:
        return base64.b64decode(text).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ZeppApiError(f"Cannot read the record summary: {text[:60]!r}") from error


def format_utc_offset(offset_minutes: int) -> str:
    """Format a UTC offset in minutes as `UTC+HH:MM`."""
    sign = "-" if offset_minutes < 0 else "+"
    minutes = abs(offset_minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def record_timezone(item: dict[str, Any], summary: dict[str, Any]) -> str | None:
    """Return the timezone a record carries, if it carries one.

    A text value is a zone name and is kept as is. A number is a UTC offset,
    in minutes when it fits a real offset and in seconds otherwise.
    """
    for key in ("timezone", "timeZone"):
        name = item.get(key) or summary.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    offset = item.get("tz", summary.get("tz"))
    if offset is None or isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return None
    minutes = int(offset) if abs(offset) <= MAX_OFFSET_MINUTES else int(offset) // 60
    return format_utc_offset(minutes)


def _body_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    """Map the Zepp summary fields onto the Record metric fields."""
    return {
        name: convert(summary.get(key)) for name, (key, convert) in SUMMARY_FIELDS.items()
    }


def record_member_id(item: dict[str, Any], queried_member_id: str) -> str:
    """Return the member a record belongs to, falling back to the queried one."""
    value = item.get("memberId")
    if value is None or value == "":
        return str(queried_member_id)
    return str(value)


def parse_record(item: dict[str, Any], queried_member_id: str) -> Record | None:
    """Build a Record from one weightRecords item.

    Returns None for a row with no usable weight or no timestamp: Zepp keeps
    deleted and partial rows in the history.
    """
    summary = parse_summary(item.get("summary"))
    weight = _positive_float(summary.get("weight"))
    timestamp = _positive_int(item.get("generatedTime"))
    if weight is None or timestamp is None:
        return None
    return Record(
        member_id=record_member_id(item, queried_member_id),
        timestamp=timestamp,
        weight_kg=weight,
        timezone=record_timezone(item, summary),
        raw=item,
        **_body_metrics(summary),
    )


def _marks_baby(payload: dict[str, Any]) -> bool | None:
    """Read a baby-weighing marker from a member or record payload."""
    for key in BABY_FLAG_KEYS:
        if key in payload:
            return bool(payload[key])
    for key in MEASURE_MODE_KEYS:
        if key in payload:
            return _to_float(payload[key]) == BABY_MEASURE_MODE
    return None


def detect_baby(item: dict[str, Any]) -> bool | None:
    """Return the baby flag of a member or record, or None when unmarked."""
    marker = _marks_baby(item)
    if marker is not None:
        return marker
    summary = item.get("summary")
    if isinstance(summary, dict):
        return _marks_baby(summary)
    return None


def baby_ids_from_records(items: Iterable[dict[str, Any]], queried_member_id: str) -> set[str]:
    """Return the ids of members whose raw records are marked as baby weighing."""
    return {
        record_member_id(item, queried_member_id)
        for item in items
        if detect_baby(item) is True
    }


def parse_members(payload: Iterable[dict[str, Any]], owner_name: str) -> list[Member]:
    """Build the member list from `familymember.get.json`, owner first.

    The family endpoint lists the relatives only, so the owner is prepended
    with the fixed id `-1` unless the account already reports it.
    """
    members = [
        Member(
            member_id=str(entry.get("fuid")),
            name=str(entry.get("nickname") or f"member {entry.get('fuid')}"),
            is_baby=detect_baby(entry),
            birthday=_member_birthday(entry),
        )
        for entry in payload
        if entry.get("fuid") is not None
    ]
    if any(member.member_id == OWNER_MEMBER_ID for member in members):
        return members
    return [Member(member_id=OWNER_MEMBER_ID, name=owner_name)] + members


def _member_birthday(entry: dict[str, Any]) -> str | None:
    """Return the member birthday. Zepp misspells the field as `brithday`."""
    for key in ("birthday", "brithday"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def apply_baby_flags(members: Iterable[Member], baby_ids: Iterable[str]) -> list[Member]:
    """Force `is_baby` on the listed members, leaving the others untouched."""
    forced = {str(member_id) for member_id in baby_ids}
    return [
        replace(member, is_baby=True) if member.member_id in forced else member
        for member in members
    ]


def owner_name_from_user_info(payload: dict[str, Any]) -> str:
    """Pick a display name out of `huami.health.getUserInfo.json`."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("nickname", "nickName", "userName", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "owner"


class RecordCollector:
    """Turns raw weightRecords items into Records, without duplicates."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.stats = ExportStats()
        self._seen: set[tuple[str, int]] = set()

    def add(self, item: dict[str, Any], queried_member_id: str) -> bool:
        """Add one item and report whether it produced a new record."""
        self.stats.fetched += 1
        if is_imported(item):
            self.stats.skipped_imported += 1
            return False
        record = parse_record(item, queried_member_id)
        if record is None:
            self.stats.invalid += 1
            return False
        key = (record.member_id, record.timestamp)
        if key in self._seen:
            self.stats.duplicates += 1
            return False
        self._seen.add(key)
        self.records.append(record)
        return True

    def add_all(self, items: Iterable[dict[str, Any]], queried_member_id: str) -> None:
        """Add every item of one page or one member query."""
        for item in items:
            self.add(item, queried_member_id)

    def sorted_records(self) -> list[Record]:
        """Return the records ordered by member and by time."""
        return sorted(self.records, key=lambda record: (record.member_id, record.timestamp))


def iter_record_pages(
    fetch: Callable[[int], dict[str, Any]],
    start_time: int,
    *,
    max_pages: int = MAX_PAGES,
) -> Iterator[dict[str, Any]]:
    """Yield raw records, walking the `next` cursor back to the first record.

    `fetch(to_time)` returns one `{"items": [...], "next": <epoch seconds>}`
    page. The walk stops on an empty page, on a cursor of 0 or less, and on a
    cursor that does not move backwards.
    """
    to_time = start_time
    for _ in range(max_pages):
        page = fetch(to_time)
        items = page.get("items") or []
        yield from items
        next_time = int(_to_float(page.get("next")) or 0)
        if not items or next_time <= 0 or next_time >= to_time:
            return
        to_time = next_time
    raise ZeppApiError(f"weightRecords did not end after {max_pages} pages")


def request_access_token(
    email: str,
    password: str,
    session: requests.Session,
) -> tuple[str, str]:
    """Step 1 of the login: swap email and password for an access token.

    The server answers with a redirect whose query string carries `access`
    and `country_code`. Nothing is printed: the password stays in the form.
    """
    url = TOKEN_URL.format(email=urllib.parse.quote(email, safe=""))
    data = {
        "state": "REDIRECTION",
        "client_id": "HuaMi",
        "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html",
        "token": "access",
        "password": password,
    }
    response = session.post(url, data=data, allow_redirects=False, timeout=30)
    response.raise_for_status()
    location = response.headers.get("location", "")
    return _read_redirect(location)


def _read_redirect(location: str) -> tuple[str, str]:
    """Pull the access token and the country code out of the redirect URL."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    access = query.get("access", [""])[0]
    country = query.get("country_code", ["US"])[0]
    if not access:
        raise ZeppApiError("Login did not return an access token. Check email and password.")
    return access, country


def exchange_access_token(
    access_token: str,
    country_code: str,
    session: requests.Session,
) -> dict[str, Any]:
    """Step 2 of the login: swap the access token for an app token.

    The answer also names the regional hosts of this account in `domains`.
    """
    data = {
        "app_name": "com.xiaomi.hm.health",
        "app_version": "6.3.5",
        "allow_registration": "false",
        "dn": "api-user.huami.com,api-mifit.huami.com,app-analytics.huami.com,api-mifit.zepp.com",
        "device_id": "02:00:00:00:00:00",
        "device_model": "android_phone",
        "third_name": "huami",
        "grant_type": "access_token",
        "country_code": country_code,
        "code": access_token,
    }
    response = session.post(LOGIN_URL, data=data, allow_redirects=False, timeout=30)
    response.raise_for_status()
    return response.json()


def resolve_api_host(login_payload: dict[str, Any], default: str = DEFAULT_API_HOST) -> str:
    """Return the data host this account must use.

    The login answer lists `domains: [{"host": ..., "cnames": [...]}]`. An EU
    account gets a `-de2` cname there; an account with no mapping keeps the
    generic host.
    """
    for entry in login_payload.get("domains") or []:
        if not isinstance(entry, dict) or "api-mifit" not in str(entry.get("host", "")):
            continue
        cnames = [name for name in entry.get("cnames") or [] if isinstance(name, str)]
        if cnames:
            return cnames[0]
    return default


def auth_from_login(login_payload: dict[str, Any], api_host: str | None = None) -> ZeppAuth:
    """Build the credentials from the login answer. No token is ever printed."""
    token_info = login_payload.get("token_info") or {}
    app_token = token_info.get("app_token")
    user_id = token_info.get("user_id")
    if not app_token or not user_id:
        raise ZeppApiError(f"Login failed: {login_payload.get('error_code') or login_payload}")
    return ZeppAuth(
        app_token=str(app_token),
        user_id=str(user_id),
        api_host=api_host or resolve_api_host(login_payload),
    )


def login(
    email: str,
    password: str,
    *,
    session: requests.Session | None = None,
    api_host: str | None = None,
    debug: DebugDump | None = None,
) -> ZeppAuth:
    """Log in with email and password and return the usable credentials."""
    session = session or requests.Session()
    access_token, country_code = request_access_token(email, password, session)
    payload = exchange_access_token(access_token, country_code, session)
    if debug is not None:
        debug.write("login", payload)
    return auth_from_login(payload, api_host)


def check_business_code(payload: Any, path: str) -> None:
    """Raise when a HTTP 200 answer carries an error code in its body.

    The gateway answers 200 with `code` 0 for an invalid token, 404 for a
    retired route and a negative number for a bad request. A code above 0 and
    below 400 is accepted: an endpoint with no data can answer with one.
    """
    if not isinstance(payload, dict) or "code" not in payload:
        return
    code = _to_float(payload.get("code"))
    if code is None or 0 < code < 400:
        return
    detail = f"{payload.get('code')} {payload.get('message', '')}".strip()
    raise ZeppApiError(f"{path}: API error {detail}")


class ZeppClient:
    """Reads members and weight history for one Zepp account."""

    def __init__(
        self,
        auth: ZeppAuth,
        *,
        session: requests.Session | None = None,
        debug: DebugDump | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.auth = auth
        self.session = session or requests.Session()
        self.debug = debug
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        """Root of every data call."""
        return f"https://{self.auth.api_host}"

    def _headers(self) -> dict[str, str]:
        """Headers the gateway wants. Without `appname` it answers 401."""
        return {
            "apptoken": self.auth.app_token,
            "appname": "com.xiaomi.hm.health",
            "appplatform": "android_phone",
            "v": "2.0",
            "lang": "en",
            "user-agent": "MiFit/6.3.5 (android_phone)",
        }

    def _request(self, method: str, path: str, label: str, **kwargs: Any) -> Any:
        """Run one API call, dump it when asked, and return the parsed JSON."""
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code == 401:
            raise ZeppApiError(f"{path}: the app token is not valid any more (HTTP 401)")
        response.raise_for_status()
        payload = response.json()
        if self.debug is not None:
            self.debug.write(label, payload)
        check_business_code(payload, path)
        return payload

    def verify_token(self) -> None:
        """Check the app token on a route that is still behind authentication.

        `/users/{id}/bodyComposition` answers 404 for every token, valid or
        not, so it cannot tell them apart.
        """
        self._request("GET", "/v1/sport/run/history.json", "token-check", params={"limit": 1})

    def fetch_user_info(self) -> dict[str, Any]:
        """Read the owner profile, for the owner display name."""
        return self._request(
            "GET",
            "/huami.health.getUserInfo.json",
            "user-info",
            params={"userid": self.auth.user_id},
        )

    def fetch_family_members(self) -> list[dict[str, Any]]:
        """Read the scale family members: `fuid`, nickname, height, birthday."""
        payload = self._request(
            "POST",
            "/huami.health.scale.familymember.get.json",
            "family-members",
            data={"fuid": "all", "userid": self.auth.user_id},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ZeppApiError(f"Unexpected family member answer: {payload}")
        return [entry for entry in data.get("list") or [] if isinstance(entry, dict)]

    def fetch_weight_page(self, member_id: str, to_time: int) -> dict[str, Any]:
        """Read one page of weight records, newest first, ending at `to_time`."""
        return self._request(
            "GET",
            f"/users/{self.auth.user_id}/members/{member_id}/weightRecords",
            f"weight-{member_id}-{to_time}",
            params={"limit": PAGE_LIMIT, "toTime": to_time},
        )

    def iter_weight_records(
        self,
        member_id: str,
        start_time: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every raw weight record of one member, newest first."""
        start = start_time if start_time is not None else int(time.time())
        return iter_record_pages(
            lambda to_time: self.fetch_weight_page(member_id, to_time), start
        )


def owner_display_name(client: ZeppClient) -> str:
    """Return the owner display name, or "owner" when the profile route fails.

    The name is only a label in the member table, so a retired profile route
    must not stop the export.
    """
    try:
        payload = client.fetch_user_info()
    except (ZeppApiError, requests.RequestException):
        return "owner"
    return owner_name_from_user_info(payload)


def members_with_owner(client: ZeppClient) -> list[Member]:
    """Read the family list and prepend the account owner."""
    entries = client.fetch_family_members()
    return parse_members(entries, owner_display_name(client))


def is_imported(item: dict[str, Any]) -> bool:
    """Tell whether Zepp copied this row from another app."""
    return bool(parse_summary(item.get("summary")).get(IMPORTED_MARKER_KEY))


def collect_records(
    client: ZeppClient,
    members: Iterable[Member],
) -> tuple[RecordCollector, set[str]]:
    """Fetch the full history of every member and return it with the baby ids.

    `members/-1` can already answer for the whole family, so each member is
    queried anyway and the collector drops what comes back twice.
    """
    collector = RecordCollector()
    baby_ids: set[str] = set()
    for member in members:
        items = list(client.iter_weight_records(member.member_id))
        baby_ids |= baby_ids_from_records(items, member.member_id)
        collector.add_all(items, member.member_id)
    return collector, baby_ids
