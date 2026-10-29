"""Read existing ETL tables and produce a local, non-loadable request preview."""

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ec_workflow import canonical_json, utc_now


TABLE_SETS = {
    'source': ('MCKB_REBATE_REQUEST_HEADER', 'MCKB_REBATE_REQUEST_LINES'),
    'pipeline-test': ('EC_REBATE_REQUEST_HEADER_PIPELINE_TEST',
                      'EC_REBATE_REQUEST_LINES_PIPELINE_TEST'),
    'smartsheet-intake-test': ('EC_REBATE_REQUEST_HEADER_ETL_TEST',
                               'EC_REBATE_REQUEST_LINES_ETL_TEST'),
}


def identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('Database and schema must be simple unquoted identifiers')
    return value


def read_etl(connection, database: str, schema: str, since: str, until: str,
             limit: int = 100, segment: str | None = None,
             table_set: str = 'source',
             request_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    if date.fromisoformat(since) >= date.fromisoformat(until):
        raise ValueError('since must precede until; until is exclusive')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Header sample limit must be between 1 and 500')
    if table_set not in TABLE_SETS:
        raise ValueError('Unknown table set; choose source, pipeline-test, or smartsheet-intake-test')
    if request_ids is not None and any(not str(request_id).strip() for request_id in request_ids):
        raise ValueError('Request IDs must be nonempty strings')
    header_table, line_table = TABLE_SETS[table_set]
    latest_intake_snapshot = table_set == 'smartsheet-intake-test'
    prefix = f'{identifier(database)}.{identifier(schema)}'
    header_sql = (
        f'SELECT * FROM {prefix}.{header_table} '
        'WHERE TRY_TO_TIMESTAMP_NTZ(CREATED_DT) >= TO_TIMESTAMP_NTZ(%s) '
        'AND TRY_TO_TIMESTAMP_NTZ(CREATED_DT) < TO_TIMESTAMP_NTZ(%s) '
    )
    parameters = [since, until]
    if segment is not None:
        header_sql += 'AND BUS_PLTFRM_DSC = %s '
        parameters.append(segment)
    if request_ids:
        request_ids = sorted(set(str(request_id).strip() for request_id in request_ids))
        placeholders = ', '.join(['%s'] * len(request_ids))
        header_sql += f'AND ROW_ID IN ({placeholders}) '
        parameters.extend(request_ids)
    if latest_intake_snapshot:
        header_sql += ('QUALIFY ROW_NUMBER() OVER (PARTITION BY ROW_ID '
                       'ORDER BY EXTRACTED_AT DESC, INGESTION_RUN_ID DESC) = 1 ')
    header_sql += 'ORDER BY TRY_TO_TIMESTAMP_NTZ(CREATED_DT), ROW_ID LIMIT %s'
    parameters.append(limit)
    cursor = connection.cursor()
    try:
        cursor.execute(header_sql, tuple(parameters))
        columns = [column[0] for column in cursor.description]
        headers = [dict(zip(columns, row)) for row in cursor.fetchall()]
        request_ids = sorted({str(value) for row in headers
                              for key, value in row.items()
                              if key.lower() == 'row_id' and value is not None and str(value).strip()})
        if not request_ids:
            return headers, []
        if latest_intake_snapshot:
            header_by_id = {str(next(value for key, value in row.items() if key.lower() == 'row_id')):
                            {key.lower(): value for key, value in row.items()} for row in headers}
            if any(not header_by_id[request_id].get('ingestion_run_id') for request_id in request_ids):
                raise ValueError('Smartsheet intake header is missing INGESTION_RUN_ID')
            pair_filters = ' OR '.join('(ROW_ID = %s AND INGESTION_RUN_ID = %s)'
                                       for _ in request_ids)
            parameters = tuple(value for request_id in request_ids
                               for value in (request_id, header_by_id[request_id]['ingestion_run_id']))
            line_sql = (f'SELECT * FROM {prefix}.{line_table} WHERE ({pair_filters}) '
                        'ORDER BY ROW_ID, ITEM_E1_NUM, REQ_UOM, REQ_PRICE')
        else:
            placeholders = ', '.join(['%s'] * len(request_ids))
            parameters = tuple(request_ids)
            line_sql = (f'SELECT * FROM {prefix}.{line_table} '
                        f'WHERE ROW_ID IN ({placeholders}) '
                        'ORDER BY ROW_ID, ITEM_E1_NUM, REQ_UOM, REQ_PRICE')
        cursor.execute(line_sql, parameters)
        columns = [column[0] for column in cursor.description]
        lines = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return headers, lines
    finally:
        cursor.close()


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f'Unsupported source value type: {type(value).__name__}')


