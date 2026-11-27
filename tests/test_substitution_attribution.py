import pytest

from clinicstock.substitution_attribution.contracts import InvalidRow, parse_link, parse_usage_day
from clinicstock.substitution_attribution.service import evaluate, validate
from clinicstock.substitution_attribution import algorithm


LINK = {'store': '门诊', 'original_sku': 'A', 'substitute_sku': 'B'}


def shelf(sku, day, *, net, open_minutes=480, unavailable_minutes=0, adjusted=None, reason='COMPLETE_DAY'):
    return dict(sku=sku, store='门诊', day=day, open_minutes=open_minutes,
                unavailable_minutes=unavailable_minutes, net_units=net,
                adjusted_units=net if adjusted is None else adjusted, reason=reason)


def original_outage(day, net=0):
    return shelf('A', day, net=net, unavailable_minutes=480, adjusted=None, reason='SEVERE_CENSORING')


def build(outage_days, substitute_nets=None, *, normal_nets=None):
    """构造原品 A 在 outage_days 全日断供、替用品 B 每日有领用的样例。"""
    rows = []
    normal_nets = normal_nets or [12, 14, 10, 13, 11, 15, 12, 14]
    cursor = 0
    for day in range(1, 30):
        if day in outage_days:
            rows.append(original_outage(day))
        else:
            rows.append(shelf('A', day, net=18))
        sub_net = substitute_nets[day] if substitute_nets and day in substitute_nets else normal_nets[cursor % len(normal_nets)]
        rows.append(shelf('B', day, net=sub_net))
        cursor += 1
    return rows


def test_original_available_day_never_attributes_substitution():
    # 原品有货时，替用品领得再多也算它自己的用量。
    rows = [shelf('A', 1, net=20), shelf('B', 1, net=90)]
    first, = evaluate([LINK], rows)['records']
    assert first['substituted_units'] == 0
    assert first['reason'] == 'ORIGINAL_AVAILABLE'
    assert first['substitute_adjusted_units'] == 90


def test_full_outage_attributes_only_excess_above_own_baseline():
    # B 平时基线中位数 12.5（取整 12）；断供日领 26，只有超出基线部分归原品，自身需求不被挤掉。
    rows = build({20}, {20: 26})
    row = next(item for item in evaluate([LINK], rows)['records'] if item['day'] == 20)
    assert row['reason'] == 'SUBSTITUTION_RECOVERED'
    assert row['substituted_units'] == 26 - 12
    assert row['substitute_adjusted_units'] == 12
    assert row['original_adjusted_units'] == 14


def test_substitution_never_negative_and_respects_observed_floor():
    # 断供日替用品领得比平时还少：替代量为 0，不把替用品自身用量扣成负数。
    rows = build({20}, {20: 3})
    row = next(item for item in evaluate([LINK], rows)['records'] if item['day'] == 20)
    assert row['substituted_units'] == 0
    assert row['substitute_adjusted_units'] == 3
    assert row['original_adjusted_units'] == 0
    assert row['reason'] == 'NO_EXCESS_ISSUE'


def test_partial_outage_does_not_open_substitution_window():
    # 部分断供仍走暴露修复，替代归因不接管，避免与可售修复重复回补。
    rows = [r for r in build(set()) if not (r['sku'] == 'A' and r['day'] == 20)]
    rows.append(shelf('A', 20, net=6, unavailable_minutes=160, adjusted=9, reason='EXPOSURE_ADJUSTED'))
    row = next(item for item in evaluate([LINK], rows)['records'] if item['day'] == 20)
    assert row['substituted_units'] == 0
    assert row['reason'] == 'ORIGINAL_AVAILABLE'


def test_two_sides_reconcile_unit_for_unit():
    rows = build({20, 21, 22}, {20: 26, 21: 30, 22: 24})
    result = evaluate([LINK], rows)
    summary, = result['summaries']
    assert summary['reconciled']
    assert summary['substituted_units'] > 0
    assert summary['original_adjusted_units'] == summary['original_observed_units'] + summary['substituted_units']
    assert summary['substitute_adjusted_units'] == summary['substitute_observed_units'] - summary['substituted_units']
    assert (summary['original_adjusted_units'] + summary['substitute_adjusted_units']
            == summary['original_observed_units'] + summary['substitute_observed_units'])
    for row in result['records']:
        assert row['substitute_adjusted_units'] >= 0


