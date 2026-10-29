import unittest
from decimal import Decimal

from ec_etl_preview import build_preview, read_etl


class FakeCursor:
    def __init__(self, result_sets=None) -> None:
        self.calls = []
        self.description = []
        self.rows = []
        self.closed = False
        self.result_sets = result_sets

    def execute(self, statement, parameters) -> None:
        self.calls.append((statement, parameters))
        if self.result_sets is not None:
            self.description, self.rows = self.result_sets[len(self.calls) - 1]
        elif len(self.calls) == 1:
            self.description = [('ROW_ID',), ('CUST_NUM_PRCA',)]
            self.rows = [('request-1', 'customer-1')]
        else:
            self.description = [('ROW_ID',), ('ITEM_E1_NUM',), ('REQ_UOM',), ('REQ_PRICE',)]
            self.rows = [('request-1', '123456.0', 'EA', Decimal('10.2500'))]

    def fetchall(self):
        return self.rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, result_sets=None) -> None:
        self.reader = FakeCursor(result_sets)

    def cursor(self):
        return self.reader


class PreviewTests(unittest.TestCase):
    def test_reader_only_selects_parameterized_existing_tables(self) -> None:
        connection = FakeConnection()
        headers, lines = read_etl(connection, 'DB', 'PRC', '2026-10-01', '2026-10-21',
                                  segment="EC' OR 1=1 --")
        self.assertEqual(len(headers), 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(connection.reader.closed)
        for statement, parameters in connection.reader.calls:
            self.assertTrue(statement.startswith('SELECT '))
            self.assertNotIn("EC' OR", statement)
        self.assertIn("EC' OR 1=1 --", connection.reader.calls[0][1])
        self.assertEqual(connection.reader.calls[1][1], ('request-1',))
        with self.assertRaises(ValueError):
            read_etl(connection, 'DB; DELETE', 'PRC', '2026-10-01', '2026-10-21')

    def test_pipeline_test_reads_only_allowlisted_test_tables(self) -> None:
        connection = FakeConnection()
        read_etl(connection, 'DB', 'PRC', '2026-09-24', '2026-10-01',
                 table_set='pipeline-test')
        self.assertIn('DB.PRC.EC_REBATE_REQUEST_HEADER_PIPELINE_TEST', connection.reader.calls[0][0])
        self.assertIn('DB.PRC.EC_REBATE_REQUEST_LINES_PIPELINE_TEST', connection.reader.calls[1][0])
        with self.assertRaises(ValueError):
            read_etl(connection, 'DB', 'PRC', '2026-09-24', '2026-10-01',
                     table_set='source; DROP TABLE anything')
        self.assertEqual(len(connection.reader.calls), 2)

    def test_smartsheet_intake_test_reads_separate_tables(self) -> None:
        connection = FakeConnection([
            ([('ROW_ID',), ('CUST_NUM_PRCA',), ('INGESTION_RUN_ID',)],
             [('request-1', '12345', 'run-1')]),
            ([('ROW_ID',), ('ITEM_E1_NUM',), ('REQ_UOM',), ('REQ_PRICE',), ('INGESTION_RUN_ID',)],
             [('request-1', '123456', 'EA', '10.25', 'run-1')]),
        ])
        read_etl(connection, 'DB', 'PRC', '2026-10-01', '2026-10-27',
                 table_set='smartsheet-intake-test')
        self.assertIn('DB.PRC.EC_REBATE_REQUEST_HEADER_ETL_TEST',
                      connection.reader.calls[0][0])
        self.assertIn('DB.PRC.EC_REBATE_REQUEST_LINES_ETL_TEST',
                      connection.reader.calls[1][0])

    def test_smartsheet_intake_preview_selects_latest_header_and_matching_lines(self) -> None:
        connection = FakeConnection([
            ([('ROW_ID',), ('CUST_NUM_PRCA',), ('INGESTION_RUN_ID',), ('EXTRACTED_AT',)],
             [('request-1', '12345', 'run-new', '2026-10-28 12:00:00')]),
            ([('ROW_ID',), ('ITEM_E1_NUM',), ('REQ_UOM',), ('REQ_PRICE',), ('INGESTION_RUN_ID',)],
             [('request-1', '654321', 'EA', '10.00', 'run-new')]),
        ])

        headers, lines = read_etl(connection, 'DB', 'PRC', '2026-10-01', '2026-11-01',
                                  table_set='smartsheet-intake-test')

        header_sql, header_params = connection.reader.calls[0]
        line_sql, line_params = connection.reader.calls[1]
        self.assertIn('QUALIFY ROW_NUMBER() OVER (PARTITION BY ROW_ID', header_sql)
        self.assertIn('ORDER BY EXTRACTED_AT DESC, INGESTION_RUN_ID DESC', header_sql)
        self.assertEqual(headers[0]['INGESTION_RUN_ID'], 'run-new')
        self.assertIn('ROW_ID = %s AND INGESTION_RUN_ID = %s', line_sql)
        self.assertEqual(line_params, ('request-1', 'run-new'))
        self.assertEqual(lines[0]['INGESTION_RUN_ID'], 'run-new')

    def test_reader_filters_specific_request_ids_with_parameters(self) -> None:
        connection = FakeConnection()
        read_etl(connection, 'DB', 'PRC', '2026-10-01', '2026-10-27',
                 request_ids=['request-2', "request-1' OR 1=1 --"])

        header_sql, header_parameters = connection.reader.calls[0]
        self.assertIn('ROW_ID IN (%s, %s)', header_sql)
        self.assertNotIn("request-1' OR", header_sql)
        self.assertEqual(header_parameters,
                         ('2026-10-01', '2026-10-27', "request-1' OR 1=1 --", 'request-2', 100))
        self.assertEqual(connection.reader.calls[1][1], ('request-1',))

    def test_reader_rejects_empty_request_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, 'nonempty strings'):
            read_etl(FakeConnection(), 'DB', 'PRC', '2026-10-01', '2026-10-27',
                     request_ids=[''])

    def test_preview_preserves_duplicates_raw_prices_and_requested_fixed_price(self) -> None:
        headers = [{'ROW_ID': 'request-1'}]
        line = {'ROW_ID': 'request-1', 'ITEM_E1_NUM': '123456.0', 'REQ_UOM': 'EA',
                'REQ_PRICE': Decimal('10.2500')}
        preview = build_preview(headers, [line, line.copy()])
        self.assertEqual(preview['summary']['line_rows'], 2)
        self.assertEqual(preview['lines'][1]['duplicate_occurrence'], 2)
        self.assertEqual(preview['lines'][0]['item'], '123456')
        self.assertEqual(preview['source']['lines'][0]['ITEM_E1_NUM'], '123456.0')
        self.assertEqual(preview['lines'][0]['fixed_price_if_approved'], '10.2500')
        self.assertEqual(preview['lines'][0]['status'], 'AWAITING_REFERENCE_ENRICHMENT')

    def test_missing_invalid_and_ambiguous_data_stays_visible(self) -> None:
        headers = [{'ROW_ID': 'duplicate'}, {'ROW_ID': 'duplicate'}, {'ROW_ID': 'no-lines'}]
        lines = [{'ROW_ID': 'duplicate', 'ITEM_E1_NUM': 'invalid', 'REQ_UOM': '', 'REQ_PRICE': '#N/A'},
                 {'ROW_ID': 'orphan', 'ITEM_E1_NUM': '123', 'REQ_UOM': 'EA', 'REQ_PRICE': 'NaN'}]
        preview = build_preview(headers, lines)
        self.assertEqual(preview['summary']['invalid_lines'], 2)
        self.assertIn('AMBIGUOUS_HEADER', preview['lines'][0]['validation_errors'])
        self.assertIn('MISSING_HEADER', preview['lines'][1]['validation_errors'])
        self.assertIsNone(preview['lines'][0]['fixed_price_if_approved'])
        self.assertTrue(any('NO_STORED_LINES' in row['errors'] for row in preview['header_exceptions']))


if __name__ == '__main__':
    unittest.main()