def build_preview(headers: list[dict], lines: list[dict]) -> dict:
    source = json.loads(json.dumps({'headers': headers, 'lines': lines}, default=json_value,
                                  allow_nan=False))
    header_counts = Counter(str(row.get('row_id', '')) for row in
                            ({key.lower(): value for key, value in raw.items()}
                             for raw in source['headers']))
    results = []
    seen = Counter()
    for position, raw in enumerate(source['lines'], start=1):
        line = {key.lower(): value for key, value in raw.items()}
        request_id = str(line.get('row_id') or '')
        errors = []
        if not request_id or header_counts[request_id] == 0:
            errors.append('MISSING_HEADER')
        elif header_counts[request_id] > 1:
            errors.append('AMBIGUOUS_HEADER')
        item = str(line.get('item_e1_num') or '')
        normalized_item = None
        try:
            number = Decimal(item)
            if not number.is_finite() or number <= 0 or number != number.to_integral_value():
                raise InvalidOperation
            normalized_item = format(number, 'f').split('.')[0]
        except InvalidOperation:
            errors.append('INVALID_ITEM')
        requested_price = str(line.get('req_price') if line.get('req_price') is not None else '')
        try:
            price = Decimal(requested_price)
            if not price.is_finite() or price <= 0:
                raise InvalidOperation
        except InvalidOperation:
            errors.append('INVALID_PRICE')
        if not str(line.get('req_uom') or '').strip():
            errors.append('MISSING_REQUESTED_UOM')
        fingerprint = hashlib.sha256(canonical_json(raw).encode('utf-8')).hexdigest()
        seen[fingerprint] += 1
        results.append({
            'preview_row_number': position, 'request_id': request_id,
            'source_row_hash': fingerprint, 'duplicate_occurrence': seen[fingerprint],
            'item': normalized_item, 'requested_uom': line.get('req_uom'),
            'requested_price': requested_price,
            'fixed_price_if_approved': requested_price if not errors else None,
            'status': 'INVALID_SOURCE_DATA' if errors else 'AWAITING_REFERENCE_ENRICHMENT',
            'validation_errors': errors,
        })
    line_counts = Counter(row['request_id'] for row in results)
    header_exceptions = []
    for position, raw in enumerate(source['headers'], start=1):
        header = {key.lower(): value for key, value in raw.items()}
        request_id = str(header.get('row_id') or '')
        errors = []
        if not request_id:
            errors.append('MISSING_REQUEST_ID')
        if header_counts[request_id] > 1:
            errors.append('DUPLICATE_HEADER_REQUEST_ID')
        if not line_counts[request_id]:
            errors.append('NO_STORED_LINES')
        if errors:
            header_exceptions.append({'preview_header_number': position,
                                      'request_id': request_id, 'errors': errors})
    return {
        'mode': 'READ_ONLY_ETL_PREVIEW_NOT_APPROVED_NOT_LOADABLE',
        'extracted_at': utc_now(),
        'source_hash': hashlib.sha256(canonical_json(source).encode('utf-8')).hexdigest(),
        'summary': {'header_rows': len(headers), 'line_rows': len(lines),
                    'invalid_lines': sum(bool(row['validation_errors']) for row in results),
                    'header_exceptions': len(header_exceptions)},
        'limitations': [
            'EC eligibility is not established from sales segment alone.',
            'No cost, reference UOM, tier, customer-impact or pricing-rule evaluation has run.',
            'No decisions, load files, source updates, emails or production loads are performed.',
            'Preview row numbers and duplicate occurrences are not durable source-line IDs.',
            'Rows removed or UOM text modified by the ETL cannot be reconstructed here.',
            'The ETL stores REQ_PRICE as FLOAT; original attachment precision is not restored.',
        ],
        'source': source, 'lines': results, 'header_exceptions': header_exceptions,
    }


