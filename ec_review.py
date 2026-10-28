"""Read-only requested-item tier scenarios; no recommendations or production writes."""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ec_etl_preview import connect_from_environment, json_value
from ec_workflow import canonical_json, utc_now


def number(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def numeric_id(value) -> str | None:
    text = str(value or '').strip()
    return text.split('.')[0] if re.fullmatch(r'[0-9]+(?:\.0+)?', text) else None


def lower_keys(row: dict) -> dict:
    return {key.lower(): value for key, value in row.items()}


def prepare_requests(preview: dict) -> tuple[list[dict], list[dict]]:
    headers = defaultdict(list)
    for raw in preview['source']['headers']:
        header = lower_keys(raw)
        headers[str(header.get('row_id') or '')].append(header)
    inputs, context = [], []
    for line_key, raw in enumerate(preview['source']['lines'], start=1):
        line = lower_keys(raw)
        request_id = str(line.get('row_id') or '')
        matches = headers[request_id]
        header = matches[0] if len(matches) == 1 else {}
        account = numeric_id(header.get('cust_num_prca'))
        item = numeric_id(line.get('item_e1_num'))
        price = str(line.get('req_price') if line.get('req_price') is not None else '')
        errors = []
        if len(matches) != 1:
            errors.append('MISSING_OR_AMBIGUOUS_HEADER')
        if not account:
            errors.append('MISSING_ACCOUNT_REFERENCE' if not header.get('cust_num_prca')
                          else 'INVALID_ACCOUNT_REFERENCE')
        if not item:
            errors.append('INVALID_ITEM_REFERENCE')
        if number(price) is None or number(price) <= 0:
            errors.append('INVALID_REQUESTED_PRICE')
        duplicate_count = line.get('source_duplicate_count', 1)
        duplicate_count = int(duplicate_count) if str(duplicate_count).isdigit() else 1
        inputs.append({'line_key': line_key, 'account_number': account, 'item': item})
        context.append({'line_key': line_key, 'request_id': request_id,
                        'account_provided': header.get('cust_num_prca'), 'item': item,
                        'requested_uom': line.get('req_uom'), 'requested_price': price,
                        'source_duplicate_count': duplicate_count,
                        'source_warnings': ['DUPLICATE_SOURCE_LINE_CONTENT'] if duplicate_count > 1 else [],
                        'source_item_description': line.get('item_dsc'),
                        'source_errors': errors})
    return inputs, context


def partition_unpriced_lines(lines: list[dict]) -> tuple[list[dict], list[dict]]:
    eligible, excluded = [], []
    for line in lines:
        raw_price = line.get('requested_price')
        price_text = '' if raw_price is None else str(raw_price).strip()
        price = number(price_text)
        if not price_text:
            reason = 'MISSING_REQUESTED_PRICE'
        elif price is None:
            reason = 'INVALID_REQUESTED_PRICE'
        elif price == 0:
            reason = 'ZERO_REQUESTED_PRICE'
        elif price < 0:
            reason = 'NEGATIVE_REQUESTED_PRICE'
        else:
            eligible.append(line)
            continue
        excluded.append({**line, 'price_exclusion_reason': reason})
    return eligible, excluded


def normalize_requested_price_to_sell_uom(line: dict, reference: dict) -> tuple[Decimal | None, str | None, str]:
    price = number(line.get('requested_price'))
    if price is None or price <= 0:
        return None, None, 'NOT_REQUIRED_NONPOSITIVE_OR_INVALID_PRICE'

    requested_uom = str(line.get('requested_uom') or '').strip().upper()
    sell_uom = str(reference.get('sell_uom') or '').strip().upper()
    buy_uom = str(reference.get('buy_uom') or '').strip().upper()
    primary_uom = str(reference.get('prmry_uom') or '').strip().upper()
    if not sell_uom:
        return None, None, 'UNKNOWN_MISSING_SELL_UOM'
    if not requested_uom:
        return price, '1', 'ASSUMED_SELL_UOM'
    if requested_uom == sell_uom:
        return price, '1', 'SELL_UOM'

    matches_buy = requested_uom == buy_uom and bool(buy_uom)
    matches_primary = requested_uom == primary_uom and bool(primary_uom)
    candidates = []
    if matches_buy:
        sell_to_buy = number(reference.get('sell_to_buy_divisor'))
        if sell_to_buy is not None and sell_to_buy > 0:
            candidates.append((price / sell_to_buy, f'1/{sell_to_buy}', 'BUY_TO_SELL_DIVISOR'))
    if matches_primary:
        primary_to_sell = number(reference.get('prmry_to_sell_divisor'))
        if primary_to_sell is not None and primary_to_sell > 0:
            candidates.append((price * primary_to_sell, str(primary_to_sell), 'PRIMARY_TO_SELL_DIVISOR'))

    if matches_buy and matches_primary and len(candidates) != 2:
        return None, None, 'UNKNOWN_AMBIGUOUS_UOM_CONVERSION'
    if not candidates:
        if matches_buy:
            return None, None, 'UNKNOWN_MISSING_BUY_TO_SELL_DIVISOR'
        if matches_primary:
            return None, None, 'UNKNOWN_MISSING_PRIMARY_TO_SELL_DIVISOR'
        return None, None, 'UNKNOWN_UNSUPPORTED_UOM_CONVERSION'
    if any(candidate[0] != candidates[0][0] for candidate in candidates[1:]):
        return None, None, 'UNKNOWN_AMBIGUOUS_UOM_CONVERSION'
    return candidates[0]


def partition_penny_contract_lines(lines: list[dict], references: list[dict]) -> tuple[list[dict], list[dict]]:
    tier_ten_by_line = {int(row['line_key']): lower_keys(row) for row in references
                        if int(row.get('tier_num') or 0) == 10}
    eligible, excluded = [], []
    for line in lines:
        reference = tier_ten_by_line.get(int(line['line_key']), {})
        enriched = dict(line)
        active_match = str(reference.get('penny_contract_flag') or '').strip().upper() == 'Y'
        reference_flag = reference.get('penny_contract_flag')
        enriched['penny_contract_flag'] = ('Y' if active_match else
            'N' if reference_flag is not None else 'UNKNOWN')
        enriched['penny_product_types'] = reference.get('penny_product_types')
        enriched['penny_man_contract_ids'] = reference.get('penny_man_contract_ids')
        enriched['penny_mck_contract_ids'] = reference.get('penny_mck_contract_ids')
        enriched['penny_contract_match_count'] = reference.get('penny_contract_match_count', 0)
        enriched['penny_contract_ambiguous'] = reference.get('penny_contract_ambiguous', False)
        enriched['penny_customer_status'] = reference.get('penny_customer_status',
            'UNKNOWN_CUSTOMER_MAPPING' if active_match or reference_flag is None else 'NOT_PENNY_ITEM')
        for field in ('sell_uom', 'buy_uom', 'prmry_uom', 'sell_to_buy_divisor',
                      'prmry_to_sell_divisor', 'prmry_to_buy_divisor'):
            enriched[field] = reference.get(field)
        enriched['penny_tier_10_cost'] = reference.get('tier_cost') if active_match else None
        enriched['penny_contract_required_flag'] = 'N' if reference_flag is not None else 'UNKNOWN'
        enriched['penny_contract_exclusion_reason'] = None

        if not active_match:
            enriched['penny_price_test_status'] = ('NOT_PENNY_ITEM' if reference_flag is not None
                                                   else 'UNKNOWN_ITEM_REFERENCE')
            eligible.append(enriched)
            continue

        normalized_price, conversion_factor, normalization_method = normalize_requested_price_to_sell_uom(
            line, reference)
        enriched['normalized_requested_price'] = str(normalized_price) if normalized_price is not None else None
        enriched['penny_uom_conversion_factor'] = conversion_factor
        enriched['penny_price_normalization_method'] = normalization_method
        if normalization_method == 'NOT_REQUIRED_NONPOSITIVE_OR_INVALID_PRICE':
            enriched['penny_price_test_status'] = normalization_method
            eligible.append(enriched)
            continue
        if normalized_price is None:
            enriched['penny_price_test_status'] = normalization_method
            enriched['penny_contract_required_flag'] = 'UNKNOWN'
            eligible.append(enriched)
            continue

        tier_ten_cost = number(reference.get('tier_cost'))
        if tier_ten_cost is None or tier_ten_cost < 0:
            enriched['penny_price_test_status'] = 'UNKNOWN_TIER_10_COST'
            enriched['penny_contract_required_flag'] = 'UNKNOWN'
            eligible.append(enriched)
        elif normalized_price <= tier_ten_cost:
            enriched['penny_price_test_status'] = 'REQUIRED_AT_TIER_10'
            enriched['penny_contract_required_flag'] = 'Y'
            enriched['penny_contract_exclusion_reason'] = 'PENNY_ITEM_CANNOT_ACHIEVE_POSITIVE_TIER_10_MARGIN'
            excluded.append(enriched)
        else:
            enriched['penny_price_test_status'] = 'POSITIVE_TIER_10_MARGIN_POSSIBLE'
            eligible.append(enriched)
    return eligible, excluded


def attach_excluded_reference_context(excluded: list[dict], references: list[dict]) -> list[dict]:
    reference_by_line = {int(row['line_key']): lower_keys(row) for row in references
                         if int(row.get('tier_num') or 0) == 10}
    first_reference_by_line = {int(row['line_key']): lower_keys(row) for row in references
                               if int(row.get('tier_num') or 0) == 1}
    output = []
    for row in excluded:
        reference = reference_by_line.get(row['line_key']) or first_reference_by_line.get(row['line_key'], {})
        active_match = str(reference.get('penny_contract_flag') or '').strip().upper() == 'Y'
        reference_flag = reference.get('penny_contract_flag')
        output.append({**row,
            'prca_num': reference.get('prca_num'),
            'grouped_lvl2': reference.get('grouped_lvl2'),
            'penny_contract_flag': ('Y' if active_match else
                'N' if reference_flag is not None else 'UNKNOWN'),
            'penny_product_types': reference.get('penny_product_types'),
            'penny_man_contract_ids': reference.get('penny_man_contract_ids'),
            'penny_mck_contract_ids': reference.get('penny_mck_contract_ids'),
            'penny_contract_match_count': reference.get('penny_contract_match_count', 0),
            'penny_contract_ambiguous': reference.get('penny_contract_ambiguous', False),
            'penny_customer_status': reference.get('penny_customer_status',
                'UNKNOWN_CUSTOMER_MAPPING' if active_match else 'NOT_PENNY_ITEM'),
            'sell_uom': reference.get('sell_uom'),
            'buy_uom': reference.get('buy_uom'),
            'prmry_uom': reference.get('prmry_uom'),
            'sell_to_buy_divisor': reference.get('sell_to_buy_divisor'),
            'prmry_to_sell_divisor': reference.get('prmry_to_sell_divisor'),
            'prmry_to_buy_divisor': reference.get('prmry_to_buy_divisor'),
            'penny_tier_10_cost': reference.get('tier_cost') if active_match else None,
            'penny_contract_required_flag': 'N',
            'penny_price_test_status': ('NOT_REQUIRED_NONPOSITIVE_OR_INVALID_PRICE'
                                        if active_match else 'NOT_PENNY_ITEM')})
    return output


def read_references(connection, inputs: list[dict]) -> list[dict]:
    if not inputs:
        return []
    statement = (Path(__file__).parent / 'sql' / 'ec_review_enrichment.snowflake.sql').read_text(encoding='utf-8')
    cursor = connection.cursor()
    try:
        cursor.execute(statement, (canonical_json(inputs),))
        columns = [column[0].lower() for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def amount(value: Decimal | None, places: str = '0.01') -> str | None:
    return str(value.quantize(Decimal(places))) if value is not None else None


def calculate_scenarios(context: list[dict], references: list[dict]) -> list[dict]:
    lookup = {}
    for raw in references:
        reference = lower_keys(raw)
        key = (int(reference['line_key']), int(reference['tier_num']))
        if key in lookup:
            raise ValueError('Enrichment returned duplicate line/tier rows')
        lookup[key] = reference
    expected = {(row['line_key'], tier) for row in context for tier in range(1, 11)}
    if set(lookup) != expected:
        raise ValueError('Enrichment must return exactly ten tier rows for every requested line')
    scenarios = []
    for line in context:
        for tier in range(1, 11):
            reference = lookup[(line['line_key'], tier)]
            errors = list(line['source_errors'])
            for field, label in [('customer_matches', 'CUSTOMER'), ('item_matches', 'ITEM'),
                                 ('pex_matches', 'PEX'), ('tapestry_matches', 'TAPESTRY')]:
                if reference.get(field) != 1:
                    errors.append(f'MISSING_OR_AMBIGUOUS_{label}')
            if reference.get('bus_pltfrm') != 'EC':
                errors.append('NOT_CONFIRMED_EC')
            sell_uom = str(reference.get('sell_uom') or '').strip().upper()
            requested_uom = str(line.get('requested_uom') or '').strip().upper()
            assumed_sell_uom = not requested_uom and bool(sell_uom)
            if not sell_uom:
                errors.append('MISSING_SELL_UOM')
            elif requested_uom and requested_uom != sell_uom:
                errors.append('REQUESTED_UOM_MISMATCH')
            price = number(line['requested_price'])
            cost = number(reference.get('tier_cost'))
            current_cost = number(reference.get('current_tier_cost'))
            if cost is None or cost < 0:
                errors.append('MISSING_OR_INVALID_TIER_COST')
            core_valid = not errors
            price_cost_valid = (price is not None and price > 0 and cost is not None and cost >= 0
                                and reference.get('item_matches') == 1
                                and reference.get('tapestry_matches') == 1 and bool(sell_uom)
                                and (not requested_uom or requested_uom == sell_uom))
            impact_errors = []
            penny_flag = str(reference.get('penny_contract_flag') or '').strip().upper()
            penny_customer_status = str(reference.get('penny_customer_status') or '').strip().upper()
            if penny_flag == 'Y' and penny_customer_status == 'Y':
                impact_errors.append('PENNY_CONTRACT_ACTIVITY_EXCLUDED')
            elif penny_flag == 'Y' and penny_customer_status.startswith('UNKNOWN'):
                impact_errors.append('PENNY_CUSTOMER_STATUS_UNKNOWN_FOR_ACTIVITY')
            if reference.get('attachment_matches') != 1 or reference.get('current_tier') not in range(1, 11):
                impact_errors.append('MISSING_OR_AMBIGUOUS_CURRENT_TIER')
            if current_cost is None or current_cost < 0:
                impact_errors.append('MISSING_OR_INVALID_CURRENT_COST')
            quantity = number(reference.get('qty_3_mth'))
            sales = number(reference.get('sls_3_mth'))
            if not reference.get('ipc_all_rows'):
                impact_errors.append('NO_IPC_ACTIVITY')
            elif reference.get('ipc_matched_rows') != reference.get('ipc_all_rows'):
                impact_errors.append('IPC_SELL_UOM_MISMATCH')
            if quantity is None or quantity <= 0:
                impact_errors.append('NO_POSITIVE_COMPLETE_THREE_MONTH_QUANTITY')
            if sales is None or sales < 0:
                impact_errors.append('MISSING_OR_INVALID_THREE_MONTH_SALES')
            if reference.get('negative_activity_rows'):
                impact_errors.append('NEGATIVE_ACTIVITY_REQUIRES_REVIEW')
            cost_delta = spend_delta = profit_delta = None
            if core_valid and not impact_errors:
                cost_delta = (cost - current_cost) * quantity
                spend_delta = price * quantity - sales
                profit_delta = spend_delta - cost_delta
            scenarios.append({
                **line, **reference, 'candidate_tier': tier,
                'status': 'EXCEPTION' if errors else 'CANDIDATE_ONLY_NOT_APPROVED',
                'fixed_price_if_approved': line['requested_price'] if core_valid else None,
                'cost_uom': reference.get('sell_uom'),
                'uom_assumed_from_sell_uom': assumed_sell_uom,
                'requested_margin_at_candidate_cost': amount((price - cost) / price, '0.000001') if price_cost_valid else None,
                'requested_margin_at_current_cost': amount((price - current_cost) / price, '0.000001')
                if price_cost_valid and current_cost is not None and current_cost >= 0 else None,
                'historical_realized_unit_price': amount(sales / quantity, '0.000001')
                if quantity is not None and quantity > 0 and sales is not None else None,
                'cost_change_3m': amount(cost_delta),
                'annualized_cost_change': amount(cost_delta * 4) if cost_delta is not None else None,
                'customer_spend_change_3m': amount(spend_delta),
                'annualized_customer_spend_change': amount(spend_delta * 4) if spend_delta is not None else None,
                'annualized_gross_profit_change_proxy': amount(profit_delta * 4) if profit_delta is not None else None,
                'validation_errors': sorted(set(errors)),
                'impact_exceptions': sorted(set(impact_errors)),
                'recommended_tier': None, 'approval_status': 'NOT_APPROVED',
            })
    return scenarios


def group_scenarios(scenarios: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in scenarios:
        groups[(row['request_id'], row.get('prca_num'), row.get('grouped_lvl2'), row['candidate_tier'])].append(row)
    output = []
    for (request_id, prca, category, tier), rows in groups.items():
        duplicate_items = len({row['item'] for row in rows}) != len(rows)
        result = {'request_id': request_id, 'prca': prca, 'category': category,
                  'candidate_tier': tier, 'requested_line_count': len(rows),
                  'exception_line_count': sum(bool(row['validation_errors']) for row in rows),
                  'impact_unavailable_line_count': sum(row['annualized_cost_change'] is None for row in rows),
                  'aggregation_exception': 'REPEATED_ITEM_IN_REQUEST_GROUP' if duplicate_items else None}
        for field in ('annualized_cost_change', 'annualized_customer_spend_change',
                      'annualized_gross_profit_change_proxy'):
            values = [number(row[field]) for row in rows]
            result[field] = amount(sum(values, Decimal(0))) if not duplicate_items and all(value is not None for value in values) else None
        output.append(result)
    return output


def spreadsheet_cell(value):
    if isinstance(value, list):
        value = '|'.join(value)
    text = '' if value is None else str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) and number(text) is None:
        return "'" + text
    return text


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row if key != 'source_errors'))
    with path.open('x', encoding='utf-8-sig', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: spreadsheet_cell(row.get(key)) for key in fields} for row in rows)


