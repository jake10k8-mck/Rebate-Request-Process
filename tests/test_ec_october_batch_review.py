import unittest
from unittest.mock import patch

from ec_october_batch_review import (analyze_snapshot, parse_request_pairs,
                                     select_target_rows)


class OctoberBatchReviewTests(unittest.TestCase):
    def test_request_pairs_parse_and_filter_by_verified_account(self):
        targets = parse_request_pairs(['MBR-1=2001', 'MBR-2=2002'])
        rows = [
            {'request_id': 'MBR-1', 'account_number': 2001},
            {'request_id': 'MBR-2', 'account_number': '2002'},
            {'request_id': 'OTHER', 'account_number': 2003},
        ]

        selected = select_target_rows(rows, targets)

        self.assertEqual([row['request_id'] for row in selected], ['MBR-1', 'MBR-2'])

    def test_request_pair_rejects_missing_or_mismatched_source(self):
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID=ACCOUNT_NUMBER'):
            parse_request_pairs(['MBR-1'])
        with self.assertRaisesRegex(ValueError, 'absent from October source snapshot'):
            select_target_rows([], {'MBR-1': '2001'})
        with self.assertRaisesRegex(ValueError, 'account does not match'):
            select_target_rows([{'request_id': 'MBR-1', 'account_number': 2002}],
                               {'MBR-1': '2001'})

    @patch('ec_october_batch_review.requested_item_tier_scenarios', return_value=[])
    @patch('ec_october_batch_review.category_scenarios', return_value=[])
    @patch('ec_october_batch_review.fetch_customer_activity', return_value=[])
    @patch('ec_october_batch_review.group_scenarios', return_value=[])
    @patch('ec_october_batch_review.calculate_scenarios')
    @patch('ec_october_batch_review.read_references')
    @patch('ec_october_batch_review.prepare_requests')
    @patch('ec_october_batch_review.build_preview', return_value={'mode': 'preview'})
    @patch('ec_october_batch_review.normalize_snapshot', return_value=([{'row_id': 'EC-1'}], [{'row_id': 'EC-1'}], {}))
    @patch('ec_october_batch_review.read_snapshot')
    def test_only_confirmed_ec_rows_are_included_and_current_tier_is_assumption(
            self, read_snapshot, normalize_snapshot, build_preview, prepare_requests,
            read_references, calculate_scenarios, group_scenarios,
            fetch_customer_activity, category_scenarios, request_tier_scenarios):
        read_snapshot.return_value = [
            {'request_id': 'EC-1', 'resolved_platform': 'EC', 'account_number': 1},
            {'request_id': 'NB-1', 'resolved_platform': 'NB', 'account_number': 2},
            {'request_id': 'UNKNOWN-1', 'resolved_platform': None, 'account_number': 3},
        ]
        prepare_requests.return_value = (
            [{'line_key': 1}, {'line_key': 2}],
            [{'line_key': 1, 'request_id': 'EC-1', 'requested_price': '10'},
             {'line_key': 2, 'request_id': 'EC-1', 'requested_price': ''}])
        read_references.return_value = [
            {'line_key': 1, 'tier_num': 1, 'prca_num': '100', 'current_tier': 5},
            {'line_key': 2, 'tier_num': 1, 'prca_num': '100', 'current_tier': 5}]
        calculate_scenarios.return_value = [{'request_id': 'EC-1', 'current_tier': 5,
                                             'candidate_tier': 1}]

        report, details = analyze_snapshot(object(), '2026-10-01', '2026-10-24')

        self.assertEqual(details['metadata']['source_requests'], 3)
        self.assertEqual(details['metadata']['confirmed_ec_source_requests'], 1)
        self.assertEqual(details['metadata']['unresolved_platform_requests_excluded'], 1)
        self.assertEqual(details['metadata']['non_ec_requests_excluded'], 1)
        self.assertEqual(report['scenarios'][0]['analyst_benchmark_tier'], 5)
        self.assertEqual([row['line_key'] for row in report['lines']], [1])
        self.assertEqual(report['excluded_unpriced_lines'][0]['line_key'], 2)
        self.assertEqual(report['excluded_unpriced_lines'][0]['prca_num'], '100')
        self.assertEqual(calculate_scenarios.call_args.args[0], [report['lines'][0]])
        self.assertEqual(calculate_scenarios.call_args.args[1], [read_references.return_value[0]])
        self.assertEqual(report['batch_metadata']['tier_analyzed_lines'], 1)
        self.assertEqual(report['batch_metadata']['excluded_unpriced_lines'], 1)
        self.assertEqual(report['coverage'], 'ALL_CONFIRMED_EC_OCTOBER_REQUESTS_PLUS_MAPPED_CUSTOMER_CATEGORY_ACTIVITY')
        self.assertEqual(normalize_snapshot.call_args.args[0], [read_snapshot.return_value[0]])


if __name__ == '__main__':
    unittest.main()