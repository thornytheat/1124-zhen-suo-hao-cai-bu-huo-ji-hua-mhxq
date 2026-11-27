from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .algorithm import calculate
from .contracts import InvalidRow, ShelfUsageDay, SubstitutionLink, parse_link, parse_usage_day


@dataclass(frozen=True, slots=True)
class AttributionRejection:
    line: int
    field: str
    code: str


def validate(links: Iterable[Mapping[str, Any]], shelf_rows: Iterable[Mapping[str, Any]]):
    accepted_links = []
    rejected = []
    for line, raw in enumerate(links, 1):
        try:
            accepted_links.append(parse_link(raw))
        except InvalidRow as issue:
            rejected.append(AttributionRejection(line, issue.field, issue.code))
    accepted_days = []
    for line, raw in enumerate(shelf_rows, 1):
        try:
            accepted_days.append(parse_usage_day(raw))
        except InvalidRow as issue:
            rejected.append(AttributionRejection(line, issue.field, issue.code))
    return accepted_links, accepted_days, rejected


def evaluate(links: Iterable[Mapping[str, Any]], shelf_rows: Iterable[Mapping[str, Any]], **options) -> dict:
    """计算同库原品/替用品的替代归因，逐行隔离字段错误，保留原因码和行号。"""
    accepted_links, accepted_days, rejected = validate(links, shelf_rows)
    daily, summaries = calculate(accepted_links, accepted_days, **options)
    return {
        'records': daily,
        'summaries': summaries,
        'accepted_link_count': len(accepted_links),
        'accepted_day_count': len(accepted_days),
        'rejected_count': len(rejected),
        'errors': [asdict(error) for error in rejected],
    }
