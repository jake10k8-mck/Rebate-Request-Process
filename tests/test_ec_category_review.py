import unittest
from decimal import Decimal
from pathlib import Path
import re

from ec_category_review import (TIER_TARGET_MARGINS, category_scenarios,
                                partition_penny_contract_activity,
                                requested_item_tier_scenarios, tier_benchmark_price)


def fixture():
    report = {'scenarios': [{'line_key': 1, 'prca_num': '100', 'request_id': 'REQ-1',
        'grouped_lvl2': 'GAUZE', 'candidate_tier': tier, 'item': '1', 'requested_price': '11',
        'tier_cost': Decimal(tier) / 2, 'sell_uom': 'EA', 'requested_uom': 'EA',
        'customer_matches': 1, 'pex_matches': 1, 'bus_pltfrm': 'EC', 'item_matches': 1,
        'tapestry_matches': 1, 'attachment_matches': 1, 'current_tier': 3}
        for tier in range(1, 11)]}
    activity = [{'prca_num': '100', 'item_e1_num': item, 'grouped_lvl2': 'GAUZE',
        'qty_3_mth': Decimal(quantity), 'sls_3_mth': Decimal(sales),
        'ipc_sell_uom': 'EA', 'reference_sell_uom': 'EA', 'item_matches': 1,
        'tapestry_matches': 1, 'prvt_brnd_flg': 'Y', 'attachment_matches': 1, 'current_tier': 3,
        **{f'tier_{tier}': Decimal(tier) for tier in range(1, 11)}}
        for item, quantity, sales in [('1', '10', '100'), ('2', '90', '900')]]
    return report, activity


