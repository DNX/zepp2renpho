"""Neutral measurement record shared by the Zepp exporter and the RENPHO importer.

The exporter writes one JSON object per line. The importer reads the same file.
Masses are in kilograms. Percentages are 0-100. Timestamps are UTC epoch seconds.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

OPTIONAL_METRICS = (
    "bmi",
    "body_fat_pct",
    "water_pct",
    "muscle_kg",
    "bone_kg",
    "bmr_kcal",
    "visceral_fat",
    "protein_pct",
    "body_age",
    "impedance",
    "height_cm",
)


@dataclass(frozen=True)
class Member:
    """One person on the source account: the owner or a family member."""

    member_id: str
    name: str
    is_baby: bool | None = None
    birthday: str | None = None


@dataclass(frozen=True)
class Record:
    """One weigh-in of one member."""

    member_id: str
    timestamp: int
    weight_kg: float
    timezone: str | None = None
    bmi: float | None = None
    body_fat_pct: float | None = None
    water_pct: float | None = None
    muscle_kg: float | None = None
    bone_kg: float | None = None
    bmr_kcal: int | None = None
    visceral_fat: float | None = None
    protein_pct: float | None = None
    body_age: int | None = None
    impedance: int | None = None
    height_cm: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


def write_records(path: Path, records: Iterable[Record]) -> int:
    """Write records as JSON lines and return how many were written."""
    count = 0
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            count += 1
    return count


def read_records(path: Path) -> Iterator[Record]:
    """Read records from a JSON lines file written by `write_records`."""
    with path.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                yield Record(**json.loads(line))


def write_members(path: Path, members: Iterable[Member]) -> None:
    """Write the member list as a JSON array."""
    payload = [asdict(member) for member in members]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_members(path: Path) -> list[Member]:
    """Read a member list written by `write_members`."""
    return [Member(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
