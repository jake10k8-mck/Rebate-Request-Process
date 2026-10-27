"""Build a read-only all-October EC request and category review package."""

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from ec_category_review import category_scenarios, requested_item_tier_scenarios
from ec_customer_workbooks import export_customers
from ec_etl_preview import build_preview, connect_from_environment, json_value
from ec_review import (calculate_scenarios, group_scenarios, numeric_id,
                       attach_excluded_reference_context, partition_unpriced_lines,
                       prepare_requests, read_references)
from ec_test_inputs import SOURCE_TABLE, normalize_snapshot, read_snapshot, rows_hash
from ec_workflow import utc_now


def fetch_customer_activity(connection, prcas: list[str]) -> list[dict]:
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


def parse_request_pairs(values: list[str]) -> dict[str, str]:
    targets = {}
    for value in values:
        request_id, separator, account_value = value.partition('=')
        account_number = numeric_id(account_value.strip()) if separator else None
        request_id = request_id.strip()
        if not request_id or account_number is None:
            raise ValueError('Each --request value must be REQUEST_ID=ACCOUNT_NUMBER')
        account_number = str(account_number)
        if request_id in targets and targets[request_id] != account_number:
            raise ValueError(f'Conflicting account numbers supplied for request {request_id}')
        targets[request_id] = account_number
    return targets


def select_target_rows(source_rows: list[dict], targets: dict[str, str] | None) -> list[dict]:
    if not targets:
        return source_rows
    found_ids = {str(row.get('request_id')) for row in source_rows if row.get('request_id')}
    missing_ids = sorted(set(targets) - found_ids)
    if missing_ids:
        raise ValueError(f'Requests absent from October source snapshot: {", ".join(missing_ids)}')
    selected_rows = [row for row in source_rows if str(row.get('request_id')) in targets]
    for row in selected_rows:
        request_id = str(row.get('request_id'))
        actual_account = numeric_id(row.get('account_number'))
        if actual_account != targets[request_id]:
            raise ValueError(f'Request {request_id} account does not match supplied account')
    return selected_rows


