from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class InvalidRow(ValueError):
    def __init__(self, field: str, code: str):
        super().__init__(f'{field}: {code}')
        self.field = field
        self.code = code


def integer(row: Mapping, key: str, low: int, high: int, field: str) -> int:
    value = row.get(key)
    if type(value) is not int:
        raise InvalidRow(field, 'EXPECTED_INTEGER')
    if not low <= value <= high:
        raise InvalidRow(field, 'OUT_OF_RANGE')
    return value


def text(row: Mapping, key: str, field: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRow(field, 'EXPECTED_NONEMPTY_TEXT')
    if len(value.strip()) > 80:
        raise InvalidRow(field, 'TEXT_TOO_LONG')
    return value.strip()


@dataclass(frozen=True, slots=True)
class SubstitutionLink:
    store: str
    original_sku: str
    substitute_sku: str


@dataclass(frozen=True, slots=True)
class ShelfUsageDay:
    """可售修复后的单日记录（M2 输出行的最小投影）。"""

    sku: str
    store: str
    day: int
    open_minutes: int
    unavailable_minutes: int
    net_units: int
    adjusted_units: float | None
    reason: str


def parse_link(values: Mapping[str, Any]) -> SubstitutionLink:
    if not isinstance(values, Mapping):
        raise InvalidRow('substitution_link', 'EXPECTED_OBJECT')
    store = text(values, 'store', 'substitution_link.store')
    original = text(values, 'original_sku', 'substitution_link.original_sku')
    substitute = text(values, 'substitute_sku', 'substitution_link.substitute_sku')
    if original == substitute:
        raise InvalidRow('substitution_link.substitute_sku', 'SUBSTITUTE_SAME_AS_ORIGINAL')
    return SubstitutionLink(store=store, original_sku=original, substitute_sku=substitute)


def parse_usage_day(values: Mapping[str, Any]) -> ShelfUsageDay:
    if not isinstance(values, Mapping):
        raise InvalidRow('substitution_day', 'EXPECTED_OBJECT')
    sku = text(values, 'sku', 'substitution_day.sku')
    store = text(values, 'store', 'substitution_day.store')
    day = integer(values, 'day', 0, 10000000, 'substitution_day.day')
    open_minutes = integer(values, 'open_minutes', 0, 1440, 'substitution_day.open_minutes')
    unavailable_minutes = integer(values, 'unavailable_minutes', 0, 1440, 'substitution_day.unavailable_minutes')
    net_units = integer(values, 'net_units', -100000, 100000, 'substitution_day.net_units')
    adjusted = values.get('adjusted_units')
    if adjusted is not None and (isinstance(adjusted, bool) or not isinstance(adjusted, (int, float))):
        raise InvalidRow('substitution_day.adjusted_units', 'EXPECTED_NUMBER')
    reason = text(values, 'reason', 'substitution_day.reason')
    return ShelfUsageDay(
        sku=sku,
        store=store,
        day=day,
        open_minutes=open_minutes,
        unavailable_minutes=unavailable_minutes,
        net_units=net_units,
        adjusted_units=None if adjusted is None else float(adjusted),
        reason=reason,
    )