class CategoryReviewTests(unittest.TestCase):
    def test_on_contract_penny_activity_is_excluded_and_unknown_is_retained_for_review(self):
        activity = [
            {'prca_num': '100', 'item_e1_num': '1', 'penny_contract_flag': 'Y',
             'penny_customer_status': 'Y'},
            {'prca_num': '100', 'item_e1_num': '2', 'penny_contract_flag': 'Y',
             'penny_customer_status': 'UNKNOWN_AMBIGUOUS_CUSTOMER_ELIGIBILITY'},
            {'prca_num': '100', 'item_e1_num': '3', 'penny_contract_flag': 'N',
             'penny_customer_status': 'NOT_PENNY_ITEM'},
        ]

        retained, excluded, uncertain = partition_penny_contract_activity(activity)

        self.assertEqual([row['item_e1_num'] for row in excluded], ['1'])
        self.assertEqual([row['item_e1_num'] for row in retained], ['2', '3'])
        self.assertEqual([row['item_e1_num'] for row in uncertain], ['2'])

    def test_category_net_impact_excludes_confirmed_penny_activity(self):
        report, activity = fixture()
        activity[1].update(penny_contract_flag='Y', penny_customer_status='Y')

        rows = category_scenarios(report, activity)

        self.assertTrue(all(row['category_item_count'] == 1 for row in rows))
        self.assertTrue(all(row['baseline_sales_3m'] == '100.00' for row in rows))

    def test_unknown_penny_activity_blocks_category_impact(self):
        report, activity = fixture()
        activity[1].update(penny_contract_flag='Y',
                           penny_customer_status='UNKNOWN_AMBIGUOUS_CUSTOMER_ELIGIBILITY')

        rows = category_scenarios(report, activity)

        self.assertTrue(all(row['baseline_sales_3m'] is None for row in rows))
        self.assertTrue(all('PENNY_CUSTOMER_STATUS_UNKNOWN_FOR_ACTIVITY' in row['exceptions']
                            for row in rows))

    def test_unrequested_items_included_with_distinct_price_scenarios(self):
        report, activity = fixture()
        rows = category_scenarios(report, activity)
        row = next(row for row in rows if row['candidate_tier'] == 8)
        self.assertEqual(row['category_item_count'], 2)
        self.assertEqual(row['baseline_gp_3m'], '700.00')
        self.assertEqual(row['tier_only_margin'], '0.200000')
        self.assertEqual(row['requested_price_sales_3m'], '1010.00')
        self.assertEqual(row['requested_price_gp_3m'], '210.00')
        self.assertEqual(row['annualized_gp_change'], '-1960.00')
        self.assertEqual(row['annualized_customer_spend_change'], '40.00')
        self.assertEqual(row['tier_target_margin'], '0.090000')
        self.assertFalse(row['suggested_for_review'])
        selected = next(scenario for scenario in rows if scenario['suggested_for_review'])
        self.assertEqual(selected['candidate_tier'], 9)

    def test_suggestion_uses_requested_price_category_margin(self):
        report, activity = fixture()
        for row in report['scenarios']:
            row['requested_price'] = '4.01'
        rows = category_scenarios(report, activity)
        selected = next(row for row in rows if row['suggested_for_review'])
        self.assertEqual(selected['candidate_tier'], 8)
        self.assertNotEqual(selected['distance_from_tier_target'],
                            str(abs(Decimal(selected['tier_only_margin'])
                                     - Decimal(selected['tier_target_margin'])).quantize(Decimal('0.000001'))))

    def test_request_tier_suggestion_does_not_require_ipc_activity(self):
        report, activity = fixture()
        self.assertFalse(any(row['suggested_for_review']
                             for row in category_scenarios(report, [])))
        request_rows = requested_item_tier_scenarios(report)
        selected = next(row for row in request_rows if row['suggested_for_review'])
        self.assertEqual(selected['request_item_count'], 1)
        self.assertIn('ONE_UNIT_PER_DISTINCT_REQUESTED_ITEM_NO_QUANTITY_DATA',
                      selected['screening_flags'])

    def test_request_tier_suggestion_counts_duplicate_item_once(self):
        report, _ = fixture()
        duplicates = [dict(row, line_key=2, source_duplicate_count=2)
                      for row in report['scenarios']]
        baseline = requested_item_tier_scenarios(report)
        duplicated = requested_item_tier_scenarios({'scenarios': report['scenarios'] + duplicates})
        baseline_tier = next(row['candidate_tier'] for row in baseline if row['suggested_for_review'])
        duplicated_tier = next(row['candidate_tier'] for row in duplicated if row['suggested_for_review'])
        self.assertEqual(duplicated_tier, baseline_tier)
        self.assertTrue(all(row['request_item_count'] == 1 for row in duplicated))
        self.assertTrue(all('DUPLICATE_REQUEST_LINES_COUNTED_ONCE' in row['screening_flags']
                            for row in duplicated))

    def test_analyst_discrete_margin_schedule_is_used_exactly(self):
        expected = ['0.25', '0.23', '0.21', '0.18', '0.16', '0.14', '0.12', '0.09', '0.07', '0.05']
        self.assertEqual([str(TIER_TARGET_MARGINS[tier]) for tier in range(1, 11)], expected)
        self.assertEqual(str(tier_benchmark_price('3.45', 7)), '3.92')
        self.assertIsNone(tier_benchmark_price('3.45', 11))

    def test_snowflake_enrichment_uses_the_same_discrete_schedule(self):
        root = Path(__file__).parents[1]
        sources = [
            (root / 'sql' / 'ec_review_enrichment.snowflake.sql',
             r'TIERS AS \(\s*SELECT COLUMN1 AS TIER_NUM, COLUMN2 AS TIER_TARGET_MARGIN\s*FROM VALUES(.*?)\n\)',
             'candidate.TIER_COST / (1 - tiers.TIER_TARGET_MARGIN)'),
            (root / 'OCTOBER 2026 MCKB REBATE REQUEST 1.sql',
             r'SELECT T_3\.\*,\s*ROUND\(T_3\.TIER_COST/\(1-CASE T_3\.TIER_NUM(.*?)END\),2\) AS BENCHMARK_PRICE',
             'ROUND(T_3.TIER_COST/(1-CASE T_3.TIER_NUM'),
            (root / 'CURRENT TAPESTRY COST LOOKUP 4.sql',
             r'SELECT T_3\.\*,\s*ROUND\(T_3\.TIER_COST/\(1-CASE T_3\.TIER_NUM(.*?)END\),2\) AS BENCHMARK_PRICE',
             'ROUND(T_3.TIER_COST/(1-CASE T_3.TIER_NUM'),
        ]
        expected = [(str(tier), str(TIER_TARGET_MARGINS[tier])) for tier in range(1, 11)]
        for path, pattern, formula in sources:
            with self.subTest(source=path.name):
                sql = path.read_text(encoding='utf-8')
                if path.name.startswith('OCTOBER') or path.name.startswith('CURRENT'):
                    match = re.search(pattern, sql, re.DOTALL)
                    self.assertIsNotNone(match)
                    margin_values = re.findall(r'WHEN\s+(\d+)\s+THEN\s+(0\.\d+)', match.group(1))
                    self.assertEqual(margin_values, expected)
                    self.assertIn(formula, sql)
                else:
                    match = re.search(pattern, sql, re.DOTALL)
                    self.assertIsNotNone(match)
                    parsed = re.findall(r'\((\d+),\s*([0-9.]+)\)', match.group(1))
                    self.assertEqual(parsed, expected)
                    self.assertIn(formula, sql)

    def test_missing_cost_blocks_complete_recommendation(self):
        report, activity = fixture()
        activity[1]['tier_8'] = None
        row = category_scenarios(report, activity)[7]
        self.assertIsNone(row['requested_price_margin'])
        self.assertFalse(row['suggested_for_review'])
        self.assertIn('MISSING_CATEGORY_CANDIDATE_COST', row['exceptions'])

    def test_duplicate_snapshot_lines_preserve_prices_but_block_suggestion(self):
        report, activity = fixture()
        for row in report['scenarios']:
            row['source_duplicate_count'] = 2
        rows = category_scenarios(report, activity)
        self.assertTrue(all(not row['suggested_for_review'] for row in rows))
        self.assertTrue(all('DUPLICATE_SNAPSHOT_SOURCE_LINES' in row['screening_flags'] for row in rows))
        self.assertEqual(next(row for row in rows if row['candidate_tier'] == 8)['requested_price_sales_3m'], '1010.00')

    def test_no_sales_not_zero_and_missing_requested_uom_is_recorded_assumed(self):
        report, _ = fixture()
        for row in report['scenarios']:
            row['requested_uom'] = None
        rows = category_scenarios(report, [])
        self.assertTrue(all(row['annualized_gp_change'] is None for row in rows))
        self.assertTrue(all(row['assumed_sell_uom_lines'] == [1] for row in rows))
        self.assertTrue(all(not row['suggested_for_review'] for row in rows))


if __name__ == '__main__':
    unittest.main()