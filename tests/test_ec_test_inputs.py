import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from ec_test_inputs import append_test_inputs, normalize_snapshot, plan_append, rows_hash


def example(request_id='OCT-1', platform='EC'):
    return {'REQUEST_ID': request_id, 'ACCOUNT_NUMBER': Decimal('14252386'),
            'CREATED_DATE': '2026-10-05 12:30:00', 'ITEM_E1_NUM': Decimal('123456'),
            'UOM_PROVIDED': 'EA', 'FUTURE_SELL_PRICE': Decimal('10.2500'),
            'RESOLVED_PLATFORM': platform, 'STRATEGIC_RATIONALE': 'Test rationale'}


class TestInputTests(unittest.TestCase):
    def test_derives_one_header_and_preserves_duplicate_line_multiplicity(self):
        headers, lines, coverage = normalize_snapshot([example(), example()])
        self.assertEqual(len(headers), 1)
        self.assertEqual(len(lines), 2)
        self.assertEqual(headers[0]['cust_num_prca'], '14252386')
        self.assertEqual(lines[0]['req_price'], Decimal('10.2500'))
        self.assertEqual(coverage['source_requests'], 1)

    def test_conflicting_headers_and_missing_ids_fail_closed(self):
        conflicting = dict(example(), ACCOUNT_NUMBER=Decimal('999'))
        with self.assertRaises(ValueError):
            normalize_snapshot([example(), conflicting])
        with self.assertRaises(ValueError):
            normalize_snapshot([dict(example(), REQUEST_ID=None)])

    def test_platform_filter_keeps_unmatched_as_exceptions_not_fake_ec(self):
        headers, lines, coverage = normalize_snapshot([example(), example('PC-1', 'PC'), example('UNK-1', None)])
        self.assertEqual(len(headers), 2)
        self.assertEqual(len(lines), 2)
        self.assertEqual(coverage['skipped_other_platform_requests'], 1)
        self.assertEqual(coverage['unresolved_platform_requests_retained'], 1)
        self.assertIsNone(headers[1]['bus_pltfrm_dsc'])

    def test_replay_noops_preserves_other_requests_and_detects_changes(self):
        headers, lines, _ = normalize_snapshot([example(), example()])
        new_headers, new_lines, skipped = plan_append(headers, lines, [], [])
        self.assertEqual(len(new_headers), 1)
        self.assertEqual(len(new_lines), 2)
        old = [{'row_id': 'SEPT-1', 'created_dt': '2026-09-25', 'cust_num_prca': None}]
        before = rows_hash(old)
        replay = plan_append(headers, lines, headers + old, lines)
        self.assertEqual(replay[:2], ([], []))
        self.assertEqual(replay[2], ['OCT-1'])
        self.assertEqual(before, rows_hash(old))
        with self.assertRaises(ValueError):
            plan_append(headers, lines, headers, lines[:1])
        with self.assertRaises(ValueError):
            plan_append(headers, [dict(lines[0], req_price=Decimal('11'))], headers, lines)

    def test_float_storage_loss_is_blocked_and_missing_uom_remains_raw(self):
        with self.assertRaises(ValueError):
            normalize_snapshot([dict(example(), FUTURE_SELL_PRICE=Decimal('0.100000000000000005'))])
        _, lines, _ = normalize_snapshot([dict(example(), UOM_PROVIDED=None)])
        self.assertIsNone(lines[0]['req_uom'])

    def test_apply_commits_only_after_header_and_line_reconciliation(self):
        headers, lines, _ = normalize_snapshot([example()])
        connection = Mock()
        with patch('ec_test_inputs.fetch', side_effect=[[], [], headers, lines]):
            result = append_test_inputs(connection, headers, lines, apply=True)
        self.assertTrue(result['applied'])
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
        statements = [call.args[0] for call in connection.cursor.return_value.executemany.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(statement.startswith('INSERT INTO PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_')
                            and '_PIPELINE_TEST' in statement for statement in statements))

    def test_failed_post_insert_check_rolls_back_without_commit(self):
        headers, lines, _ = normalize_snapshot([example()])
        connection = Mock()
        with patch('ec_test_inputs.fetch', side_effect=[[], [], headers, []]):
            with self.assertRaises(ValueError):
                append_test_inputs(connection, headers, lines, apply=True)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_default_dry_run_never_inserts(self):
        headers, lines, _ = normalize_snapshot([example()])
        connection = Mock()
        with patch('ec_test_inputs.fetch', side_effect=[[], []]):
            result = append_test_inputs(connection, headers, lines)
        self.assertFalse(result['applied'])
        connection.cursor.return_value.executemany.assert_not_called()
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_smartsheet_intake_apply_uses_separate_allowlisted_tables(self):
        headers, lines, _ = normalize_snapshot([example()])
        connection = Mock()
        with patch('ec_test_inputs.fetch', side_effect=[[], [], headers, lines]):
            result = append_test_inputs(connection, headers, lines, apply=True,
                                        table_set='smartsheet-intake-test')
        self.assertTrue(result['applied'])
        statements = [call.args[0] for call in connection.cursor.return_value.executemany.call_args_list]
        self.assertIn('INSERT INTO PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_HEADER_ETL_TEST',
                      statements[0])
        self.assertIn('INSERT INTO PRD_SBX_MMS_DB.PRC.EC_REBATE_REQUEST_LINES_ETL_TEST',
                      statements[1])
        with self.assertRaisesRegex(ValueError, 'Unknown test table set'):
            append_test_inputs(connection, headers, lines, table_set='arbitrary-table')


if __name__ == '__main__':
    unittest.main()