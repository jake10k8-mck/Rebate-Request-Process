import io
import unittest
from datetime import date, datetime
from unittest.mock import Mock

from openpyxl import Workbook

from ETL.ec_smartsheet_intake import (append_raw_snapshot, build_raw_attachment,
                                      build_raw_header, build_raw_line,
                                      find_line_columns, parse_attachment,
                                      parse_request_pairs, select_sheet_rows)


class SmartsheetIntakeTests(unittest.TestCase):
    def test_request_pairs_validate_and_reject_conflicting_accounts(self):
        self.assertEqual(parse_request_pairs(['MBR-1=2001', 'MBR-2=2002']),
                         {'MBR-1': '2001', 'MBR-2': '2002'})
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID=NUMERIC_ACCOUNT'):
            parse_request_pairs(['MBR-1'])
        with self.assertRaisesRegex(ValueError, 'Conflicting accounts'):
            parse_request_pairs(['MBR-1=2001', 'MBR-1=2002'])

    def test_sheet_selection_prefers_original_id_and_skips_blank_rows(self):
        columns = {1: 'Original Row ID', 2: 'Row ID', 3: 'Customer Account Number/PRCA', 4: 'Created'}
        rows = [
            {'id': 42, 'cells': [
                {'columnId': 1, 'value': 'MBR-1'},
                {'columnId': 2, 'value': 987654},
                {'columnId': 3, 'value': 2001},
                {'columnId': 4, 'value': '2026-10-26T09:00:00Z'},
            ]},
            {'id': 43, 'cells': [{'columnId': 1, 'value': None}]},
            {'id': 44, 'cells': [
                {'columnId': 1, 'value': 'MBR-OLD'},
                {'columnId': 3, 'value': 2002},
                {'columnId': 4, 'value': '2026-10-20T09:00:00Z'},
            ]},
        ]

        selected = select_sheet_rows(rows, 'landing', columns, {'MBR-1': '2001'}, None, None)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['request_id'], 'MBR-1')
        self.assertEqual(selected[0]['account_number'], '2001')
        self.assertEqual(selected[0]['source_sheet_row_id'], '42')

    def test_sheet_selection_checks_account_and_dates(self):
        columns = {1: 'Original Row ID', 2: 'Customer Account Number/PRCA', 3: 'Created'}
        rows = [{'id': 42, 'cells': [
            {'columnId': 1, 'value': 'MBR-1'},
            {'columnId': 2, 'value': 2002},
            {'columnId': 3, 'value': '2026-10-26T09:00:00Z'},
        ]}]
        with self.assertRaisesRegex(ValueError, 'does not match supplied account'):
            select_sheet_rows(rows, 'landing', columns, {'MBR-1': '2001'}, None, None)
        selected = select_sheet_rows(rows, 'landing', columns, {}, None,
                         date(2026, 10, 26))
        self.assertEqual(selected, [])

    def test_batch_retains_missing_account_as_unresolved_but_targeted_run_fails(self):
        columns = {1: 'Original Row ID', 2: 'Customer Account Number/PRCA', 3: 'Created'}
        rows = [{'id': 42, 'cells': [
            {'columnId': 1, 'value': 'MBR-1'},
            {'columnId': 2, 'value': None},
            {'columnId': 3, 'value': '2026-10-26T09:00:00Z'},
        ]}]

        selected = select_sheet_rows(rows, 'landing', columns, {}, None, None)

        self.assertEqual(len(selected), 1)
        self.assertIsNone(selected[0]['account_number'])
        with self.assertRaisesRegex(ValueError, 'missing Customer Account Number/PRCA'):
            select_sheet_rows(rows, 'landing', columns, {'MBR-1': '2001'}, None, None)

    def test_xlsx_parser_finds_header_after_instructions_and_preserves_duplicates(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = 'Request Items'
        worksheet.append(['Item number', None, None])
        worksheet.append(['Instructions', None, None])
        worksheet.append(['E1 Item Number', 'Requested UOM', 'Future Sell Price',
                  'Current Sell Price', 'Item Description'])
        worksheet.append([123456, 'EA', 10.25, 9.75, 'Test item'])
        worksheet.append([123456, 'EA', 10.25, 9.75, 'Test item'])
        content = io.BytesIO()
        workbook.save(content)
        workbook.close()

        lines = parse_attachment(content.getvalue(), 'MBR-1', 'request.xlsx')

        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['item_e1_num'], 123456)
        self.assertEqual(lines[0]['future_sell_price'], 10.25)
        self.assertEqual(lines[0]['item_dsc'], 'Test item')
        self.assertEqual(lines[1]['source_row_number'], 5)

    def test_csv_parser_accepts_established_column_aliases(self):
        content = b'Notes\nITEM_E1_NUM,REQ_UOM,REQ_PRICE\n123456,EA,10.25\n'

        lines = parse_attachment(content, 'MBR-1', 'request.csv')

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['item_e1_num'], '123456')
        self.assertEqual(lines[0]['future_sell_price'], '10.25')

    def test_incomplete_instruction_row_is_not_treated_as_an_invalid_header(self):
        self.assertIsNone(find_line_columns(['Item Number', 'Instructions']))

    def test_raw_records_preserve_missing_prca_and_unparseable_price(self):
        extracted_at = datetime(2026, 10, 26, 12, 0, 0)
        header = build_raw_header({
            'request_id': 'MBR-1', 'created_date': '2026-10-26',
            'account_number': None, 'source_stage': 'landing',
            'source_sheet_id': 'sheet-1', 'source_sheet_row_id': 'row-1',
            'source_header': {'Sales Segment': 'Extended Care'},
            'attachments': [{'attachment_id': 'att-1'}],
        }, 'run-1', extracted_at, attachment_count=1)
        line = build_raw_line({
            'request_id': 'MBR-1', 'item_e1_num': 123456,
            'item_dsc': 'Example', 'uom_provided': 'EA',
            'future_sell_price': 'pending', 'attachment_id': 'att-1',
            'source_file': 'request.xlsx', 'attachment_sha256': 'a' * 64,
            'source_sheet': 'Items', 'source_row_number': 10,
        }, 'run-1', extracted_at)
        self.assertIsNone(header['cust_num_prca'])
        self.assertEqual(header['attachment_count'], 1)
        self.assertIsNone(line['req_price'])
        self.assertEqual(line['req_price_raw'], 'pending')

    def test_multi_value_additional_bts_stays_raw_not_in_scalar_field(self):
        header = build_raw_header({
            'request_id': 'MBR-1', 'created_date': '2026-10-26',
            'account_number': '2001', 'source_stage': 'landing',
            'source_sheet_id': 'sheet-1', 'source_sheet_row_id': 'row-1',
            'source_header': {
                'Additional BTs': '57095234\n54510967',
                'Account to Mirror': 'Mirror all contracts between all accounts listed',
            },
            'attachments': [],
        }, 'run-1', datetime(2026, 10, 26), attachment_count=0)
        self.assertIsNone(header['addtl_bt_num'])
        self.assertEqual(header['addtl_bt_num_raw'], '57095234\n54510967')
        self.assertIsNone(header['acct_to_mirror'])
        self.assertEqual(header['acct_to_mirror_raw'],
                 'Mirror all contracts between all accounts listed')

    def test_raw_append_defaults_to_dry_run_and_applies_all_three_tables_atomically(self):
        extracted_at = datetime(2026, 10, 26, 12, 0, 0)
        header = build_raw_header({
            'request_id': 'MBR-1', 'created_date': '2026-10-26',
            'account_number': '2001', 'source_stage': 'landing',
            'source_sheet_id': 'sheet-1', 'source_sheet_row_id': 'row-1',
            'source_header': {}, 'attachments': [],
        }, 'run-1', extracted_at, attachment_count=0)
        line = build_raw_line({
            'request_id': 'MBR-1', 'item_e1_num': '123456',
            'future_sell_price': '10.25',
        }, 'run-1', extracted_at)
        attachment = build_raw_attachment({
            'request_id': 'MBR-1', 'source_stage': 'landing',
            'source_sheet_id': 'sheet-1', 'source_sheet_row_id': 'row-1',
            'attachment_id': 'att-1', 'attachment_name': 'request.xlsx',
            'file_extension': '.xlsx', 'download_status': 'SUCCEEDED',
            'parse_status': 'PARSED', 'parsed_line_count': 1,
        }, 'run-1', extracted_at)
        connection = Mock()
        connection.cursor.return_value.fetchone.side_effect = [
            (0,), (0,), (0,), (1,), (1,), (1,),
        ]

        preview = append_raw_snapshot(connection, [header], [line], [attachment], 'run-1')
        self.assertFalse(preview['applied'])
        connection.rollback.assert_called_once()
        connection.cursor.return_value.executemany.assert_not_called()

        connection.cursor.return_value.fetchone.side_effect = [
            (0,), (0,), (0,), (1,), (1,), (1,),
        ]
        result = append_raw_snapshot(connection, [header], [line], [attachment],
                                     'run-1', apply=True)
        self.assertTrue(result['applied'])
        self.assertEqual(connection.cursor.return_value.executemany.call_count, 3)
        connection.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