def read_customer_activity(connection, prcas: list[str]) -> list[dict]:
    if not prcas:
        return []
    statement = (Path(__file__).parent / 'sql' / 'ec_customer_activity.snowflake.sql').read_text(encoding='utf-8')
    cursor = connection.cursor()
    try:
        cursor.execute(statement, (json.dumps(prcas),))
        columns = [column[0].lower() for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Existing read-only ETL preview JSON')
    parser.add_argument('--output-dir', type=Path, required=True, help='New private local output directory')
    args = parser.parse_args()
    preview = json.loads(args.input.read_text(encoding='utf-8'))
    if preview.get('mode') != 'READ_ONLY_ETL_PREVIEW_NOT_APPROVED_NOT_LOADABLE':
        parser.error('Input must be an ETL preview, not an approved load artifact')
    if args.output_dir.exists():
        parser.error('Output directory already exists; use a new directory')
    all_inputs, all_context = prepare_requests(preview)
    price_eligible_lines, excluded_unpriced_lines = partition_unpriced_lines(all_context)
    connection = connect_from_environment()
    try:
        all_references = read_references(connection, all_inputs)
        prcas = sorted({numeric_id(row.get('prca_num')) for row in all_references
                        if numeric_id(row.get('prca_num'))})
        activity = read_customer_activity(connection, prcas)
    finally:
        connection.close()
    excluded_unpriced_lines = attach_excluded_reference_context(
        excluded_unpriced_lines, all_references)
    context, excluded_penny_contract_lines = partition_penny_contract_lines(
        price_eligible_lines, all_references)
    eligible_line_keys = {line['line_key'] for line in context}
    references = [row for row in all_references if int(row['line_key']) in eligible_line_keys]
    scenarios = calculate_scenarios(context, references)
    groups = group_scenarios(scenarios)
    from ec_category_review import (category_scenarios, partition_penny_contract_activity,
                                    requested_item_tier_scenarios)

    _, excluded_penny_contract_activity, uncertain_penny_contract_activity = (
        partition_penny_contract_activity(activity))
    report_base = {'lines': context, 'scenarios': scenarios, 'activity': activity}
    category_groups = category_scenarios(report_base, activity)
    request_tier_scenarios = requested_item_tier_scenarios(report_base)
    reference_hash = hashlib.sha256(json.dumps(
        {'item': all_references, 'activity': activity}, default=json_value, sort_keys=True
    ).encode('utf-8')).hexdigest()
    summary = {'requested_headers': len(preview['source']['headers']),
               'requested_lines': len(all_context), 'tier_analyzed_lines': len(context),
               'excluded_unpriced_lines': len(excluded_unpriced_lines),
               'excluded_penny_contract_lines': len(excluded_penny_contract_lines),
               'excluded_penny_contract_activity_rows': len(excluded_penny_contract_activity),
               'uncertain_penny_contract_activity_rows': len(uncertain_penny_contract_activity),
               'candidate_rows': len(scenarios), 'group_tier_scenarios': len(groups),
               'category_scenarios': len(category_groups),
               'request_tier_scenarios': len(request_tier_scenarios),
               'headers_missing_account_reference': sum(not lower_keys(row).get('cust_num_prca') for row in preview['source']['headers']),
               'lines_with_validation_exceptions': len({row['line_key'] for row in scenarios if row['validation_errors']}),
               'lines_with_available_candidate_margin': len({row['line_key'] for row in scenarios if row['requested_margin_at_candidate_cost'] is not None}),
               'lines_with_available_impact': len({row['line_key'] for row in scenarios if row['annualized_cost_change'] is not None}),
               'validation_exception_line_counts': dict(Counter(error for _, error in
                   {(row['line_key'], error) for row in scenarios for error in row['validation_errors']})),
               'impact_exception_line_counts': dict(Counter(error for _, error in
                   {(row['line_key'], error) for row in scenarios for error in row['impact_exceptions']}))}
    report = {'mode': 'INTERNAL_REVIEW_ONLY_NOT_APPROVED_NOT_LOADABLE', 'evaluated_at': utc_now(),
              'source_preview': str(args.input), 'source_hash': preview['source_hash'],
              'reference_hash': reference_hash,
              'summary': summary, 'header_exceptions': preview['header_exceptions'],
              'assumptions': [
                  'Tapestry costs represent item SELL_UOM, as confirmed by the operator.',
                  'IPC QTY_3_MTH is assumed to be in IPC SELL_UOM, following the supplied query; verify before relying on estimates.',
                  'Annualization is three-month activity multiplied by four, not a forecast.',
                  'Customer spend change uses requested price times historical quantity minus historical sales; positive means increased spend.',
                  'Cost change is candidate minus current tier cost times historical quantity; positive means higher internal cost.',
                  'Gross-profit change is a proxy using current tier cost, not historical actual cost.',
                  'Reference costs/attachments are current at extraction, not reconstructed as of the historical request date.',
                  'Active penny items whose normalized requested price cannot exceed Tier 10 cost are excluded from all tier scenarios.',
                  'Confirmed on-contract penny activity is excluded from category baselines and net-impact calculations; unknown customer status blocks impact.',
                  'Category impacts use mapped customer IPC activity; no volume is imputed.',
                  'Do not sum across candidate tiers or overlapping requests; each is an alternative scenario.',
                  'No tier recommendation, approval, loader output, emails or source updates are produced.',
              ], 'lines': context, 'activity': activity,
              'excluded_penny_contract_activity': excluded_penny_contract_activity,
              'uncertain_penny_contract_activity': uncertain_penny_contract_activity,
              'excluded_unpriced_lines': excluded_unpriced_lines,
              'excluded_penny_contract_lines': excluded_penny_contract_lines,
              'references': all_references, 'scenarios': scenarios, 'groups': groups,
              'category_groups': category_groups,
              'request_tier_scenarios': request_tier_scenarios}
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with (args.output_dir / 'review.json').open('x', encoding='utf-8') as destination:
        json.dump(report, destination, default=json_value, indent=2, allow_nan=False)
    write_csv(args.output_dir / 'item_tier_review.csv', scenarios)
    write_csv(args.output_dir / 'excluded_unpriced_lines.csv', excluded_unpriced_lines)
    write_csv(args.output_dir / 'excluded_penny_contract_lines.csv', excluded_penny_contract_lines)
    write_csv(args.output_dir / 'excluded_penny_contract_activity.csv', excluded_penny_contract_activity)
    write_csv(args.output_dir / 'uncertain_penny_contract_activity.csv', uncertain_penny_contract_activity)
    write_csv(args.output_dir / 'customer_category_tier_review.csv', groups)
    print(json.dumps({'mode': report['mode'], 'summary': summary, 'output_directory': str(args.output_dir)}, indent=2))


if __name__ == '__main__':
    main()