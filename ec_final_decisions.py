"""Create immutable per-line decision snapshots for completed sandbox requests."""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ec_etl_preview import connect_from_environment, json_value
from ec_review import (lower_keys, normalize_requested_price_to_sell_uom, number,
                       prepare_requests, spreadsheet_cell)
from ec_workflow import canonical_json, utc_now


DECISION_TABLE = 'PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_FINAL_DECISIONS_ETL_TEST'
DECISION_FIELDS = (
    'decision_snapshot_id', 'decision_record_key', 'record_hash', 'request_id',
    'source_line_key', 'source_ingestion_run_id', 'source_stage', 'source_sheet_id',
    'source_sheet_row_id', 'source_attachment_id', 'source_attachment_name',
    'source_attachment_sha256', 'source_sheet_name', 'source_row_number',
    'completed_flag', 'completed_dt', 'rejected_flag', 'decision_status',
    'decision_reason', 'source_account_number', 'resolved_prca', 'item_e1_num',
    'item_dsc', 'requested_price_raw', 'requested_price', 'requested_uom',
    'sell_uom_price', 'sell_uom', 'buy_uom', 'prmry_uom', 'sell_to_buy_divisor',
    'prmry_to_sell_divisor', 'prmry_to_buy_divisor', 'assigned_tier',
    'tapestry_cost_sell_uom', 'penny_contract_flag', 'penny_contract_required_flag',
    'penny_price_test_status', 'penny_product_types', 'penny_man_contract_ids',
    'penny_mck_contract_ids', 'penny_contract_ambiguous', 'penny_customer_status',
    'source_hash', 'reference_hash', 'snapshot_at',
)


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'true', '1', 'yes', 'y'}


def source_cells(header: dict) -> dict:
    raw = header.get('source_header_json')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {str(key).strip().lower(): value for key, value in raw.items()} if isinstance(raw, dict) else {}


def find_value(values: dict, *names: str):
    for name in names:
        for key, value in values.items():
            if key.replace('_', '').strip().lower() == name.replace('_', '').strip().lower():
                if value not in (None, ''):
                    return value
    return None


