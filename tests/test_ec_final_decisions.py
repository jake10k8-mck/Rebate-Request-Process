import json
import unittest

from ec_final_decisions import build_final_decisions


def reference(line_key, item):
    return {'line_key': line_key, 'tier_num': 10, 'item_e1_num': item,
            'customer_matches': 1, 'pex_matches': 1, 'bus_pltfrm': 'EC',
            'item_matches': 1, 'tapestry_matches': 1, 'attachment_matches': 1,
            'prca_num': '100', 'grouped_lvl2': 'GAUZE', 'current_tier': 3,
            'current_tier_cost': '6.25', 'tier_cost': '5.00',
            'sell_uom': 'EA', 'buy_uom': 'CS', 'prmry_uom': 'EA',
            'sell_to_buy_divisor': '10', 'prmry_to_sell_divisor': '1',
            'prmry_to_buy_divisor': '10', 'penny_contract_flag': 'N',
            'penny_contract_match_count': 0, 'penny_contract_ambiguous': False,
            'penny_customer_status': 'NOT_PENNY_ITEM'}


def fixture():
    headers = [
        {'ROW_ID': 'REQ-APPROVED', 'CUST_NUM_PRCA': '100', 'CMPLTD_FLG': 'true',
         'CMPLTD_DT': '2026-10-27', 'SOURCE_STAGE': 'completed',
         'SOURCE_SHEET_ID': 'sheet-1', 'SOURCE_SHEET_ROW_ID': 'row-1',
         'INGESTION_RUN_ID': 'ing-1', 'SOURCE_HEADER_JSON': json.dumps({'Rejected': False})},
        {'ROW_ID': 'REQ-REJECTED', 'CUST_NUM_PRCA': '100', 'CMPLTD_FLG': 'false',
         'CMPLTD_DT': None, 'SOURCE_STAGE': 'rejected', 'SOURCE_SHEET_ID': 'sheet-2',
         'SOURCE_SHEET_ROW_ID': 'row-2', 'INGESTION_RUN_ID': 'ing-2',
         'SOURCE_HEADER_JSON': json.dumps({'Rejected': True, 'Outbound Notes ': 'Rejected by analyst'})},
        {'ROW_ID': 'REQ-PENDING', 'CUST_NUM_PRCA': '100', 'CMPLTD_FLG': 'false',
         'CMPLTD_DT': None, 'SOURCE_STAGE': 'intermediate', 'SOURCE_SHEET_ID': 'sheet-3',
         'SOURCE_SHEET_ROW_ID': 'row-3', 'INGESTION_RUN_ID': 'ing-3',
         'SOURCE_HEADER_JSON': json.dumps({'Rejected': False})},
    ]
    lines = [
        {'ROW_ID': 'REQ-APPROVED', 'ITEM_E1_NUM': '1', 'ITEM_DSC': 'Approved item',
         'REQ_UOM': 'EA', 'REQ_PRICE': '15.00', 'REQ_PRICE_RAW': '15.00',
         'SOURCE_ATTACHMENT_ID': 'a1', 'SOURCE_ATTACHMENT_NAME': 'form.xlsx',
         'SOURCE_ATTACHMENT_SHA256': 'hash1', 'SOURCE_SHEET_NAME': 'Items',
         'SOURCE_ROW_NUMBER': 4, 'INGESTION_RUN_ID': 'ing-1'},
        {'ROW_ID': 'REQ-APPROVED', 'ITEM_E1_NUM': '2', 'ITEM_DSC': 'Missing price',
         'REQ_UOM': 'EA', 'REQ_PRICE': None, 'REQ_PRICE_RAW': '',
         'SOURCE_ATTACHMENT_ID': 'a1', 'SOURCE_ATTACHMENT_NAME': 'form.xlsx',
         'SOURCE_ATTACHMENT_SHA256': 'hash1', 'SOURCE_SHEET_NAME': 'Items',
         'SOURCE_ROW_NUMBER': 5, 'INGESTION_RUN_ID': 'ing-1'},
        {'ROW_ID': 'REQ-REJECTED', 'ITEM_E1_NUM': '3', 'ITEM_DSC': 'Rejected item',
         'REQ_UOM': 'EA', 'REQ_PRICE': '12', 'REQ_PRICE_RAW': '12',
         'SOURCE_ATTACHMENT_ID': 'a2', 'SOURCE_ATTACHMENT_NAME': 'form.xlsx',
         'SOURCE_ATTACHMENT_SHA256': 'hash2', 'SOURCE_SHEET_NAME': 'Items',
         'SOURCE_ROW_NUMBER': 4, 'INGESTION_RUN_ID': 'ing-2'},
        {'ROW_ID': 'REQ-PENDING', 'ITEM_E1_NUM': '4', 'ITEM_DSC': 'Pending item',
         'REQ_UOM': 'EA', 'REQ_PRICE': '12', 'REQ_PRICE_RAW': '12',
         'SOURCE_ATTACHMENT_ID': 'a3', 'SOURCE_ATTACHMENT_NAME': 'form.xlsx',
         'SOURCE_ATTACHMENT_SHA256': 'hash3', 'SOURCE_SHEET_NAME': 'Items',
         'SOURCE_ROW_NUMBER': 4, 'INGESTION_RUN_ID': 'ing-3'},
    ]
    preview = {'mode': 'READ_ONLY_ETL_PREVIEW_NOT_APPROVED_NOT_LOADABLE',
               'source_hash': 'source-hash',
               'source': {'headers': headers, 'lines': lines}}
    report = {'mode': 'INTERNAL_REVIEW_ONLY_NOT_APPROVED_NOT_LOADABLE',
              'source_hash': 'source-hash', 'reference_hash': 'reference-hash',
              'references': [reference(index, str(index)) for index in range(1, 5)],
              'lines': [{'line_key': 1, 'request_id': 'REQ-APPROVED',
                         'penny_contract_flag': 'N', 'penny_contract_required_flag': 'N',
                         'penny_price_test_status': 'NOT_PENNY_ITEM'}],
              'excluded_unpriced_lines': [], 'excluded_penny_contract_lines': []}
    return preview, report