def analyze_snapshot(connection, since: str, until: str,
                     targets: dict[str, str] | None = None) -> tuple[dict, dict]:
    source_rows = read_snapshot(connection, since, until)
    if not source_rows:
        raise ValueError('No October source rows found in the selected date range')
    source_rows = select_target_rows(source_rows, targets)

    all_request_ids = {str(row.get('request_id')) for row in source_rows if row.get('request_id')}
    ec_rows = [row for row in source_rows if row.get('resolved_platform') == 'EC']
    ec_request_ids = {str(row.get('request_id')) for row in ec_rows if row.get('request_id')}
    unresolved_request_ids = {str(row.get('request_id')) for row in source_rows
                              if row.get('resolved_platform') is None and row.get('request_id')}
    other_platform_request_ids = all_request_ids - ec_request_ids - unresolved_request_ids
    if not ec_rows:
        raise ValueError('No source requests have a uniquely confirmed EC platform mapping')

    headers, lines, normalization = normalize_snapshot(ec_rows, platform='EC')
    preview = build_preview(headers, lines)
    preview['mode'] = 'READ_ONLY_ETL_PREVIEW_NOT_APPROVED_NOT_LOADABLE'
    inputs, all_context = prepare_requests(preview)
    context, excluded_unpriced_lines = partition_unpriced_lines(all_context)
    eligible_line_keys = {row['line_key'] for row in context}
    all_references = read_references(connection, inputs)
    references = [row for row in all_references if row.get('line_key') in eligible_line_keys]
    excluded_unpriced_lines = attach_excluded_reference_context(
        excluded_unpriced_lines, all_references)
    scenarios = calculate_scenarios(context, references)
    for row in scenarios:
        row['analyst_benchmark_tier'] = row.get('current_tier')
        row['analyst_tier_assumption'] = 'CURRENT_ASSIGNED_EC_TIER_ASSUMED_ANALYST_ASSIGNMENT'

    prcas = sorted({numeric_id(row.get('prca_num')) for row in all_references
                    if numeric_id(row.get('prca_num'))})
    activity = fetch_customer_activity(connection, prcas)
    report_base = {
        'mode': 'INTERNAL_REVIEW_ONLY_NOT_APPROVED_NOT_LOADABLE',
        'evaluated_at': utc_now(),
        'source_hash': rows_hash(source_rows),
        'lines': context,
        'excluded_unpriced_lines': excluded_unpriced_lines,
        'scenarios': scenarios,
        'groups': group_scenarios(scenarios),
        'activity': activity,
        'analyst_tier_assumption': (
            'Current assigned EC tier at extraction is assumed to equal the analyst-assigned tier for the October request; '
            'historical October attachment state is not independently reconstructed.'),
        'assumptions': [
            'All source requests with a unique confirmed EC platform mapping are included; unresolved and non-EC requests are counted but excluded.',
            'Current assigned EC tier at extraction is used as the assumed analyst tier for each request/category.',
            'Request-tier suggestion uses requested prices and candidate costs, with one sell unit per distinct requested SKU because requested quantities are unavailable.',
            'Duplicate source lines are preserved in requested-line outputs and counted once per distinct item in request-tier margins.',
            'Category impact uses customer IPC activity; requested price replaces IPC revenue only for requested items with positive comparable activity.',
            'No volume is assumed for requested items without IPC activity; all missing and UOM-incompatible values remain explicit exceptions.',
            'Annualization is three-month activity multiplied by four, not a forecast.',
            'No email, approval, load, or source-system write is performed.',
        ],
    }
    category_rows = category_scenarios(report_base, activity)
    request_tier_rows = requested_item_tier_scenarios(report_base)
    reference_hash = hashlib.sha256(json.dumps(
        {'item': references, 'activity': activity}, default=json_value, sort_keys=True
    ).encode('utf-8')).hexdigest()
    metadata = {
        'source_table': SOURCE_TABLE,
        'since_inclusive': since,
        'until_exclusive': until,
        'source_rows': len(source_rows),
        'source_requests': len(all_request_ids),
        'confirmed_ec_source_requests': len(ec_request_ids),
        'unresolved_platform_requests_excluded': len(unresolved_request_ids),
        'non_ec_requests_excluded': len(other_platform_request_ids),
        'selected_ec_lines': len(all_context),
        'tier_analyzed_lines': len(context),
        'excluded_unpriced_lines': len(excluded_unpriced_lines),
        'selected_ec_customers': len(prcas),
        'candidate_item_rows': len(scenarios),
        'category_scenarios': len(category_rows),
        'request_tier_scenarios': len(request_tier_rows),
        'normalization': normalization,
        'source_hash': report_base['source_hash'],
        'reference_hash': reference_hash,
        'current_tiers_missing': sum(row.get('current_tier') is None for row in scenarios
                                     if row.get('candidate_tier') == 1),
    }
    report = {**report_base, 'category_groups': category_rows,
              'request_tier_scenarios': request_tier_rows,
              'reference_hash': reference_hash,
              'coverage': 'ALL_CONFIRMED_EC_OCTOBER_REQUESTS_PLUS_MAPPED_CUSTOMER_CATEGORY_ACTIVITY',
              'batch_metadata': metadata}
    return report, {'source_rows': source_rows, 'preview': preview, 'metadata': metadata}


def main() -> None:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default=today.replace(day=1).isoformat(),
                        help='Inclusive request creation date (defaults to first day of current month)')
    parser.add_argument('--until', default=(today + timedelta(days=1)).isoformat(),
                        help='Exclusive request creation date (defaults to tomorrow)')
    parser.add_argument('--request', action='append', default=[], metavar='REQUEST_ID=ACCOUNT_NUMBER',
                        help='Limit review to a verified request/account pair; repeat for multiple requests')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if date.fromisoformat(args.since) >= date.fromisoformat(args.until):
        parser.error('--since must precede --until')
    try:
        targets = parse_request_pairs(args.request)
    except ValueError as error:
        parser.error(str(error))
    if args.output_dir.exists():
        parser.error('Choose a new output directory; existing review files are never overwritten')

    connection = connect_from_environment()
    try:
        report, source = analyze_snapshot(connection, args.since, args.until, targets or None)
    finally:
        connection.close()

    output_dir = args.output_dir
    export_manifest = export_customers(report, output_dir)
    with (output_dir / 'review.json').open('x', encoding='utf-8') as destination:
        json.dump(report, destination, default=json_value, indent=2)
    with (output_dir / 'source_snapshot.json').open('x', encoding='utf-8') as destination:
        json.dump({'source_table': SOURCE_TABLE, 'source_hash': source['metadata']['source_hash'],
                   'rows': source['source_rows']}, destination, default=json_value, indent=2)
    manifest = {**export_manifest, **source['metadata'],
                'analyst_tier_assumption': report['analyst_tier_assumption'],
                'files': export_manifest['files'] + [
                    {'filename': 'review.json'}, {'filename': 'source_snapshot.json'}]}
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'output_directory': str(output_dir), **source['metadata'],
                      'customers_exported': export_manifest['customers'],
                      'email_sent': False, 'production_writes': False}, indent=2))


if __name__ == '__main__':
    main()