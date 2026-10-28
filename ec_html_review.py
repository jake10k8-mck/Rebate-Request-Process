"""Export a local, static HTML review from an internal request review JSON."""

import argparse
import hashlib
import html
import json
import re
from collections import defaultdict
from pathlib import Path


def display(value) -> str:
    if value is None or value == '':
        return 'Unavailable'
    if isinstance(value, list):
        return '; '.join(str(entry) for entry in value) or 'None'
    return str(value)


def cell(value) -> str:
    return html.escape(display(value), quote=True)


def table(headers: list[str], rows: list[list], empty_message: str) -> str:
    if not rows:
        return f'<p class="empty">{html.escape(empty_message)}</p>'
    head = ''.join(f'<th scope="col">{html.escape(label)}</th>' for label in headers)
    body = ''.join('<tr>' + ''.join(f'<td>{cell(value)}</td>' for value in row) + '</tr>'
                   for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def request_filename(request_id: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', request_id).strip('-') or 'request'
    digest = hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:8]
    return f'request-{slug}-{digest}.html'


def style() -> str:
    return '''
    :root { color-scheme: light; --ink: #1d2b2d; --muted: #526365; --paper: #f3f6f3;
      --panel: #ffffff; --line: #cbd6d1; --teal: #0e625b; --pale: #e5f1ed;
      --warn: #7d3c16; --warn-bg: #fff1df; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--paper); color: var(--ink);
      font: 15px/1.5 "Segoe UI", "Aptos", sans-serif; }
    main { width: min(1440px, 100%); margin: 0 auto; padding: 28px clamp(16px, 3vw, 42px) 56px; }
    h1, h2, h3 { line-height: 1.2; }
    h1 { margin: 0 0 8px; font-size: 30px; }
    h2 { margin: 34px 0 12px; font-size: 21px; }
    h3 { margin: 24px 0 10px; font-size: 16px; }
    p { margin: 8px 0 14px; }
    a { color: var(--teal); }
    .eyebrow { color: var(--teal); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .status { margin: 18px 0; padding: 12px 16px; border-left: 4px solid var(--warn);
      background: var(--warn-bg); color: #51280f; font-weight: 700; }
    .lede, .muted, .empty { color: var(--muted); }
    .meta { display: flex; flex-wrap: wrap; gap: 8px 24px; padding: 14px 0;
      border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .meta strong { margin-right: 5px; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 18px 0; }
    .metric { padding: 14px 16px; border: 1px solid var(--line); background: var(--panel); }
    .metric b { display: block; color: var(--teal); font-size: 25px; }
    .metric span { color: var(--muted); font-size: 13px; }
    .table-wrap { overflow-x: auto; border: 1px solid var(--line); background: var(--panel); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { background: var(--pale); color: #174a45; font-weight: 700; white-space: nowrap; }
    tbody tr:last-child td { border-bottom: 0; }
    .empty { padding: 14px 16px; border: 1px solid var(--line); background: var(--panel); }
    .back { display: inline-block; margin-bottom: 20px; font-weight: 700; }
    .foot { margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
    @media (max-width: 700px) { main { padding-top: 20px; } h1 { font-size: 25px; }
      .metrics { grid-template-columns: 1fr; } th, td { padding: 8px; } }
    @media print { body { background: white; } main { width: 100%; padding: 0; }
      .table-wrap { overflow: visible; } a { color: inherit; text-decoration: none; } }
    '''


def document(title: str, body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{style()}</style></head>'
            f'<body><main>{body}</main></body></html>')


def collect_requests(report: dict) -> dict[str, dict]:
    requests = defaultdict(lambda: {
        'items': [], 'excluded': [], 'penny_lines': [], 'tiers': [], 'category_tiers': []})
    for row in report.get('scenarios', []):
        if row.get('candidate_tier') == 1:
            request = requests[str(row['request_id'])]
            request['items'].append(row)
            if row.get('penny_contract_flag') in ('Y', 'UNKNOWN'):
                request['penny_lines'].append(row)
    for row in report.get('excluded_unpriced_lines', []):
        request = requests[str(row['request_id'])]
        request['excluded'].append(row)
        if row.get('penny_contract_flag') in ('Y', 'UNKNOWN'):
            request['penny_lines'].append(row)
    for row in report.get('excluded_penny_contract_lines', []):
        requests[str(row['request_id'])]['penny_lines'].append(row)
    for row in report.get('groups', []):
        requests[str(row['request_id'])]['tiers'].append(row)
    for row in report.get('category_groups', []):
        requests[str(row['request_id'])]['category_tiers'].append(row)
    return dict(sorted(requests.items()))


def request_account(records: dict) -> tuple[str, str]:
    line_rows = records['items'] + records['excluded'] + records['penny_lines']
    provided = sorted({str(row.get('account_provided')) for row in line_rows
                       if row.get('account_provided') not in (None, '')})
    resolved = sorted({str(row.get('prca_num') or row.get('prca'))
                       for row in line_rows
                       if row.get('prca_num') or row.get('prca')})
    return ', '.join(provided) or 'Unavailable', ', '.join(resolved) or 'Unavailable'


def build_request_page(request_id: str, records: dict, report: dict) -> str:
    provided, resolved = request_account(records)
    items = sorted(records['items'], key=lambda row: (str(row.get('item') or ''), str(row.get('line_key') or '')))
    excluded = sorted(records['excluded'], key=lambda row: (str(row.get('item') or ''), str(row.get('line_key') or '')))
    penny_lines = sorted(records['penny_lines'], key=lambda row: (str(row.get('item') or ''), str(row.get('line_key') or '')))
    tiers = sorted(records['tiers'], key=lambda row: (str(row.get('category') or ''), row.get('candidate_tier') or 0))
    category_tiers = sorted(records['category_tiers'],
                            key=lambda row: (str(row.get('category') or ''), row.get('candidate_tier') or 0))
    _, resolved = request_account(records)
    resolved_prcas = set(resolved.split(', ')) if resolved != 'Unavailable' else set()
    penny_activity = [row for row in report.get('excluded_penny_contract_activity', [])
                      if str(row.get('prca_num')) in resolved_prcas]
    uncertain_activity = [row for row in report.get('uncertain_penny_contract_activity', [])
                          if str(row.get('prca_num')) in resolved_prcas]
    item_rows = [[row.get('item'), row.get('source_item_description'), row.get('requested_uom'),
                  row.get('sell_uom'), row.get('requested_price'), row.get('current_tier'),
                  row.get('current_tier_cost'), row.get('requested_margin_at_candidate_cost'),
                  row.get('penny_contract_flag'), row.get('penny_product_types'),
                  row.get('penny_man_contract_ids'), row.get('penny_mck_contract_ids'),
                  row.get('penny_contract_ambiguous'), row.get('penny_customer_status'),
                  row.get('penny_price_test_status'), row.get('normalized_requested_price'),
                  row.get('penny_contract_required_flag'), row.get('validation_errors'),
                  row.get('impact_exceptions')]
                 for row in items]
    tier_rows = [[row.get('category'), row.get('candidate_tier'), row.get('requested_line_count'),
                  row.get('exception_line_count'), row.get('impact_unavailable_line_count'),
                  row.get('annualized_cost_change'), row.get('annualized_customer_spend_change'),
                  row.get('annualized_gross_profit_change_proxy'), row.get('aggregation_exception')]
                 for row in tiers]
    excluded_rows = [[row.get('item'), row.get('source_item_description'), row.get('requested_uom'),
                      row.get('requested_price'), row.get('price_exclusion_reason'),
                      row.get('account_provided'), row.get('prca_num'), row.get('source_errors')]
                     for row in excluded]
    penny_line_rows = [[row.get('item'), row.get('source_item_description'),
                            row.get('requested_price'), row.get('requested_uom'),
                        row.get('normalized_requested_price'), row.get('prmry_uom'),
                        row.get('sell_uom'), row.get('buy_uom'),
                        row.get('sell_to_buy_divisor'), row.get('prmry_to_sell_divisor'),
                        row.get('prmry_to_buy_divisor'),
                            row.get('penny_tier_10_cost'), row.get('penny_product_types'),
                            row.get('penny_man_contract_ids'), row.get('penny_mck_contract_ids'),
                        row.get('penny_price_normalization_method'),
                            row.get('penny_contract_flag'), row.get('penny_contract_required_flag'),
                            row.get('penny_price_test_status'), row.get('penny_contract_ambiguous'),
                            row.get('penny_customer_status'),
                            row.get('penny_contract_exclusion_reason')]
                           for row in penny_lines]
    activity_rows = [[row.get('item_e1_num'), row.get('item_dsc'), row.get('grouped_lvl2'),
                      row.get('ipc_sell_uom'), row.get('qty_3_mth'), row.get('sls_3_mth'),
                      row.get('penny_product_types'), row.get('penny_man_contract_ids'),
                      row.get('penny_mck_contract_ids'), row.get('penny_contract_ambiguous'),
                      row.get('penny_customer_status')]
                     for row in penny_activity]
    uncertain_rows = [[row.get('item_e1_num'), row.get('item_dsc'), row.get('grouped_lvl2'),
                       row.get('ipc_sell_uom'), row.get('qty_3_mth'), row.get('sls_3_mth'),
                       row.get('penny_product_types'), row.get('penny_man_contract_ids'),
                       row.get('penny_mck_contract_ids'), row.get('penny_contract_ambiguous'),
                       row.get('penny_customer_status')]
                      for row in uncertain_activity]
    item_table = table(['Item', 'Description', 'Requested UOM', 'Sell UOM', 'Requested Price',
                                'Current Tier', 'Current Tier Cost', 'Margin at Tier 1 Cost', 'Penny Item',
                                'Penny Product', 'Manufacturer Contract IDs', 'McK Contract IDs',
                                'Contract Ambiguous', 'Customer On Contract', 'Tier 10 Price Test',
                                'Normalized Price', 'Penny Contract Required',
                                'Validation Exceptions', 'Impact Exceptions'], item_rows,
                       'No positive-price lines were eligible for tier analysis.')
    tier_table = table(['Category', 'Tier', 'Requested Lines', 'Exception Lines',
                        'Impact Unavailable', 'Annualized Cost Change',
                        'Annualized Customer Spend Change', 'Annualized GP Change Proxy',
                        'Aggregation Exception'], tier_rows,
                       'No category-tier scenarios were calculated for this request.')
    category_tier_rows = [[row.get('category'), row.get('candidate_tier'), row.get('category_item_count'),
                           row.get('baseline_sales_3m'), row.get('baseline_margin'),
                           row.get('tier_only_margin'), row.get('requested_price_margin'),
                           row.get('annualized_gp_change'), row.get('annualized_internal_cost_change'),
                           row.get('annualized_customer_spend_change'), row.get('suggested_for_review'),
                           row.get('screening_flags'), row.get('exceptions')]
                          for row in category_tiers]
    category_tier_table = table(['Category', 'Tier', 'Portfolio Items', 'Baseline Sales 3m',
                                 'Baseline Margin', 'Tier-only Margin', 'Requested-price Margin',
                                 'Annualized GP Change', 'Annualized Internal Cost Change',
                                 'Annualized Customer Spend Change', 'Suggested for Review',
                                 'Screening Flags', 'Exceptions'], category_tier_rows,
                                'No customer-category net-impact scenarios were calculated for this request.')
    excluded_table = table(['Item', 'Description', 'Requested UOM', 'Raw Requested Price',
                            'Exclusion Reason', 'Source Account', 'Resolved PRCA',
                            'Source Validation Flags'], excluded_rows,
                           'No requested lines were excluded for price.')
    penny_line_table = table(['Item', 'Description', 'Raw Requested Price', 'Requested UOM',
                              'Price in Sell UOM', 'Primary UOM', 'Sell UOM', 'Buy UOM',
                              'Sell-to-Buy Divisor', 'Primary-to-Sell Divisor', 'Primary-to-Buy Divisor',
                              'Tier 10 Cost', 'Penny Product',
                                  'Manufacturer Contract IDs', 'McK Contract IDs', 'Active Penny Item',
                              'UOM Normalization', 'Penny Contract Required', 'Tier 10 Price Test', 'Contract Ambiguous',
                                  'Customer On Contract', 'Exclusion Reason'], penny_line_rows,
                                 'No requested line matched an active penny contract item.')
    penny_activity_table = table(['Item', 'Description', 'Category', 'IPC UOM', 'Quantity 3 Months',
                                  'Sales 3 Months', 'Penny Product', 'Manufacturer Contract IDs',
                                  'McK Contract IDs', 'Contract Ambiguous', 'Customer On Contract'],
                                 activity_rows,
                                 'No on-contract penny activity was excluded for this customer.')
    uncertain_activity_table = table(['Item', 'Description', 'Category', 'IPC UOM', 'Quantity 3 Months',
                                      'Sales 3 Months', 'Penny Product', 'Manufacturer Contract IDs',
                                      'McK Contract IDs', 'Contract Ambiguous', 'Customer On Contract'],
                                     uncertain_rows,
                                     'No penny activity has an uncertain customer status.')
    header = (f'<a class="back" href="index.html">&larr; All requests</a>'
              '<div class="eyebrow">Internal pricing review</div>'
              f'<h1>{html.escape(request_id)}</h1>'
              '<p class="status">INTERNAL REVIEW | NOT APPROVED | NOT LOADABLE</p>'
              '<p class="lede">Requested prices are shown as supplied. Tier scenarios are alternatives for review, not recommendations.</p>'
              f'<div class="meta"><span><strong>Source account</strong>{html.escape(provided)}</span>'
              f'<span><strong>Resolved PRCA</strong>{html.escape(resolved)}</span>'
              f'<span><strong>Evaluated</strong>{cell(report.get("evaluated_at"))}</span></div>'
              '<div class="metrics">'
              f'<div class="metric"><b>{len(items)}</b><span>Positive-price lines analyzed</span></div>'
              f'<div class="metric"><b>{len(excluded)}</b><span>Unpriced lines excluded and retained</span></div>'
              f'<div class="metric"><b>{len(penny_lines)}</b><span>Penny items identified or unresolved</span></div>'
              f'<div class="metric"><b>{len(penny_activity)}</b><span>On-contract activity excluded</span></div>'
              f'<div class="metric"><b>{len(tiers)}</b><span>Category-tier scenarios</span></div></div>')
    body = (header + '<h2>Positive-price item review</h2>' + item_table
            + '<h2>Requested item aggregation scenarios</h2>' + tier_table
            + '<h2>Customer category net-impact scenarios</h2>' + category_tier_table
            + '<h2>Penny Contract Items</h2>' + penny_line_table
            + '<h2>Excluded unpriced lines</h2>' + excluded_table
            + '<h2>On-contract penny activity excluded from impact</h2>' + penny_activity_table
            + '<h2>Penny activity with uncertain customer status</h2>' + uncertain_activity_table
            + '<p class="foot">Source hash: ' + cell(report.get('source_hash'))
            + ' | Reference hash: ' + cell(report.get('reference_hash'))
            + '<br>Annualized values multiply three-month activity by four; they are not forecasts. Missing values are unavailable, not zero. No approval, email, source update, or pricing load was performed.</p>')
    return document(f'{request_id} | Internal EC Review', body)


def build_index(report: dict, requests: dict[str, dict], filenames: dict[str, str]) -> str:
    rows = []
    for request_id, records in requests.items():
        provided, resolved = request_account(records)
        resolved_prcas = set(resolved.split(', ')) if resolved != 'Unavailable' else set()
        excluded_activity_count = sum(str(row.get('prca_num')) in resolved_prcas
            for row in report.get('excluded_penny_contract_activity', []))
        penny_line_count = len(records['penny_lines'])
        rows.append([f'<a href="{html.escape(filenames[request_id], quote=True)}">{html.escape(request_id)}</a>',
                     html.escape(provided), html.escape(resolved), len(records['items']),
                     len(records['excluded']), penny_line_count,
                     excluded_activity_count, len(records['tiers']) + len(records['category_tiers'])])
    body_rows = ''.join('<tr>' + ''.join(f'<td>{value}</td>' for value in row) + '</tr>'
                        for row in rows)
    summary = report.get('summary', {})
    body = ('<div class="eyebrow">Extended Care | October 2026</div>'
            '<h1>Six-request pricing review</h1>'
            '<p class="status">INTERNAL REVIEW | NOT APPROVED | NOT LOADABLE</p>'
            '<p class="lede">Static local review generated from the read-only request snapshot and current reference data. No external scripts or network assets are used.</p>'
            f'<div class="metrics"><div class="metric"><b>{cell(summary.get("requested_lines"))}</b><span>Source lines</span></div>'
            f'<div class="metric"><b>{cell(summary.get("tier_analyzed_lines"))}</b><span>Positive-price lines analyzed</span></div>'
            f'<div class="metric"><b>{cell(summary.get("excluded_unpriced_lines"))}</b><span>Unpriced lines retained as exceptions</span></div>'
            f'<div class="metric"><b>{cell(summary.get("excluded_penny_contract_lines"))}</b><span>Penny lines excluded</span></div>'
            f'<div class="metric"><b>{cell(summary.get("excluded_penny_contract_activity_rows"))}</b><span>On-contract activity excluded</span></div></div>'
            '<h2>Requests</h2><div class="table-wrap"><table><thead><tr>'
            '<th scope="col">Request</th><th scope="col">Source Account</th><th scope="col">Resolved PRCA</th>'
            '<th scope="col">Analyzed Lines</th><th scope="col">Unpriced Excluded</th>'
            '<th scope="col">Penny Items</th><th scope="col">Penny Activity Excluded</th>'
            '<th scope="col">Tier Scenarios</th></tr></thead><tbody>' + body_rows + '</tbody></table></div>'
            '<p class="foot">Evaluated: ' + cell(report.get('evaluated_at'))
            + ' | Source hash: ' + cell(report.get('source_hash'))
            + '<br>Use approved local storage; these pages contain customer, internal cost, and margin data. No pricing recommendation or approval is created.</p>')
    return document('Six-request Internal EC Review', body)


def build_pages(report: dict) -> dict[str, str]:
    if report.get('mode') != 'INTERNAL_REVIEW_ONLY_NOT_APPROVED_NOT_LOADABLE':
        raise ValueError('Input must be an internal review report')
    requests = collect_requests(report)
    if not requests:
        raise ValueError('Review report contains no request lines')
    filenames = {request_id: request_filename(request_id) for request_id in requests}
    pages = {'index.html': build_index(report, requests, filenames)}
    pages.update({filenames[request_id]: build_request_page(request_id, records, report)
                  for request_id, records in requests.items()})
    return pages


def export_html(report: dict, output_dir: Path) -> dict:
    if output_dir.exists():
        raise ValueError('Choose a new HTML output directory; existing files are never overwritten')
    pages = build_pages(report)
    output_dir.mkdir(parents=True, exist_ok=False)
    for filename, content in pages.items():
        (output_dir / filename).write_text(content, encoding='utf-8')
    return {'pages': len(pages) - 1, 'index': str(output_dir / 'index.html'),
            'files': sorted(pages)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Internal review.json')
    parser.add_argument('--output-dir', type=Path, required=True, help='New local HTML output directory')
    args = parser.parse_args()
    report = json.loads(args.input.read_text(encoding='utf-8'))
    try:
        result = export_html(report, args.output_dir)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()