class FinalDecisionTests(unittest.TestCase):
    def test_completed_rows_snapshot_all_lines_with_approved_and_not_evaluated_status(self):
        preview, report = fixture()

        rows, summary = build_final_decisions(preview, report, '2026-10-28T12:00:00Z')

        self.assertEqual(len(rows), 3)
        approved = next(row for row in rows if row['request_id'] == 'REQ-APPROVED'
                        and row['item_e1_num'] == '1')
        not_evaluated = next(row for row in rows if row['request_id'] == 'REQ-APPROVED'
                             and row['item_e1_num'] == '2')
        self.assertEqual(approved['decision_status'], 'APPROVED')
        self.assertEqual(approved['assigned_tier'], 3)
        self.assertEqual(approved['tapestry_cost_sell_uom'], '6.25')
        self.assertEqual(approved['requested_price_raw'], '15.00')
        self.assertEqual(approved['requested_uom'], 'EA')
        self.assertEqual(approved['sell_uom_price'], '15.00')
        self.assertEqual(not_evaluated['decision_status'], 'NOT_EVALUATED')
        self.assertIn('REQUESTED_PRICE_MISSING_OR_INVALID', not_evaluated['decision_reason'])
        self.assertEqual(summary['pending_requests'], 1)

    def test_rejected_stage_is_snapshotted_with_source_reason(self):
        preview, report = fixture()

        rows, summary = build_final_decisions(preview, report)

        rejected = next(row for row in rows if row['request_id'] == 'REQ-REJECTED')
        self.assertEqual(rejected['decision_status'], 'REJECTED')
        self.assertEqual(rejected['decision_reason'], 'Rejected by analyst')
        self.assertEqual(summary['rejected_rows'], 1)

    def test_source_hash_mismatch_fails_closed(self):
        preview, report = fixture()
        report['source_hash'] = 'different-source'

        with self.assertRaisesRegex(ValueError, 'source hashes do not match'):
            build_final_decisions(preview, report)


if __name__ == '__main__':
    unittest.main()