def sha256_value(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def build_final_decisions(preview: dict, review: dict, snapshot_at: str | None = None) -> tuple[list[dict], dict]:
    if preview.get('mode') != 'READ_ONLY_ETL_PREVIEW_NOT_APPROVED_NOT_LOADABLE':
        raise ValueError('Input must be a read-only ETL preview')
    if review.get('mode') != 'INTERNAL_REVIEW_ONLY_NOT_APPROVED_NOT_LOADABLE':
        raise ValueError('Review input must be an internal review report')
    if preview.get('source_hash') != review.get('source_hash'):
        raise ValueError('Preview and review source hashes do not match')

    inputs, contexts = prepare_requests(preview)
    headers_by_id = defaultdict(list)
    for raw in preview['source']['headers']:
        header = lower_keys(raw)
        request_id = str(header.get('row_id') or '').strip()
        if request_id:
            headers_by_id[request_id].append(header)
    lines_by_id = defaultdict(list)
    for context in contexts:
        lines_by_id[context['request_id']].append(context)

    completed_ids, rejected_ids, pending_ids = set(), set(), set()
    rejection_by_id = {}
    for request_id, headers in headers_by_id.items():
        if len(headers) != 1:
            if any(as_bool(header.get('cmpltd_flg')) for header in headers):
                raise ValueError(f'Completed request has duplicate source headers: {request_id}')
            pending_ids.add(request_id)
            continue
        header = headers[0]
        cells = source_cells(header)
        rejected = (str(header.get('source_stage') or '').strip().lower() == 'rejected'
                    or as_bool(find_value(cells, 'Rejected')))
        completed = as_bool(header.get('cmpltd_flg'))
        if rejected:
            rejected_ids.add(request_id)
            rejection_by_id[request_id] = (find_value(cells, 'Outbound Notes', 'Rejected Reason')
                or find_value(cells, 'Additional Notes for Awareness')
                or 'Request is marked rejected in Smartsheet.')
        elif completed:
            completed_ids.add(request_id)
        else:
            pending_ids.add(request_id)

    if not completed_ids and not rejected_ids:
        return [], {'completed_requests': 0, 'rejected_requests': 0,
                    'pending_requests': len(pending_ids), 'decision_rows': 0,
                    'approved_rows': 0, 'rejected_rows': 0, 'not_evaluated_rows': 0}

    reference_by_line = {}
    for raw in review.get('references', []):
        reference = lower_keys(raw)
        if int(reference.get('tier_num') or 0) == 10:
            reference_by_line[int(reference['line_key'])] = reference
    classifications = {}
    for collection in ('lines', 'excluded_unpriced_lines', 'excluded_penny_contract_lines'):
        for row in review.get(collection, []):
            classifications[int(row['line_key'])] = row

    raw_lines = [{key.lower(): value for key, value in raw.items()}
                 for raw in preview['source']['lines']]
    evaluated_at = snapshot_at or utc_now()
    records = []
    for context in contexts:
        request_id = context['request_id']
        is_rejected = request_id in rejected_ids
        if request_id not in completed_ids and not is_rejected:
            continue
        header = headers_by_id[request_id][0]
        raw_line = raw_lines[context['line_key'] - 1]
        reference = reference_by_line.get(context['line_key'], {})
        cells = source_cells(header)
        raw_price = raw_line.get('req_price_raw')
        if raw_price is None:
            raw_price = raw_line.get('req_price')
        normalized_context = {**context, 'requested_price': '' if raw_price is None else str(raw_price)}
        sell_uom_price, conversion_factor, normalization_method = normalize_requested_price_to_sell_uom(
            normalized_context, reference)
        assigned_tier = number(reference.get('current_tier'))
        assigned_tier = int(assigned_tier) if assigned_tier is not None and assigned_tier == int(assigned_tier) else None
        assigned_cost = number(reference.get('current_tier_cost'))
        requested_price = number(raw_price)
        errors = list(context.get('source_errors') or [])
        if not is_rejected:
            if reference.get('customer_matches') != 1:
                errors.append('CUSTOMER_MAPPING_MISSING_OR_AMBIGUOUS')
            if reference.get('pex_matches') != 1 or reference.get('bus_pltfrm') != 'EC':
                errors.append('CUSTOMER_NOT_CONFIRMED_EC')
            if reference.get('item_matches') != 1:
                errors.append('ITEM_REFERENCE_MISSING_OR_AMBIGUOUS')
            if reference.get('tapestry_matches') != 1 or not reference.get('grouped_lvl2'):
                errors.append('TAPESTRY_CATEGORY_MISSING_OR_AMBIGUOUS')
            if reference.get('attachment_matches') != 1 or assigned_tier not in range(1, 11):
                errors.append('ASSIGNED_TIER_MISSING_OR_AMBIGUOUS')
            if assigned_cost is None or assigned_cost < 0:
                errors.append('ASSIGNED_TIER_COST_MISSING_OR_INVALID')
            if requested_price is None or requested_price <= 0:
                errors.append('REQUESTED_PRICE_MISSING_OR_INVALID')
            if sell_uom_price is None:
                errors.append(f'SELL_UOM_PRICE_UNAVAILABLE:{normalization_method}')
        if is_rejected:
            status = 'REJECTED'
            reason = str(rejection_by_id[request_id])
        elif errors:
            status = 'NOT_EVALUATED'
            reason = '|'.join(sorted(set(errors)))
        else:
            status = 'APPROVED'
            reason = 'Completed in Smartsheet; assigned tier and Tapestry cost snapshotted.'

        source_hash = preview['source_hash']
        reference_hash = review['reference_hash']
        snapshot_identity = {
            'request_id': request_id,
            'source_ingestion_run_id': header.get('ingestion_run_id'),
            'completed_dt': header.get('cmpltd_dt'),
            'source_sheet_row_id': header.get('source_sheet_row_id'),
        }
        snapshot_id = sha256_value(snapshot_identity)
        line_identity = {
            'request_id': request_id,
            'attachment_sha256': raw_line.get('source_attachment_sha256'),
            'attachment_name': raw_line.get('source_attachment_name'),
            'sheet_name': raw_line.get('source_sheet_name'),
            'row_number': raw_line.get('source_row_number'),
            'item': context.get('item'),
            'requested_uom': context.get('requested_uom'),
            'requested_price_raw': raw_price,
            'line_key_fallback': context['line_key'],
        }
        source_line_key = sha256_value(line_identity)
        classification = classifications.get(context['line_key'], {})
        penny_flag = classification.get('penny_contract_flag', reference.get('penny_contract_flag', 'UNKNOWN'))
        record = {
            'decision_snapshot_id': snapshot_id,
            'decision_record_key': sha256_value([snapshot_id, source_line_key]),
            'record_hash': None,
            'request_id': request_id,
            'source_line_key': source_line_key,
            'source_ingestion_run_id': header.get('ingestion_run_id'),
            'source_stage': header.get('source_stage'),
            'source_sheet_id': header.get('source_sheet_id'),
            'source_sheet_row_id': header.get('source_sheet_row_id'),
            'source_attachment_id': raw_line.get('source_attachment_id'),
            'source_attachment_name': raw_line.get('source_attachment_name'),
            'source_attachment_sha256': raw_line.get('source_attachment_sha256'),
            'source_sheet_name': raw_line.get('source_sheet_name'),
            'source_row_number': raw_line.get('source_row_number'),
            'completed_flag': as_bool(header.get('cmpltd_flg')),
            'completed_dt': header.get('cmpltd_dt'),
            'rejected_flag': is_rejected,
            'decision_status': status,
            'decision_reason': reason,
            'source_account_number': header.get('cust_num_prca'),
            'resolved_prca': reference.get('prca_num'),
            'item_e1_num': context.get('item'),
            'item_dsc': reference.get('item_dsc') or context.get('source_item_description'),
            'requested_price_raw': None if raw_price is None else str(raw_price),
            'requested_price': None if requested_price is None else str(requested_price),
            'requested_uom': context.get('requested_uom'),
            'sell_uom_price': None if sell_uom_price is None else str(sell_uom_price),
            'sell_uom': reference.get('sell_uom'),
            'buy_uom': reference.get('buy_uom'),
            'prmry_uom': reference.get('prmry_uom'),
            'sell_to_buy_divisor': reference.get('sell_to_buy_divisor'),
            'prmry_to_sell_divisor': reference.get('prmry_to_sell_divisor'),
            'prmry_to_buy_divisor': reference.get('prmry_to_buy_divisor'),
            'assigned_tier': assigned_tier,
            'tapestry_cost_sell_uom': None if assigned_cost is None else str(assigned_cost),
            'penny_contract_flag': penny_flag,
            'penny_contract_required_flag': classification.get('penny_contract_required_flag', 'UNKNOWN'),
            'penny_price_test_status': classification.get('penny_price_test_status', 'UNKNOWN'),
            'penny_product_types': reference.get('penny_product_types'),
            'penny_man_contract_ids': reference.get('penny_man_contract_ids'),
            'penny_mck_contract_ids': reference.get('penny_mck_contract_ids'),
            'penny_contract_ambiguous': reference.get('penny_contract_ambiguous'),
            'penny_customer_status': reference.get('penny_customer_status'),
            'source_hash': source_hash,
            'reference_hash': reference_hash,
            'snapshot_at': evaluated_at,
        }
        record['record_hash'] = sha256_value({key: value for key, value in record.items()
                                              if key not in {'record_hash', 'snapshot_at'}})
        records.append(record)

    summary = {
        'completed_requests': len(completed_ids),
        'rejected_requests': len(rejected_ids),
        'pending_requests': len(pending_ids),
        'decision_rows': len(records),
        'approved_rows': sum(row['decision_status'] == 'APPROVED' for row in records),
        'rejected_rows': sum(row['decision_status'] == 'REJECTED' for row in records),
        'not_evaluated_rows': sum(row['decision_status'] == 'NOT_EVALUATED' for row in records),
    }
    return records, summary


def write_rows(path: Path, rows: list[dict]) -> None:
    fields = list(DECISION_FIELDS)
    with path.open('x', encoding='utf-8-sig', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: spreadsheet_cell(row.get(key)) for key in fields} for row in rows)