def connect_from_environment():
    import snowflake.connector

    user = os.environ.get('SNOWFLAKE_USER', '')
    if not user:
        raise ValueError('SNOWFLAKE_USER is required; use your approved environment configuration')
    settings = {
        'user': user,
        'account': os.environ.get('SNOWFLAKE_ACCOUNT', 'fr99188.west-us-2.azure'),
        'database': identifier(os.environ.get('SNOWFLAKE_DATABASE', 'PRD_SBX_MMS_DB')),
        'schema': identifier(os.environ.get('SNOWFLAKE_SCHEMA', 'PRC')),
        'login_timeout': 300, 'network_timeout': 120,
        'session_parameters': {'QUERY_TAG': 'EC_READ_ONLY_LOCAL_PREVIEW',
                               'STATEMENT_TIMEOUT_IN_SECONDS': 120},
    }
    authenticator = os.environ.get('SNOWFLAKE_AUTHENTICATOR', '')
    if authenticator:
        settings['authenticator'] = authenticator
    if os.environ.get('SNOWFLAKE_PASSWORD'):
        settings['password'] = os.environ['SNOWFLAKE_PASSWORD']
    elif authenticator != 'externalbrowser':
        raise ValueError('Configure SNOWFLAKE_PASSWORD or SNOWFLAKE_AUTHENTICATOR=externalbrowser')
    for option in ('role', 'warehouse'):
        if os.environ.get(f'SNOWFLAKE_{option.upper()}'):
            settings[option] = os.environ[f'SNOWFLAKE_{option.upper()}']
    return snowflake.connector.connect(**settings)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', required=True, help='Inclusive header creation date YYYY-MM-DD')
    parser.add_argument('--until', required=True, help='Exclusive header creation date YYYY-MM-DD')
    parser.add_argument('--limit', type=int, default=100, help='Maximum header rows, at most 500')
    parser.add_argument('--segment', help='Optional exact sales-segment filter; not proof of EC eligibility')
    parser.add_argument('--request-id', action='append', default=[],
                        help='Limit preview to this request ID; repeat for multiple requests')
    parser.add_argument('--table-set', choices=TABLE_SETS, default='source',
                        help='Read original ETL tables or isolated pipeline-test tables')
    parser.add_argument('--output', required=True, type=Path, help='New local JSON file; never overwritten')
    args = parser.parse_args()
    if date.fromisoformat(args.since) >= date.fromisoformat(args.until) or not 1 <= args.limit <= 500:
        parser.error('Use since < until and limit between 1 and 500')
    if args.output.exists():
        parser.error('Output already exists; choose a new filename')
    connection = connect_from_environment()
    try:
        headers, lines = read_etl(
            connection, os.environ.get('SNOWFLAKE_DATABASE', 'PRD_SBX_MMS_DB'),
            os.environ.get('SNOWFLAKE_SCHEMA', 'PRC'), args.since, args.until, args.limit,
            args.segment, args.table_set, args.request_id or None,
        )
    finally:
        connection.close()
    preview = build_preview(headers, lines)
    preview['selection'] = {'since_inclusive': args.since, 'until_exclusive': args.until,
                            'header_limit': args.limit, 'sales_segment': args.segment,
                            'request_ids': sorted(set(args.request_id)),
                            'missing_request_ids': sorted(
                                set(args.request_id) - {str(row.get('row_id')) for row in headers}),
                            'table_set': args.table_set,
                            'header_table': TABLE_SETS[args.table_set][0],
                            'line_table': TABLE_SETS[args.table_set][1],
                            'database': os.environ.get('SNOWFLAKE_DATABASE', 'PRD_SBX_MMS_DB'),
                            'schema': os.environ.get('SNOWFLAKE_SCHEMA', 'PRC')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as destination:
        json.dump(preview, destination, indent=2, allow_nan=False)
    print(json.dumps({'mode': preview['mode'], 'summary': preview['summary'],
                      'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()