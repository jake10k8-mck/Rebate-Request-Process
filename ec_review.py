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


def attach_excluded_reference_context(excluded: list[dict], references: list[dict]) -> list[dict]:
    reference_by_line = {int(row['line_key']): lower_keys(row) for row in references
                         if int(row.get('tier_num') or 0) == 1}
    return [{**row,
             'prca_num': reference_by_line.get(row['line_key'], {}).get('prca_num'),
             'grouped_lvl2': reference_by_line.get(row['line_key'], {}).get('grouped_lvl2')}
            for row in excluded]


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
    context, excluded_unpriced_lines = partition_unpriced_lines(all_context)
    eligible_line_keys = {line['line_key'] for line in context}
    connection = connect_from_environment()
    try:
        all_references = read_references(connection, all_inputs)
    finally:
        connection.close()
    excluded_unpriced_lines = attach_excluded_reference_context(
        excluded_unpriced_lines, all_references)
    references = [row for row in all_references if int(row['line_key']) in eligible_line_keys]
    scenarios = calculate_scenarios(context, references)
    groups = group_scenarios(scenarios)
    summary = {'requested_headers': len(preview['source']['headers']),
               'requested_lines': len(all_context), 'tier_analyzed_lines': len(context),
               'excluded_unpriced_lines': len(excluded_unpriced_lines),
               'candidate_rows': len(scenarios), 'group_tier_scenarios': len(groups),
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
              'reference_hash': hashlib.sha256(json.dumps(references, default=json_value, sort_keys=True).encode('utf-8')).hexdigest(),
              'summary': summary, 'header_exceptions': preview['header_exceptions'],
              'assumptions': [
                  'Tapestry costs represent item SELL_UOM, as confirmed by the operator.',
                  'IPC QTY_3_MTH is assumed to be in IPC SELL_UOM, following the supplied query; verify before relying on estimates.',
                  'Annualization is three-month activity multiplied by four, not a forecast.',
                  'Customer spend change uses requested price times historical quantity minus historical sales; positive means increased spend.',
                  'Cost change is candidate minus current tier cost times historical quantity; positive means higher internal cost.',
                  'Gross-profit change is a proxy using current tier cost, not historical actual cost.',
                  'Reference costs/attachments are current at extraction, not reconstructed as of the historical request date.',
                  'Groups cover requested items only; category-wide effects on other items are not assessed.',
                  'Do not sum across candidate tiers or overlapping requests; each is an alternative scenario.',
                  'No tier recommendation, approval, loader output, emails or source updates are produced.',
              ], 'lines': context, 'excluded_unpriced_lines': excluded_unpriced_lines,
              'references': all_references, 'scenarios': scenarios, 'groups': groups}
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with (args.output_dir / 'review.json').open('x', encoding='utf-8') as destination:
        json.dump(report, destination, default=json_value, indent=2, allow_nan=False)
    write_csv(args.output_dir / 'item_tier_review.csv', scenarios)
    write_csv(args.output_dir / 'excluded_unpriced_lines.csv', excluded_unpriced_lines)
    write_csv(args.output_dir / 'customer_category_tier_review.csv', groups)
    print(json.dumps({'mode': report['mode'], 'summary': summary, 'output_directory': str(args.output_dir)}, indent=2))


if __name__ == '__main__':
    main()