def test_recovered_demand_is_not_counted_twice_in_stockout_repair():
    # 原品全日断供日 M2 无修复值；归因后该日由替代量回补，同一件数不会两条链路各算一次。
    rows = build({20}, {20: 26})
    record = next(item for item in evaluate([LINK], rows)['records'] if item['day'] == 20)
    assert record['original_net_units'] == 0
    assert record['original_adjusted_units'] == record['substituted_units']


def test_outage_without_enough_baseline_history_is_left_alone():
    rows = [original_outage(2), shelf('B', 1, net=12), shelf('B', 2, net=40)]
    row = next(item for item in evaluate([LINK], rows)['records'] if item['day'] == 2)
    assert row['substituted_units'] == 0
    assert row['reason'] == 'OUTAGE_WITHOUT_BASELINE'


def test_summary_counts_all_full_outages_even_when_not_attributed():
    # 第 2 日因基线不足未归因，但仍是全日断供；汇总的断供天数与替代量要分开统计。
    rows = [original_outage(2), shelf('B', 1, net=12), shelf('B', 2, net=40)] + build({20}, {20: 26})
    summary, = evaluate([LINK], rows)['summaries']
    assert summary['outage_days'] == 2
    assert summary['substituted_units'] > 0
    assert summary['reconciled']


def test_closed_substitute_on_outage_day_has_no_attribution():
    rows = build({20}, {20: 0})
    closed = [r for r in rows if not (r['sku'] == 'B' and r['day'] == 20)]
    closed.append(shelf('B', 20, net=0, open_minutes=0, reason='CLOSED_SESSION'))
    row = next(item for item in evaluate([LINK], closed)['records'] if item['day'] == 20)
    assert row['substituted_units'] == 0
    assert row['reason'] == 'OUTAGE_SUBSTITUTE_CLOSED'


def test_substitution_scope_never_crosses_store():
    foreign = [dict(r, store='二站') for r in build({20}, {20: 26})]
    out = evaluate([LINK], foreign)
    assert all(row['substituted_units'] == 0 for row in out['records'])


def test_late_usage_revision_changes_attribution_and_forecast_inputs():
    # 晚到的领用记录抬高断供日替用品净领后，替代量与两边调整目标都要随之重算。
    early = build({20}, {20: 26})
    late = build({20}, {20: 34})
    before = next(r for r in evaluate([LINK], early)['records'] if r['day'] == 20)
    after = next(r for r in evaluate([LINK], late)['records'] if r['day'] == 20)
    assert after['substituted_units'] > before['substituted_units']
    assert after['original_adjusted_units'] > before['original_adjusted_units']
    assert after['substitute_adjusted_units'] == before['substitute_adjusted_units']


def test_duplicate_link_is_rejected():
    with pytest.raises(ValueError, match='DUPLICATE_SUBSTITUTION_LINK'):
        algorithm.calculate([parse_link(LINK), parse_link(LINK)], [parse_usage_day(shelf('A', 1, net=5))])


def test_one_substitute_serving_two_originals_is_rejected():
    other = {'store': '门诊', 'original_sku': 'C', 'substitute_sku': 'B'}
    with pytest.raises(ValueError, match='SUBSTITUTE_SHARED_BY_MULTIPLE_ORIGINALS'):
        algorithm.calculate([parse_link(LINK), parse_link(other)], [parse_usage_day(shelf('A', 1, net=5))])


def test_field_errors_are_isolated_by_line():
    bad_link = {'store': '门诊', 'original_sku': 'A', 'substitute_sku': 'A'}
    accepted_links, _, rejected = validate([bad_link], [shelf('A', 1, net=5)])
    assert accepted_links == []
    assert rejected[0].code == 'SUBSTITUTE_SAME_AS_ORIGINAL'
    bad_day = dict(shelf('B', 1, net=5), net_units='x')
    _, accepted_days, day_errors = validate([LINK], [bad_day])
    assert accepted_days == [] and day_errors[0].code == 'EXPECTED_INTEGER'
    with pytest.raises(InvalidRow):
        parse_usage_day(dict(shelf('B', 1, net=5), adjusted_units=True))
