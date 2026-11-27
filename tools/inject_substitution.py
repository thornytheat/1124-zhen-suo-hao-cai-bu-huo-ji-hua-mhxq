"""向合成样例注入同库原品断供 / 替用品领用场景（确定性、可重复）。

场景：社区巡诊库点，原品 GAUZE-07（纱布块）在第 63–66 日全日断供，
临床改用替用品 WIPE-04（消毒擦片）。另附一条第 66 日替用品领用的晚到
修订（第 84 日才过账），用于验证晚到记录改变替代量后的全链路重算。

运行：python tools/inject_substitution.py
"""
from __future__ import annotations

import json
from pathlib import Path

STORE = '社区巡诊'
ORIGINAL = 'GAUZE-07'
SUBSTITUTE = 'WIPE-04'
OUTAGE_DAYS = (63, 64, 65, 66)
# 原品断供日，替用品在自身平时用量之上的多领件数（即替代量）。
SUBSTITUTE_EXTRA = {63: 14, 64: 12, 65: 16, 66: 10}
LATE_DAY = 66
LATE_EXTRA = 18
LATE_POSTED = 84


def main() -> None:
    path = Path(__file__).resolve().parents[1] / 'sample_data' / 'community_clinic_stockroom.json'
    data = json.loads(path.read_text(encoding='utf-8'))

    daily_index = {(row['sku'], row['store'], row['day']): row for row in data['daily']}
    receipt_index = {(row['ticket'],): row for row in data['receipts']}

    # 1) 原品断供日：整班不可售、当日零领用、结存清零。
    for day in OUTAGE_DAYS:
        shelf = daily_index[(ORIGINAL, STORE, day)]
        shelf['unavailable_minutes'] = shelf['open_minutes']
        shelf['closing_units'] = 0
        ticket = f'RC-07-{day:03d}'
        receipt = receipt_index[(ticket,)]
        assert receipt['movement'] == 'issue' and receipt['revision'] == 1
        # 领用流水契约要求 units >= 1，断供日零领用通过删除该 issue 表达。
        data['receipts'].remove(receipt)

    # 2) 替用品在断供日多领（保留其自身平时用量）。
    for day, extra in SUBSTITUTE_EXTRA.items():
        ticket = f'RC-04-{day:03d}'
        receipt = receipt_index[(ticket,)]
        receipt['units'] += extra

    # 3) 晚到的替用品领用：第 66 日实际多领 LATE_EXTRA 件，第 84 日才过账。
    late_ticket = 'RC-04-L66'
    assert (late_ticket,) not in receipt_index
    data['receipts'].append({
        'sku': SUBSTITUTE,
        'store': STORE,
        'ticket': late_ticket,
        'movement': 'issue',
        'day': LATE_DAY,
        'posted': LATE_POSTED,
        'units': LATE_EXTRA,
        'unit': 'piece',
        'pack': 24,
        'revision': 1,
    })

    # 4) 替代关系目录（同库、原品 -> 替用品）。
    data.setdefault('substitutions', [])
    link = {'store': STORE, 'original_sku': ORIGINAL, 'substitute_sku': SUBSTITUTE}
    assert link not in data['substitutions']
    data['substitutions'].append(link)

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'updated {path}: outage {OUTAGE_DAYS}, late ticket {late_ticket}')


if __name__ == '__main__':
    main()
