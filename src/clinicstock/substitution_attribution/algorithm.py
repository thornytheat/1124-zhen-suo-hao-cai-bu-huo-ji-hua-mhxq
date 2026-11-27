from __future__ import annotations

from .contracts import ShelfUsageDay, SubstitutionLink

from collections import defaultdict
from statistics import median


def is_full_outage(row: ShelfUsageDay) -> bool:
    """原品整班无货才构成替代窗口；部分断供仍走暴露修复，不产生替代归因。"""
    return row.open_minutes > 0 and row.unavailable_minutes == row.open_minutes


def is_open_day(row: ShelfUsageDay) -> bool:
    return row.open_minutes > 0


def own_demand_baseline(rows: list[ShelfUsageDay], outage_days: set[int], lookback: int, target_day: int) -> float | None:
    """替用品在原品有货日的净领用中位数，代表它自己的平时用量。"""
    history = [
        row.net_units
        for row in rows
        if is_open_day(row)
        and row.day not in outage_days
        and target_day - lookback <= row.day < target_day
        and row.net_units >= 0
    ]
    if len(history) < 3:
        return None
    return median(history)


def attribute_pair(link: SubstitutionLink, shelf_rows: list[ShelfUsageDay], *, lookback: int):
    by_sku = defaultdict(list)
    for row in shelf_rows:
        if row.store == link.store and row.sku in (link.original_sku, link.substitute_sku):
            by_sku[row.sku].append(row)
    originals = sorted(by_sku.get(link.original_sku, []), key=lambda row: row.day)
    substitutes = sorted(by_sku.get(link.substitute_sku, []), key=lambda row: row.day)
    outage_days = {row.day for row in originals if is_full_outage(row)}
    substitute_by_day = {row.day: row for row in substitutes}

    daily = []
    for original in originals:
        substitute = substitute_by_day.get(original.day)
        sub_net = substitute.net_units if substitute is not None else 0
        if not is_full_outage(original):
            # 原品有货（含部分断供）：替用品多领的仍是它自己的用量。
            substituted = 0
            explanation = 'ORIGINAL_AVAILABLE'
        else:
            baseline = own_demand_baseline(substitutes, outage_days, lookback, original.day)
            if baseline is None:
                substituted = 0
                explanation = 'OUTAGE_WITHOUT_BASELINE'
            elif substitute is None or not is_open_day(substitute):
                substituted = 0
                explanation = 'OUTAGE_SUBSTITUTE_CLOSED'
            else:
                substituted = max(0, sub_net - round(baseline))
                explanation = 'SUBSTITUTION_RECOVERED' if substituted else 'NO_EXCESS_ISSUE'

        original_adjusted = original.net_units + substituted
        substitute_adjusted = sub_net - substituted
        daily.append({
            'store': link.store,
            'original_sku': link.original_sku,
            'substitute_sku': link.substitute_sku,
            'day': original.day,
            'original_open_minutes': original.open_minutes,
            'original_unavailable_minutes': original.unavailable_minutes,
            'original_net_units': original.net_units,
            'substitute_net_units': sub_net,
            'substituted_units': substituted,
            'original_adjusted_units': original_adjusted,
            'substitute_adjusted_units': substitute_adjusted,
            'reason': explanation,
        })
    return daily


def reconcile(daily: list[dict]) -> dict:
    """两边领用件数对账：归回原品的量必须与替用品扣减量逐笔相等且守恒。"""
    original_observed = sum(row['original_net_units'] for row in daily)
    substitute_observed = sum(row['substitute_net_units'] for row in daily)
    moved = sum(row['substituted_units'] for row in daily)
    original_adjusted = sum(row['original_adjusted_units'] for row in daily)
    substitute_adjusted = sum(row['substitute_adjusted_units'] for row in daily)
    conserved = (
        original_adjusted == original_observed + moved
        and substitute_adjusted == substitute_observed - moved
        and original_adjusted + substitute_adjusted == original_observed + substitute_observed
        and all(row['substituted_units'] >= 0 for row in daily)
        and all(row['substitute_adjusted_units'] >= 0 for row in daily)
    )
    return {
        'store': daily[0]['store'] if daily else None,
        'original_sku': daily[0]['original_sku'] if daily else None,
        'substitute_sku': daily[0]['substitute_sku'] if daily else None,
        'days': len(daily),
        'outage_days': sum(row['original_unavailable_minutes'] == row['original_open_minutes']
                           and row['original_open_minutes'] > 0 for row in daily),
        'original_observed_units': original_observed,
        'substitute_observed_units': substitute_observed,
        'substituted_units': moved,
        'original_adjusted_units': original_adjusted,
        'substitute_adjusted_units': substitute_adjusted,
        'reconciled': conserved,
    }


def calculate(links: list[SubstitutionLink], shelf_rows: list[ShelfUsageDay], *, lookback=35):
    if lookback < 1:
        raise ValueError('INVALID_SUBSTITUTION_LOOKBACK')
    seen_pairs = set()
    substitute_roles = set()
    for link in links:
        key = link.store, link.original_sku, link.substitute_sku
        if key in seen_pairs:
            raise ValueError(f'DUPLICATE_SUBSTITUTION_LINK:{key}')
        seen_pairs.add(key)
        # 同一库点一个替用品只能对应一个原品，否则同日替代量会被重复归因、破坏对账。
        sub_role = link.store, link.substitute_sku
        if sub_role in substitute_roles:
            raise ValueError(f'SUBSTITUTE_SHARED_BY_MULTIPLE_ORIGINALS:{sub_role}')
        substitute_roles.add(sub_role)

    daily = []
    summaries = []
    for link in links:
        rows = attribute_pair(link, shelf_rows, lookback=lookback)
        daily.extend(rows)
        summaries.append(reconcile(rows))
    return sorted(daily, key=lambda row: (row['store'], row['original_sku'], row['day'])), summaries