def append_snapshots(connection, rows: list[dict]) -> dict:
    if not rows:
        return {'applied': False, 'inserted_rows': 0, 'unchanged_snapshots': 0}
    cursor = connection.cursor()
    snapshots = defaultdict(list)
    for row in rows:
        snapshots[row['decision_snapshot_id']].append(row)
    placeholders = ', '.join(['%s'] * len(DECISION_FIELDS))
    columns = ', '.join(field.upper() for field in DECISION_FIELDS)
    inserted_rows = unchanged_snapshots = 0
    try:
        cursor.execute('BEGIN')
        for snapshot_id, snapshot_rows in snapshots.items():
            cursor.execute(f'SELECT DECISION_RECORD_KEY, RECORD_HASH FROM {DECISION_TABLE} '
                           'WHERE DECISION_SNAPSHOT_ID = %s', (snapshot_id,))
            existing = {key: record_hash for key, record_hash in cursor.fetchall()}
            expected = {row['decision_record_key']: row['record_hash'] for row in snapshot_rows}
            if existing:
                if existing != expected:
                    raise ValueError('A final decision snapshot already exists with different rows; create an explicit amendment.')
                unchanged_snapshots += 1
                continue
            cursor.executemany(
                f'INSERT INTO {DECISION_TABLE} ({columns}) VALUES ({placeholders})',
                [tuple(row.get(field) for field in DECISION_FIELDS) for row in snapshot_rows],
            )
            inserted_rows += len(snapshot_rows)
        connection.commit()
        return {'applied': bool(inserted_rows), 'inserted_rows': inserted_rows,
                'unchanged_snapshots': unchanged_snapshots}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', type=Path, required=True, help='Read-only ETL preview JSON')
    parser.add_argument('--review', type=Path, required=True, help='Review JSON created from the same preview')
    parser.add_argument('--output-dir', type=Path, required=True, help='New local output directory')
    parser.add_argument('--apply', action='store_true',
                        help='Append to the isolated final-decision sandbox table only')
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Choose a new output directory; existing outputs are never overwritten')
    preview = json.loads(args.preview.read_text(encoding='utf-8'))
    review = json.loads(args.review.read_text(encoding='utf-8'))
    try:
        rows, summary = build_final_decisions(preview, review)
    except ValueError as error:
        parser.error(str(error))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with (args.output_dir / 'final_decisions.json').open('x', encoding='utf-8') as destination:
        json.dump({'mode': 'FINAL_DECISION_SNAPSHOT_DRY_RUN', 'summary': summary,
                   'source_hash': preview['source_hash'], 'reference_hash': review['reference_hash'],
                   'rows': rows}, destination, default=json_value, indent=2, allow_nan=False)
    write_rows(args.output_dir / 'final_decisions.csv', rows)
    applied = {'applied': False, 'inserted_rows': 0, 'unchanged_snapshots': 0}
    if args.apply:
        connection = connect_from_environment()
        try:
            applied = append_snapshots(connection, rows)
        finally:
            connection.close()
    manifest = {'mode': 'FINAL_DECISION_SNAPSHOT_APPLIED' if applied['applied']
                else 'FINAL_DECISION_SNAPSHOT_DRY_RUN', 'summary': summary,
                'source_hash': preview['source_hash'], 'reference_hash': review['reference_hash'],
                'files': ['final_decisions.json', 'final_decisions.csv'], **applied}
    with (args.output_dir / 'manifest.json').open('x', encoding='utf-8') as destination:
        json.dump(manifest, destination, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()