"""Append October snapshot requests to the fixed pipeline-test tables only."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ec_etl_preview import connect_from_environment, json_value
from ec_workflow import utc_now


SOURCE_TABLE = 'PRD_SBX_MMS_DB.PRC.MCKB_RBT_TEST_OCT_2026'
HEADER_TABLE = 'PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_HEADER_PIPELINE_TEST'
LINE_TABLE = 'PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_LINES_PIPELINE_TEST'
TEST_TABLES = {
    'pipeline-test': (HEADER_TABLE, LINE_TABLE),
    'smartsheet-intake-test': (
        'PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_HEADER_ETL_TEST',
        'PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_LINES_ETL_TEST',
    ),
}
HEADER_FIELDS = ('row_id', 'created_dt', 'cust_num_prca', 'bus_pltfrm_dsc')
LINE_FIELDS = ('row_id', 'item_e1_num', 'item_dsc', 'req_uom', 'req_price')


def lower(row: dict) -> dict:
    return {key.lower(): value for key, value in row.items()}


def numeric_text(value):
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Nonfinite source number')
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def created_text(value):
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.isoformat(sep=' ', timespec='microseconds')


def fetch(connection, statement: str, parameters=()) -> list[dict]:
    cursor = connection.cursor()
    try:
        cursor.execute(statement, parameters)
        fields = [column[0].lower() for column in cursor.description]
        return [dict(zip(fields, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def read_snapshot(connection, since: str, until: str) -> list[dict]:
    rows = fetch(connection, f'SELECT * FROM {SOURCE_TABLE} '
                 'WHERE CREATED_DATE >= TO_TIMESTAMP_NTZ(%s) AND CREATED_DATE < TO_TIMESTAMP_NTZ(%s) '
                 'ORDER BY REQUEST_ID, ITEM_E1_NUM, UOM_PROVIDED, FUTURE_SELL_PRICE', (since, until))
    accounts = sorted({numeric_text(row['account_number']) for row in rows
                       if row.get('account_number') is not None})
    platforms = defaultdict(set)
    if accounts:
        mappings = fetch(connection,
            'SELECT DISTINCT customer.CUST_E1_NUM, pex.BUS_PLTFRM '
            'FROM PRD_PL_MMS_EDW_DB.EDWRPT.V_DIM_CUST_CURR customer '
            'LEFT JOIN PRD_SBX_MMS_DB.PRC.PEX_PRCA_VIEW pex ON pex.PRCA_NUM = customer.PRCA_NUM '
            'WHERE customer.SYS_PLTFRM = %s AND customer.CUST_TYPE_CD IN (%s, %s, %s) '
            'AND customer.CUST_E1_NUM IN '
            '(SELECT value::NUMBER FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))))',
            ('E1', 'B', 'S', 'X', json.dumps(accounts)))
        for mapping in mappings:
            platforms[numeric_text(mapping['cust_e1_num'])].add(mapping.get('bus_pltfrm'))
    for row in rows:
        matches = platforms[numeric_text(row.get('account_number'))]
        row['resolved_platform'] = next(iter(matches)) if len(matches) == 1 else None
    return rows


def normalize_snapshot(rows: list[dict], platform: str = 'EC') -> tuple[list[dict], list[dict], dict]:
    grouped = defaultdict(list)
    for raw in rows:
        row = lower(raw)
        request_id = str(row.get('request_id') or '').strip()
        if not request_id:
            raise ValueError('Snapshot contains a missing request ID')
        grouped[request_id].append(row)
    headers, lines = [], []
    skipped = unresolved = 0
    for request_id, requested in sorted(grouped.items()):
        attributes = {(numeric_text(row.get('account_number')), created_text(row.get('created_date')),
                       row.get('resolved_platform')) for row in requested}
        if len(attributes) != 1:
            raise ValueError(f'Conflicting account/date/platform for request {request_id}')
        account, created, actual_platform = next(iter(attributes))
        if actual_platform is not None and actual_platform != platform:
            skipped += 1
            continue
        unresolved += int(actual_platform is None)
        headers.append({'row_id': request_id, 'cust_num_prca': account, 'created_dt': created,
                        'bus_pltfrm_dsc': 'Extended Care' if actual_platform == 'EC' else None})
        for row in requested:
            price = row.get('future_sell_price')
            if price is not None and numeric_text(price) != numeric_text(float(price)):
                raise ValueError(f'Price cannot be preserved in test FLOAT column: {request_id}')
            lines.append({'row_id': request_id, 'item_e1_num': numeric_text(row.get('item_e1_num')),
                          'item_dsc': None, 'req_uom': row.get('uom_provided'), 'req_price': price})
    return headers, lines, {'source_requests': len(grouped), 'skipped_other_platform_requests': skipped,
                            'unresolved_platform_requests_retained': unresolved}


def header_signature(row: dict) -> tuple:
    row = lower(row)
    return (str(row.get('row_id')), created_text(row.get('created_dt')),
            numeric_text(row.get('cust_num_prca')), row.get('bus_pltfrm_dsc'))


def line_signature(row: dict) -> tuple:
    row = lower(row)
    return (str(row.get('row_id')), numeric_text(row.get('item_e1_num')),
            row.get('item_dsc'), row.get('req_uom'), numeric_text(row.get('req_price')))


def plan_append(headers: list[dict], lines: list[dict], existing_headers: list[dict],
                existing_lines: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    old_headers, old_lines, new_lines = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in existing_headers:
        old_headers[str(lower(row).get('row_id'))].append(row)
    for row in existing_lines:
        old_lines[str(lower(row).get('row_id'))].append(row)
    for row in lines:
        new_lines[row['row_id']].append(row)
    insert_headers, insert_lines, skipped = [], [], []
    for header in headers:
        request_id = header['row_id']
        previous = old_headers[request_id]
        if previous or old_lines[request_id]:
            same_header = len(previous) == 1 and header_signature(previous[0]) == header_signature(header)
            same_lines = Counter(map(line_signature, old_lines[request_id])) == Counter(map(line_signature, new_lines[request_id]))
            if not same_header or not same_lines:
                raise ValueError(f'Existing test request differs; no overwrite allowed: {request_id}')
            skipped.append(request_id)
        else:
            insert_headers.append(header)
            insert_lines.extend(new_lines[request_id])
    return insert_headers, insert_lines, skipped


def rows_hash(rows: list[dict]) -> str:
    encoded = sorted(json.dumps(lower(row), sort_keys=True, default=json_value) for row in rows)
    return hashlib.sha256('\n'.join(encoded).encode('utf-8')).hexdigest()


def append_test_inputs(connection, headers: list[dict], lines: list[dict], apply: bool = False,
                       table_set: str = 'pipeline-test') -> dict:
    if table_set not in TEST_TABLES:
        raise ValueError('Unknown test table set; choose pipeline-test or smartsheet-intake-test')
    header_table, line_table = TEST_TABLES[table_set]
    cursor = connection.cursor()
    try:
        cursor.execute('BEGIN')
        old_headers = fetch(connection, f'SELECT * FROM {header_table}')
        old_lines = fetch(connection, f'SELECT * FROM {line_table}')
        new_headers, new_lines, skipped = plan_append(headers, lines, old_headers, old_lines)
        untouched_ids = {str(row['row_id']) for row in old_headers} - {row['row_id'] for row in headers}
        before = [rows_hash([row for row in old_headers if str(row['row_id']) in untouched_ids]),
                  rows_hash([row for row in old_lines if str(row['row_id']) in untouched_ids])]
        result = {'planned_headers': len(new_headers), 'planned_lines': len(new_lines),
                  'unchanged_requests_skipped': len(skipped), 'applied': False,
                  'preserved_other_requests': len(untouched_ids),
                  'target_tables': [header_table, line_table]}
        if not apply:
            connection.rollback()
            return result
        for table, fields, records in ((header_table, HEADER_FIELDS, new_headers),
                        (line_table, LINE_FIELDS, new_lines)):
            if records:
                columns = ', '.join(field.upper() for field in fields)
                placeholders = ', '.join(['%s'] * len(fields))
                cursor.executemany(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})',
                                   [tuple(row[field] for field in fields) for row in records])
        after_headers = fetch(connection, f'SELECT * FROM {header_table}')
        after_lines = fetch(connection, f'SELECT * FROM {line_table}')
        pending_headers, pending_lines, verified = plan_append(headers, lines, after_headers, after_lines)
        after = [rows_hash([row for row in after_headers if str(row['row_id']) in untouched_ids]),
                 rows_hash([row for row in after_lines if str(row['row_id']) in untouched_ids])]
        if pending_headers or pending_lines or before != after or len(verified) != len(headers):
            raise ValueError('Post-insert reconciliation failed')
        connection.commit()
        return {**result, 'applied': True, 'unchanged_requests_verified': True,
                'replay_would_insert_headers': 0, 'replay_would_insert_lines': 0}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True, help='Exclusive YYYY-MM-DD')
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Append to the fixed test tables only')
    args = parser.parse_args()
    if date.fromisoformat(args.since) >= date.fromisoformat(args.until):
        parser.error('since must precede until')
    if args.output_dir.exists():
        parser.error('Choose a new output directory')
    connection = connect_from_environment()
    try:
        source = read_snapshot(connection, args.since, args.until)
        if not source:
            raise ValueError('No source rows in the requested period; no changes made')
        headers, lines, coverage = normalize_snapshot(source)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        source_hash = rows_hash(source)
        with (args.output_dir / 'source_snapshot.json').open('x', encoding='utf-8') as destination:
            json.dump({'source_table': SOURCE_TABLE, 'source_hash': source_hash, 'rows': source},
                      destination, default=json_value, indent=2)
        result = append_test_inputs(connection, headers, lines, args.apply)
        report = {'mode': 'TEST_INPUT_APPEND_ONLY_NO_PRICING_LOAD', 'processed_at': utc_now(),
                  'source_table': SOURCE_TABLE, 'source_hash': source_hash,
                  'since_inclusive': args.since, 'until_exclusive': args.until,
                  'source_rows': len(source), 'selected_headers': len(headers),
                  'selected_lines': len(lines), **coverage, **result,
                  'limitations': ['Derived headers contain only verified account/date/platform; other metadata remains null.',
                                  'Original notes and rationale retained in source_snapshot.json.',
                                  'Run as a single writer; ordinary Snowflake tables do not enforce unique request IDs.',
                                  'Source costs, tiers, requests and production prices are not modified.']}
        with (args.output_dir / 'manifest.json').open('x', encoding='utf-8') as destination:
            json.dump(report, destination, indent=2)
        print(json.dumps(report, indent=2))
    finally:
        connection.close()


if __name__ == '__main__':
    main()