"""Deterministic category comparisons; suggestions require analyst review."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from ec_review import amount, lower_keys, number, numeric_id

TIER_TARGET_MARGINS = {
    1: Decimal('0.25'), 2: Decimal('0.23'), 3: Decimal('0.21'), 4: Decimal('0.18'),
    5: Decimal('0.16'), 6: Decimal('0.14'), 7: Decimal('0.12'), 8: Decimal('0.09'),
    9: Decimal('0.07'), 10: Decimal('0.05'),
}


def tier_benchmark_price(tier_cost, tier: int) -> Decimal | None:
    cost = number(tier_cost)
    if tier not in TIER_TARGET_MARGINS or cost is None or cost < 0:
        return None
    return (cost / (Decimal(1) - TIER_TARGET_MARGINS[tier])).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP)


def requested_item_tier_scenarios(report: dict) -> list[dict]:
    grouped = defaultdict(list)
    for row in report['scenarios']:
        grouped[(row.get('request_id'), numeric_id(row.get('prca_num')),
                 row.get('grouped_lvl2'))].append(row)

    output = []
    for (request_id, prca, category), request_lines in grouped.items():
        by_item = defaultdict(list)
        base_lines = [row for row in request_lines if row.get('candidate_tier') == 1]
        for row in base_lines:
            by_item[numeric_id(row.get('item'))].append(row)
        unique_lines = {item: rows[0] for item, rows in by_item.items() if item}
        duplicate_items = {item for item, rows in by_item.items() if len(rows) > 1}
        current_tiers = {row.get('current_tier') for row in request_lines
                 if row.get('current_tier') in range(1, 11)}
        current_tier = next(iter(current_tiers)) if len(current_tiers) == 1 else None
        conflicting_items = {item for item, rows in by_item.items()
            if len({(str(row.get('requested_uom') or '').strip().upper(),
                     str(number(row.get('requested_price')))) for row in rows}) > 1}
        group_results = []
        for tier in range(1, 11):
            item_prices, item_costs, errors = [], [], []
            for item, base_row in unique_lines.items():
                row = next((candidate for candidate in request_lines
                    if numeric_id(candidate.get('item')) == item
                    and candidate.get('candidate_tier') == tier), None)
                if row is None:
                    errors.append('MISSING_REQUEST_TIER_COST')
                    continue
                errors.extend(row.get('validation_errors') or [])
                price, cost = number(row.get('requested_price')), number(row.get('tier_cost'))
                if item in conflicting_items:
                    errors.append('CONFLICTING_REQUESTED_PRICES_FOR_ITEM')
                if price is None or cost is None or cost < 0:
                    errors.append('MISSING_OR_INVALID_REQUEST_PRICE_OR_TIER_COST')
                elif price <= cost:
                    errors.append('REQUEST_PRICE_DOES_NOT_EXCEED_TIER_COST')
                else:
                    item_prices.append(price)
                    item_costs.append(cost)
            request_sales_proxy = sum(item_prices, Decimal(0))
            request_cost_proxy = sum(item_costs, Decimal(0))
            request_margin = ((request_sales_proxy - request_cost_proxy) / request_sales_proxy
                              if not errors and request_sales_proxy > 0 else None)
            target = TIER_TARGET_MARGINS[tier]
            screening = ['ONE_UNIT_PER_DISTINCT_REQUESTED_ITEM_NO_QUANTITY_DATA']
            if duplicate_items:
                screening.append('DUPLICATE_REQUEST_LINES_COUNTED_ONCE')
            group_results.append({
                'request_id': request_id, 'prca': prca, 'category': category,
                'candidate_tier': tier, 'current_tier': current_tier,
                'analyst_benchmark_tier': current_tier, 'request_item_count': len(unique_lines),
                'requested_unit_sales_proxy': amount(request_sales_proxy) if request_margin is not None else None,
                'requested_unit_cost_proxy': amount(request_cost_proxy) if request_margin is not None else None,
                'requested_item_margin': amount(request_margin, '0.000001'),
                'tier_target_margin': amount(target, '0.000001'),
                'distance_from_tier_target': amount(abs(request_margin - target), '0.000001')
                    if request_margin is not None else None,
                'suggested_for_review': False, 'screening_flags': sorted(set(screening)),
                'exceptions': sorted(set(errors)),
            })
        eligible = [row for row in group_results if not row['exceptions']
                    and row['distance_from_tier_target'] is not None]
        if eligible:
            best_distance = min(Decimal(row['distance_from_tier_target']) for row in eligible)
            best = [row for row in eligible if Decimal(row['distance_from_tier_target']) == best_distance]
            if len(best) == 1:
                best[0]['suggested_for_review'] = True
            else:
                for row in best:
                    row['screening_flags'].append('TIED_MARGIN_ANALYST_SELECTION_REQUIRED')
        output.extend(group_results)
    return output


def partition_penny_contract_activity(raw_activity: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    retained, excluded, uncertain = [], [], []
    for raw in raw_activity:
        row = lower_keys(raw)
        penny_flag = str(row.get('penny_contract_flag') or '').strip().upper()
        customer_status = str(row.get('penny_customer_status') or '').strip().upper()
        if penny_flag == 'Y' and customer_status == 'Y':
            excluded.append(row)
        else:
            retained.append(row)
            if penny_flag == 'Y' and customer_status.startswith('UNKNOWN'):
                uncertain.append(row)
    return retained, excluded, uncertain


def category_scenarios(report: dict, raw_activity: list[dict]) -> list[dict]:
    activity, _, uncertain_penny_activity = partition_penny_contract_activity(raw_activity)
    uncertain_penny_keys = {(numeric_id(row.get('prca_num')),
                             numeric_id(row.get('item_e1_num')))
                            for row in uncertain_penny_activity}
    requested = defaultdict(list)
    for row in report['scenarios']:
        requested[(numeric_id(row.get('prca_num')), row['request_id'], row.get('grouped_lvl2'))].append(row)
    output = []
    for (prca, request_id, category), candidates in requested.items():
        portfolio = [row for row in activity if numeric_id(row.get('prca_num')) == prca
                     and row.get('grouped_lvl2') == category and row.get('prvt_brnd_flg') == 'Y'
                     and (number(row.get('qty_3_mth')) != 0 or number(row.get('sls_3_mth')) != 0)]
        base_errors = []
        if not portfolio:
            base_errors.append('NO_EXISTING_MAPPED_CATEGORY_ACTIVITY')
        if any((prca, numeric_id(row.get('item_e1_num'))) in uncertain_penny_keys for row in portfolio):
            base_errors.append('PENNY_CUSTOMER_STATUS_UNKNOWN_FOR_ACTIVITY')
        if not category:
            base_errors.append('MISSING_CATEGORY')
        sales, current_cost = Decimal(0), Decimal(0)
        baseline_complete = True
        for row in portfolio:
            quantity, revenue = number(row.get('qty_3_mth')), number(row.get('sls_3_mth'))
            if quantity is None or revenue is None or quantity < 0 or revenue < 0 or row.get('negative_activity_rows'):
                base_errors.append('INCOMPLETE_OR_NEGATIVE_CATEGORY_ACTIVITY')
            else:
                sales += revenue
            if row.get('item_matches') != 1 or row.get('tapestry_matches') != 1:
                base_errors.append('AMBIGUOUS_CATEGORY_REFERENCE')
            reference_uom = str(row.get('reference_sell_uom') or '').strip().upper()
            if not reference_uom or str(row.get('ipc_sell_uom') or '').strip().upper() != reference_uom:
                base_errors.append('CATEGORY_ACTIVITY_UOM_MISMATCH')
            current_tier = row.get('current_tier')
            cost = number(row.get(f'tier_{current_tier}')) if current_tier in range(1, 11) else None
            if row.get('attachment_matches') != 1 or cost is None or cost < 0 or quantity is None:
                baseline_complete = False
            else:
                current_cost += cost * quantity
        if sales <= 0:
            base_errors.append('NO_POSITIVE_CATEGORY_SALES')
        baseline_gp = sales - current_cost if baseline_complete and not base_errors else None
        tier_values = {row.get('current_tier') for row in candidates
                       if row.get('attachment_matches') == 1 and row.get('current_tier') in range(1, 11)}
        current_tier = next(iter(tier_values)) if len(tier_values) == 1 else None
        group_results = []
        for tier in range(1, 11):
            options = [row for row in candidates if row['candidate_tier'] == tier]
            if not options:
                raise ValueError('Every requested category must retain ten tier options')
            errors, screening, prices, assumed = list(base_errors), [], {}, []
            for row in options:
                item = numeric_id(row.get('item'))
                price, cost = number(row.get('requested_price')), number(row.get('tier_cost'))
                sell_uom = str(row.get('sell_uom') or '').strip().upper()
                raw_uom = str(row.get('requested_uom') or '').strip().upper()
                if not raw_uom and sell_uom:
                    assumed.append(row['line_key'])
                if row.get('source_duplicate_count', 1) > 1:
                    screening.append('DUPLICATE_SNAPSHOT_SOURCE_LINES')
                if not sell_uom or (raw_uom or sell_uom) != sell_uom:
                    errors.append('REQUESTED_UOM_MISMATCH')
                if row.get('customer_matches') != 1 or row.get('pex_matches') != 1 or row.get('bus_pltfrm') != 'EC':
                    errors.append('REQUEST_CUSTOMER_NOT_CONFIRMED_EC')
                if not item or row.get('item_matches') != 1 or row.get('tapestry_matches') != 1:
                    errors.append('INVALID_REQUEST_ITEM_REFERENCE')
                if price is None or price <= 0 or cost is None or cost < 0:
                    errors.append('INVALID_REQUEST_PRICE_OR_COST')
                elif price <= cost:
                    errors.append('REQUEST_PRICE_DOES_NOT_EXCEED_TIER_COST')
                tier_ten = next((number(option.get('tier_cost')) for option in candidates
                                 if option['line_key'] == row['line_key'] and option['candidate_tier'] == 10), None)
                if tier_ten is None:
                    screening.append('MISSING_TIER_10_SCREENING_COST')
                elif price is not None and price <= tier_ten:
                    screening.append('AT_OR_BELOW_TIER_10_COST_REQUIRES_REVIEW')
                if item in prices and prices[item] != price:
                    errors.append('CONFLICTING_REQUESTED_PRICES')
                prices[item] = price
            scenario_sales, scenario_cost = sales, Decimal(0)
            costs_complete = True
            for row in portfolio:
                quantity, revenue = number(row.get('qty_3_mth')), number(row.get('sls_3_mth'))
                cost = number(row.get(f'tier_{tier}'))
                if cost is None or cost < 0 or quantity is None:
                    errors.append('MISSING_CATEGORY_CANDIDATE_COST')
                    costs_complete = False
                else:
                    scenario_cost += cost * quantity
                price = prices.get(numeric_id(row.get('item_e1_num')))
                if price is not None and quantity is not None and revenue is not None:
                    scenario_sales += price * quantity - revenue
            tier_only_gp = sales - scenario_cost if costs_complete and not base_errors else None
            gp = scenario_sales - scenario_cost if not errors and scenario_sales > 0 else None
            margin = gp / scenario_sales if gp is not None else None
            tier_target_margin = TIER_TARGET_MARGINS[tier]
            tier_only_margin = tier_only_gp / sales if tier_only_gp is not None and sales > 0 else None
            delta = gp - baseline_gp if gp is not None and baseline_gp is not None else None
            spend = scenario_sales - sales if gp is not None else None
            cost_delta = scenario_cost - current_cost if tier_only_gp is not None and baseline_gp is not None else None
            result = {
                'prca': prca, 'request_id': request_id, 'category': category,
                'current_tier': current_tier, 'candidate_tier': tier,
                'category_item_count': len({row['item_e1_num'] for row in portfolio}),
                'requested_items_without_activity': len(set(prices) - {numeric_id(row.get('item_e1_num')) for row in portfolio}),
                'baseline_sales_3m': amount(sales) if not base_errors else None,
                'baseline_gp_3m': amount(baseline_gp),
                'baseline_margin': amount(baseline_gp / sales, '0.000001') if baseline_gp is not None else None,
                'tier_only_gp_3m': amount(tier_only_gp),
                'tier_only_margin': amount(tier_only_margin, '0.000001'),
                'tier_target_margin': amount(tier_target_margin, '0.000001'),
                'requested_price_sales_3m': amount(scenario_sales) if gp is not None else None,
                'requested_price_gp_3m': amount(gp), 'requested_price_margin': amount(margin, '0.000001'),
                'distance_from_tier_target': amount(abs(margin - tier_target_margin), '0.000001')
                    if margin is not None else None,
                'annualized_gp_change': amount(delta * 4) if delta is not None else None,
                'annualized_internal_cost_change': amount(cost_delta * 4) if cost_delta is not None else None,
                'annualized_customer_spend_change': amount(spend * 4) if spend is not None else None,
                'suggested_for_review': False, 'assumed_sell_uom_lines': sorted(set(assumed)),
                'screening_flags': sorted(set(screening)), 'exceptions': sorted(set(errors)),
                'approval_status': 'NOT_APPROVED', 'opportunity_projection': 'NOT_PROVIDED',
            }
            group_results.append(result)
        eligible = [row for row in group_results if not row['exceptions']
                and 'DUPLICATE_SNAPSHOT_SOURCE_LINES' not in row['screening_flags']
                and row['distance_from_tier_target'] is not None
                and row['requested_price_margin'] is not None]
        if eligible:
            best_distance = min(Decimal(row['distance_from_tier_target']) for row in eligible)
            best = [row for row in eligible if Decimal(row['distance_from_tier_target']) == best_distance]
            if len(best) == 1:
                best[0]['suggested_for_review'] = True
            else:
                for row in best:
                    row['screening_flags'].append('TIED_MARGIN_ANALYST_SELECTION_REQUIRED')
        output.extend(group_results)
    return output