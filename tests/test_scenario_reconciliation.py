from collections import Counter
from decimal import Decimal
from functools import lru_cache
from math import ceil

import pytest

from clinicstock.pipeline import read_warehouse, run


@lru_cache(maxsize=4)
def completed_case(capacity):
    return run(capacity)


@pytest.mark.parametrize('capacity', [1,9,48,100])
def test_stock_and_order_ledgers_conserve_cents_and_fulfillment(capacity):
    plan=completed_case(capacity)
    stage=plan['stages'][5]['records']
    root=read_warehouse()
    portfolio=plan['reconciliation']['purchase_comparison']
    receipts=plan['reconciliation']['receipt_balance']
    stockroom={(item['store'],item['sku']):item for item in root['stock']}
    expected_budget=capacity*1000
    assert portfolio['budget_cents']==expected_budget
    assert sum(row['spent'] for row in stage)==portfolio['baseline_spend_cents']
    assert all(row['budget_remaining']+portfolio['baseline_spend_cents']==expected_budget for row in stage)
    assert sum(Decimal(str(row['cost'])) for row in plan['rows'])==Decimal(portfolio['baseline_spend_cents'])/100
    assert sum(row['cases'] for row in plan['rows'])==plan['metrics']['建议整箱']
    assert receipts['raw_rows']==len(root['receipts'])
    assert receipts['balance_ok']
    for order in stage:
        item=stockroom[order['store'],order['sku']]
        assert order['pack']==item['pack'] and order['on_hand']==item['on_hand']
        assert order['allocated_cases']<=order['requested_cases']
        assert order['spent']==order['allocated_cases']*item['case_cost']
        if order['allocated_cases']:
            staged_units=order['on_hand']+order['inbound']+order['allocated_cases']*order['pack']
            assert staged_units<=item['cold_capacity']
            assert order['validated']
            assert order['shortage']==max(0,ceil(order['forecast'])+order['buffer']-order['on_hand']-order['inbound'])
        if not order['validated']:
            assert order['state']=='待人工'
            assert not order['allocated_cases'] and order['shortage'] is None


def test_forecasts_and_reorder_buffers_align_with_only_closed_clinic_sessions():
    batch=completed_case(48)
    by_item={(row['store'],row['sku']):row for row in batch['stages'][3]['records']}
    intervals={(row['store'],row['sku']):row for row in batch['stages'][4]['records']}
    base_sessions=Counter((row['store'],row['sku']) for row in batch['stages'][1]['records'] if row['eligible'])
    recovered=Counter((row['store'],row['original_sku']) for row in batch['stages'][2]['records'] if row['substituted_units']>0)
    for stage in batch['stages'][5]['records']:
        location=stage['store'],stage['sku']
        model=by_item[location]
        interval=intervals.get(location,{})
        # 替代归因把全日断供日重新纳入训练：有效天数 = 可售修复有效日 + 替代回补日。
        assert model['history_days']==base_sessions[location]+recovered[location]
        horizon=model['daily_forecast']
        if model['forecast_total'] is None:
            assert horizon==[]
        else:
            assert len(horizon)==7
            assert model['forecast_total']==pytest.approx(sum(horizon))
            assert all(item>=0 for item in horizon)
        if stage['validated']:
            assert stage['forecast']==model['forecast_total']
            assert stage['buffer']==interval['cycle_buffer']
            assert interval['state']=='BAND_READY'
        else:
            assert model['forecast_total'] is None or interval.get('cycle_buffer') is None


def test_independent_inventory_runs_share_data_but_never_reuse_budget_allocations():
    low=completed_case(1)
    high=completed_case(100)
    assert low['metrics']['建议整箱']<high['metrics']['建议整箱']
    assert low['metrics']['建议金额']<high['metrics']['建议金额']
    assert [item['records'] for item in low['stages'][:5]]==[item['records'] for item in high['stages'][:5]]
    unchanged=['receipt_balance','exposure_sensitivity','observed_weekly_holdout']
    assert all(low['reconciliation'][key]==high['reconciliation'][key] for key in unchanged)
    assert low['reconciliation']['purchase_comparison']['weighted_coverage']<=high['reconciliation']['purchase_comparison']['weighted_coverage']


def test_per_sku_error_chart_has_no_data_from_unsettled_synthetic_baseline():
    batch=completed_case(48)
    report=batch['stages'][6]['records']
    products={row['label']:row for row in report if row['scope']=='SKU' and row['model_mae'] is not None}
    outline={row['label']:row['value'] for row in batch['series']}
    assert set(outline)==set(products)
    for name,val in outline.items():
        assert val==round(products[name]['model_mae'],2)
        assert products[name]['rows']>0
        assert products[name]['model_mae']>=0
    assert batch['metrics']['待人工核量']>0
    ready={row['sku'] for row in batch['rows'] if row['status']=='待人工'}
    assert not ready.intersection(outline)


def test_adjustments_do_not_treat_clinic_closure_and_zero_demand_as_one_status():
    batch=completed_case(48)
    eligible=batch['stages'][1]['records']
    completely_missing=[row for row in eligible if row['reason'] in {'CLOSED_SESSION','SEVERE_CENSORING'}]
    observed_zero=[row for row in eligible if row['reason']=='COMPLETE_DAY' and row['net_units']==0]
    assert completely_missing and observed_zero
    for item in completely_missing:
        assert not item['eligible']
        assert item['adjusted_units'] is None
    for item in observed_zero:
        assert item['eligible']
        assert item['adjusted_units']==0
    assert {row['sku'] for row in observed_zero}<={row['sku'] for row in batch['rows']}


