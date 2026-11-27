from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from . import (
    carton_budget,
    consumption_review,
    demand_smoothing,
    outage_today,
    reorder_bands,
    substitution_attribution,
    usage_receipts,
)


SNAPSHOT = 85
DEFAULT_CAPACITY = 48


def read_warehouse():
    source = Path(__file__).resolve().parents[2] / 'sample_data' / 'community_clinic_stockroom.json'
    return json.loads(source.read_text(encoding='utf-8'))


def daily_accounting(tickets, sessions):
    totals = Counter()
    for ticket in tickets:
        if ticket['net'] is not None:
            totals[ticket['sku'],ticket['store'],ticket['day']] += ticket['net']
    return [session|{'net_units':totals[session['sku'],session['store'],session['day']]} for session in sessions]


def forecast_validation(forecasts, eligible_rows):
    # 验证真值只取自完整营业日；替代归因改写过的日期不再作为观察真值，
    # 归回原品的替代量不能再进入断供回补与留出校验。
    observed = {(row['store'],row['sku'],row['day']) for row in eligible_rows if row['reason']=='COMPLETE_DAY'}
    return [
        dict(record,store=source['store'],sku=source['sku'],available=True)
        for source in forecasts for record in source['training_errors']
        if (source['store'],source['sku'],record['day']) in observed
    ]


def apply_substitution(shelf_rows, attribution):
    """把替代量并入可售修复结果。

    原品全日断供日的需求由替用品领用回补（M2 当日通常无修复值）；
    替用品自身只保留扣减替代量后的用量。归回量不再进入断供暴露回补，
    因此同一件数不会在两条链路上各算一次。
    """
    overrides = {}
    for row in attribution['records']:
        if row['substituted_units'] <= 0:
            continue
        key = row['store'], row['original_sku'], row['day']
        overrides[key] = (row['original_adjusted_units'], 'SUBSTITUTION_RECOVERED')
        sub_key = row['store'], row['substitute_sku'], row['day']
        overrides[sub_key] = (row['substitute_adjusted_units'], 'OWN_DEMAND_AFTER_SUBSTITUTION')

    merged = []
    for row in shelf_rows:
        key = row['store'], row['sku'], row['day']
        if key not in overrides:
            merged.append(row)
            continue
        adjusted_units, reason = overrides[key]
        merged.append(row | {'eligible': True, 'adjusted_units': float(adjusted_units),
                             'reason': reason, 'availability': None})
    return merged


def compose_requests(shelves, trend, intervals):
    keyed = {(row['store'],row['sku']):row for row in trend}
    bounds = {(row['store'],row['sku']):row for row in intervals}
    for item in shelves:
        identity = item['store'],item['sku']
        model = keyed.get(identity,{})
        forecast = model.get('forecast_total')
        safety = bounds.get(identity,{}).get('cycle_buffer')
        yield item | {'forecast':forecast or 0,'buffer':safety or 0,'validated':forecast is not None and safety is not None}


def decisions(order, forecasts):
    forecast = {(row['store'],row['sku']):row['forecast_total'] for row in forecasts}
    return [{
        'sku':row['sku'],'store':row['store'],'stock':row['on_hand'],
        'forecast':round(estimate,1) if (estimate:=forecast.get((row['store'],row['sku']))) is not None else None,
        'buffer':row['buffer'] if row['validated'] else None,'cases':row['allocated_cases'],
        'cost':row['spent']/100,'status':row['state'],'reason':row['reason'],
    } for row in order]


def run(capacity: int | None = None, *, as_of: int | None = None) -> dict:
    data = read_warehouse()
    snapshot = SNAPSHOT if as_of is None else as_of
    tickets = usage_receipts.evaluate(data['receipts'],as_of=snapshot)
    periods = outage_today.evaluate(daily_accounting(tickets['records'],data['daily']),today=snapshot)
    attribution = substitution_attribution.evaluate(
        data.get('substitutions', []), periods['records'],
    )
    merged_shelves = apply_substitution(periods['records'], attribution)
    known = [row for row in merged_shelves if row['eligible']]
    forecast = demand_smoothing.evaluate(known,today=snapshot)
    validation = forecast_validation(forecast['records'],merged_shelves)
    bands = reorder_bands.evaluate(validation,as_of=snapshot)
    requests = list(compose_requests(data['stock'],forecast['records'],bands['records']))
    money = 1000*(DEFAULT_CAPACITY if capacity is None else capacity)
    purchase = carton_budget.evaluate(requests,money=money)
    closing = [
        dict(row,model=row['predicted'],baseline=row['naive'],realized=row['actual'],closed=row['day']+1,observed=True)
        for row in validation if row['naive'] is not None
    ]
    review = consumption_review.evaluate(closing,as_of=snapshot)
    decisions_view = decisions(purchase['records'],forecast['records'])
    return {
        'reconciliation': {
            'receipt_balance': usage_receipts.balance_receipts(tickets),
            'exposure_sensitivity': outage_today.compare_exposure_policies(daily_accounting(tickets['records'],data['daily']),today=snapshot),
            'substitution_ledger': attribution['summaries'],
            'purchase_comparison': carton_budget.optimize_cartons(requests,money=money),
            'observed_weekly_holdout': consumption_review.compare_weekly_holdout(closing,as_of=snapshot),
        },
        'metrics': {'耗材品项':len(decisions_view),'建议整箱':sum(row['cases'] for row in decisions_view),
                    '建议金额':round(sum(row['cost'] for row in decisions_view),2),
                    '待人工核量':sum(row['status']=='待人工' for row in decisions_view)},
        'columns': {'sku':'耗材','store':'库点','stock':'现存件数','forecast':'未来七日','buffer':'安全库存',
                    'cases':'建议箱数','cost':'采购金额','status':'处理状态','reason':'处置证据'},
        'rows': decisions_view,
        'series': [{'label':row['label'],'value':round(row['model_mae'],2)}
                   for row in review['records'] if row['scope']=='SKU' and row['model_mae'] is not None],
        'stages': [{'title':title,'summary':summary,'records':result['records'],'errors':result['errors']}
                   for title,summary,result in [
                       ('领用流水','调拨及破损不进入临床消耗，修订冲突保留凭证',tickets),
                       ('可售修复','断供期按暴露比例修正，重度缺失期隔离',periods),
                       ('替代归因','同库断供日把替用品超基线领用归回原品，两边件数逐笔对账',attribution),
                       ('用量建模','同期中位数结合 Croston 稀疏需求模型',forecast),
                       ('残差区间','非重叠七日回测窗口估计安全件数',bands),
                       ('整箱分配','按优先级、边际短缺减少量和冷位配额采购',purchase),
                       ('消耗复盘','对真实结算窗口同时回放模型与同期基线',review),
                   ]],
    }
