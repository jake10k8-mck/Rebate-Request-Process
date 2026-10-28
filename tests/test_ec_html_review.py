import tempfile
import unittest
from pathlib import Path

from ec_html_review import build_pages, export_html


def example():
    return {
        'mode': 'INTERNAL_REVIEW_ONLY_NOT_APPROVED_NOT_LOADABLE',
        'evaluated_at': '2026-10-27T12:00:00Z',
        'source_hash': 'source-hash',
        'reference_hash': 'reference-hash',
        'summary': {'requested_lines': 2, 'tier_analyzed_lines': 1,
                    'excluded_unpriced_lines': 1},
        'scenarios': [{
            'request_id': 'MBR-TEST-1', 'candidate_tier': 1, 'prca_num': '100',
            'account_provided': '900', 'grouped_lvl2': 'GAUZE', 'item': '42',
            'source_item_description': '<script>alert(1)</script>', 'requested_uom': 'EA',
            'sell_uom': 'EA', 'requested_price': '12.00', 'current_tier': 4,
            'current_tier_cost': '8.00', 'requested_margin_at_candidate_cost': '0.333333',
            'validation_errors': [], 'impact_exceptions': ['NO_IPC_ACTIVITY'],
        }],
        'groups': [{'request_id': 'MBR-TEST-1', 'prca': '100', 'category': 'GAUZE',
                    'candidate_tier': 1, 'requested_line_count': 1,
                    'exception_line_count': 0, 'impact_unavailable_line_count': 1,
                    'annualized_cost_change': None,
                    'annualized_customer_spend_change': None,
                    'annualized_gross_profit_change_proxy': None}],
        'category_groups': [{'request_id': 'MBR-TEST-1', 'prca': '100', 'category': 'GAUZE',
            'candidate_tier': 1, 'category_item_count': 2, 'baseline_sales_3m': '100',
            'baseline_margin': '0.2', 'tier_only_margin': '0.2', 'requested_price_margin': '0.1',
            'annualized_gp_change': '-40', 'annualized_internal_cost_change': '0',
            'annualized_customer_spend_change': '40', 'suggested_for_review': False,
            'screening_flags': [], 'exceptions': []}],
        'excluded_unpriced_lines': [{
            'request_id': 'MBR-TEST-1', 'line_key': 2, 'prca_num': '100',
            'account_provided': '900', 'item': '43', 'source_item_description': 'Missing price',
            'requested_uom': 'EA', 'requested_price': '',
            'price_exclusion_reason': 'MISSING_REQUESTED_PRICE', 'source_errors': ['INVALID_REQUESTED_PRICE'],
        }, {
            'request_id': 'MBR-TEST-2', 'line_key': 3, 'prca_num': '200',
            'account_provided': '200', 'item': '77', 'source_item_description': 'Zero price item',
            'requested_uom': 'BX', 'requested_price': '0',
            'price_exclusion_reason': 'ZERO_REQUESTED_PRICE', 'source_errors': ['INVALID_REQUESTED_PRICE'],
        }],
        'excluded_penny_contract_lines': [{
            'request_id': 'MBR-TEST-1', 'line_key': 4, 'prca_num': '100',
            'account_provided': '900', 'item': '44', 'source_item_description': 'Penny contract item',
            'requested_uom': 'EA', 'requested_price': '1', 'normalized_requested_price': '1',
            'sell_uom': 'EA', 'penny_tier_10_cost': '2', 'penny_product_types': 'DISPENSER',
            'penny_man_contract_ids': 'MCKB DISPENSERS', 'penny_mck_contract_ids': '87918',
            'penny_contract_ambiguous': False, 'penny_customer_status': 'Y',
            'penny_contract_exclusion_reason': 'PENNY_ITEM_CANNOT_ACHIEVE_POSITIVE_TIER_10_MARGIN',
        }],
        'excluded_penny_contract_activity': [{
            'prca_num': '100', 'item_e1_num': '88', 'item_dsc': 'Excluded penny activity',
            'grouped_lvl2': 'GAUZE', 'ipc_sell_uom': 'EA', 'qty_3_mth': 1, 'sls_3_mth': 2,
            'penny_customer_status': 'Y', 'penny_product_types': 'DISPENSER',
            'penny_man_contract_ids': 'MCKB DISPENSERS', 'penny_mck_contract_ids': '87918',
            'penny_contract_ambiguous': False,
        }],
        'uncertain_penny_contract_activity': [],
    }


class HtmlReviewTests(unittest.TestCase):
    def test_builds_index_and_one_page_per_request_with_escaped_values(self):
        pages = build_pages(example())
        self.assertEqual(len(pages), 3)
        index = pages['index.html']
        self.assertIn('MBR-TEST-1', index)
        self.assertIn('MBR-TEST-2', index)
        request_page = next(value for name, value in pages.items() if name.startswith('request-MBR-TEST-1-'))
        self.assertIn('MISSING_REQUESTED_PRICE', request_page)
        self.assertIn('PENNY_ITEM_CANNOT_ACHIEVE_POSITIVE_TIER_10_MARGIN', request_page)
        self.assertIn('Excluded penny activity', request_page)
        self.assertIn('Customer category net-impact scenarios', request_page)
        self.assertIn('Annualized Internal Cost Change', request_page)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', request_page)
        self.assertNotIn('<script>alert(1)</script>', request_page)
        self.assertIn('NOT APPROVED', request_page)
        self.assertIn('Source Account', request_page)

    def test_exports_to_new_directory_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'html'
            result = export_html(example(), output)
            self.assertEqual(result['pages'], 2)
            self.assertTrue((output / 'index.html').is_file())
            with self.assertRaisesRegex(ValueError, 'new HTML output directory'):
                export_html(example(), output)


if __name__ == '__main__':
    unittest.main()