def test_cash_constrained_request_lines_keep_a_positive_physically_fillable_gap():
    plan=completed_case(1)
    capped=[item for item in plan['stages'][5]['records'] if item['reason']=='BUDGET_LIMIT']
    assert capped
    for line in capped:
        assert line['validated']
        assert 0<=line['allocated_cases']<line['requested_cases']
        assert line['shortage']>0


PAIR = ('社区巡诊', 'GAUZE-07', 'WIPE-04')
OUTAGE_DAYS = (63, 64, 65, 66)


def pair_attribution(plan):
    return [row for row in plan['stages'][2]['records']
            if (row['store'], row['original_sku'], row['substitute_sku']) == PAIR]


def test_substitution_recovers_demand_only_on_full_outage_days_and_reconciles():
    plan = completed_case(48)
    rows = pair_attribution(plan)
    by_day = {row['day']: row for row in rows}
    # 原品有货日一律不产生替代量；断供日按替用品超基线领用回补。
    assert all(by_day[day]['substituted_units'] == 0 for day in by_day if day not in OUTAGE_DAYS)
    assert all(by_day[day]['substituted_units'] > 0 for day in OUTAGE_DAYS)
    ledger, = plan['reconciliation']['substitution_ledger']
    assert ledger['reconciled']
    assert ledger['substituted_units'] == sum(by_day[day]['substituted_units'] for day in OUTAGE_DAYS)
    # 归回量逐笔守恒，且替用品调整后不会被扣成负数。
    assert all(by_day[day]['substitute_adjusted_units'] >= 0 for day in OUTAGE_DAYS)
    assert ledger['original_adjusted_units'] == ledger['original_observed_units'] + ledger['substituted_units']


def test_recovered_substitution_feeds_forecast_without_double_counting_stockout_repair():
    plan = completed_case(48)
    m2 = {(row['sku'], row['day']): row for row in plan['stages'][1]['records'] if row['store'] == '社区巡诊'}
    m3 = {(row['store'], row['sku']): row for row in plan['stages'][3]['records']}
    # 断供日可售修复留空（SEVERE_CENSORING），替代量是该日唯一的回补来源。
    for day in OUTAGE_DAYS:
        assert m2['GAUZE-07', day]['reason'] == 'SEVERE_CENSORING'
        assert m2['GAUZE-07', day]['adjusted_units'] is None
    # 替代回补日进入原品训练目标，替用品同日目标只保留自身基线用量。
    by_day = {row['day']: row for row in pair_attribution(plan)}
    original_errors = {row['day']: row for row in m3['社区巡诊', 'GAUZE-07']['training_errors']}
    substitute_errors = {row['day']: row for row in m3['社区巡诊', 'WIPE-04']['training_errors']}
    for day in OUTAGE_DAYS:
        assert original_errors[day]['actual'] == by_day[day]['original_adjusted_units']
        assert substitute_errors[day]['actual'] == by_day[day]['substitute_adjusted_units']


def test_late_usage_record_recomputes_attribution_forecast_bands_and_cases():
    before = run(as_of=83)
    after = run(as_of=85)
    day66_before = next(row for row in pair_attribution(before) if row['day'] == 66)
    day66_after = next(row for row in pair_attribution(after) if row['day'] == 66)
    # 晚到的第 66 日领用（第 84 日过账）抬高替代量与原品回补量。
    assert day66_after['substituted_units'] > day66_before['substituted_units']
    assert day66_after['original_adjusted_units'] > day66_before['original_adjusted_units']
    # 替代量变化后，两边的预测指纹与残差区间签名都要重算（输入改变即失效）。
    for sku in ('GAUZE-07', 'WIPE-04'):
        f_before = next(row for row in before['stages'][3]['records'] if row['sku'] == sku)
        f_after = next(row for row in after['stages'][3]['records'] if row['sku'] == sku)
        assert f_before['fit_id'] != f_after['fit_id']
        b_before = next(row for row in before['stages'][4]['records'] if row['sku'] == sku)
        b_after = next(row for row in after['stages'][4]['records'] if row['sku'] == sku)
        assert b_before['signature'] != b_after['signature']
    # 原品预测吸收回补量后改变，订货环节以新预测重算短缺件数（整箱舍入未跨箱时箱数可不变）。
    g_before = next(row for row in before['stages'][5]['records'] if row['sku'] == 'GAUZE-07')
    g_after = next(row for row in after['stages'][5]['records'] if row['sku'] == 'GAUZE-07')
    assert g_before['forecast'] != g_after['forecast']
    assert g_before['shortage'] != g_after['shortage']
    assert g_after['forecast'] == next(
        row for row in after['stages'][3]['records'] if row['sku'] == 'GAUZE-07')['forecast_total']


def test_recomputed_forecast_actually_re_drives_order_cases_across_pack_boundary():
    # 同样的库存约束下，回补把预测推高过整箱边界时，订货箱数必须随之增加（选无冷位上限约束的替用品）。
    from clinicstock.carton_budget.algorithm import demand_inventory
    from clinicstock.carton_budget.contracts import parse
    plan = run(as_of=85)
    line = next(row for row in plan['stages'][5]['records'] if row['sku'] == 'WIPE-04')
    base = {key: line[key] for key in (
        'sku', 'store', 'pack', 'case_cost', 'on_hand', 'inbound', 'forecast', 'buffer',
        'validated', 'critical', 'cold_capacity', 'min_term_days')}
    lower = parse(base | {'forecast': base['forecast'] - 30})
    higher = parse(base | {'forecast': base['forecast'] + 30})
    assert demand_inventory(lower)[0] < demand_inventory(higher)[0]
