import unittest
from decimal import Decimal

from ec_review import (calculate_scenarios, group_scenarios, normalize_requested_price_to_sell_uom,
                       partition_penny_contract_lines, partition_unpriced_lines,
                       attach_excluded_reference_context, prepare_requests, spreadsheet_cell)


def fixture():
    preview = {'source': {'headers': [{'ROW_ID': 'request-1', 'CUST_NUM_PRCA': '12345'}],
                          'lines': [{'ROW_ID': 'request-1', 'ITEM_E1_NUM': '654321.0',
                                     'REQ_UOM': 'EA', 'REQ_PRICE': '10.2500'}]}}
    inputs, context = prepare_requests(preview)
    references = [{'line_key': 1, 'tier_num': tier, 'customer_matches': 1, 'item_matches': 1,
                   'pex_matches': 1, 'tapestry_matches': 1, 'bus_pltfrm': 'EC',
                   'sell_uom': 'EA', 'prca_num': '12345', 'grouped_lvl2': 'GAUZE',
                   'attachment_matches': 1, 'current_tier': 3, 'current_tier_cost': Decimal('6'),
                   'tier_cost': Decimal('7'), 'ipc_all_rows': 2, 'ipc_matched_rows': 2,
                   'qty_3_mth': Decimal('100'), 'sls_3_mth': Decimal('900'),
                   'negative_activity_rows': 0} for tier in range(1, 11)]
    return inputs, context, references


class ReviewTests(unittest.TestCase):
    def test_unpriced_lines_are_separated_with_specific_reasons(self):
        lines = [
            {'line_key': 1, 'requested_price': ''},
            {'line_key': 2, 'requested_price': '0'},
            {'line_key': 3, 'requested_price': '-2.50'},
            {'line_key': 4, 'requested_price': 'bad'},
            {'line_key': 5, 'requested_price': '12.25'},
        ]

        eligible, excluded = partition_unpriced_lines(lines)

        self.assertEqual([line['line_key'] for line in eligible], [5])
        self.assertEqual([line['price_exclusion_reason'] for line in excluded], [
            'MISSING_REQUESTED_PRICE', 'ZERO_REQUESTED_PRICE',
            'NEGATIVE_REQUESTED_PRICE', 'INVALID_REQUESTED_PRICE',
        ])

    def test_excluded_lines_keep_resolved_customer_attribution(self):
        excluded = [{'line_key': 2, 'account_provided': '91053545',
                     'price_exclusion_reason': 'MISSING_REQUESTED_PRICE'}]
        references = [{'line_key': 2, 'tier_num': 1, 'PRCA_NUM': '20149288',
                       'GROUPED_LVL2': 'WOUND CARE'},
                      {'line_key': 2, 'tier_num': 2, 'PRCA_NUM': '20149288'}]

        result = attach_excluded_reference_context(excluded, references)

        self.assertEqual(result[0]['account_provided'], '91053545')
        self.assertEqual(result[0]['prca_num'], '20149288')
        self.assertEqual(result[0]['grouped_lvl2'], 'WOUND CARE')

    def test_requested_price_normalizes_buy_and_primary_uoms_to_sell_basis(self):
        buy_price, buy_factor, buy_method = normalize_requested_price_to_sell_uom(
            {'requested_price': '10', 'requested_uom': 'CS'},
            {'sell_uom': 'EA', 'buy_uom': 'CS', 'prmry_uom': 'EA',
             'sell_to_buy_divisor': '10', 'prmry_to_sell_divisor': '1'})
        primary_price, primary_factor, primary_method = normalize_requested_price_to_sell_uom(
            {'requested_price': '1', 'requested_uom': 'EA'},
            {'sell_uom': 'CS', 'buy_uom': 'CS', 'prmry_uom': 'EA',
             'sell_to_buy_divisor': '1', 'prmry_to_sell_divisor': '10'})

        self.assertEqual((buy_price, buy_factor, buy_method),
                         (Decimal('1'), '1/10', 'BUY_TO_SELL_DIVISOR'))
        self.assertEqual((primary_price, primary_factor, primary_method),
                         (Decimal('10'), '10', 'PRIMARY_TO_SELL_DIVISOR'))

    def test_ambiguous_conversion_and_missing_factor_are_unknown(self):
        ambiguous = normalize_requested_price_to_sell_uom(
            {'requested_price': '1', 'requested_uom': 'EA'},
            {'sell_uom': 'CS', 'buy_uom': 'EA', 'prmry_uom': 'EA',
             'sell_to_buy_divisor': '10', 'prmry_to_sell_divisor': '12'})
        missing = normalize_requested_price_to_sell_uom(
            {'requested_price': '1', 'requested_uom': 'EA'},
            {'sell_uom': 'CS', 'buy_uom': 'CS', 'prmry_uom': 'EA',
             'prmry_to_sell_divisor': None})

        self.assertEqual(ambiguous, (None, None, 'UNKNOWN_AMBIGUOUS_UOM_CONVERSION'))
        self.assertEqual(missing, (None, None, 'UNKNOWN_MISSING_PRIMARY_TO_SELL_DIVISOR'))

    def test_penny_price_gate_excludes_at_or_below_tier_ten_and_keeps_unknown_uom(self):
        lines = [
            {'line_key': 1, 'requested_price': '10', 'requested_uom': 'EA'},
            {'line_key': 2, 'requested_price': '10', 'requested_uom': 'BOX'},
            {'line_key': 3, 'requested_price': '11', 'requested_uom': 'EA'},
        ]
        references = []
        for line_key, tier_ten_cost, penny_flag in [(1, '10', 'Y'), (2, '10', 'Y'), (3, '10', 'Y')]:
            references.append({'line_key': line_key, 'tier_num': 10, 'tier_cost': tier_ten_cost,
                'penny_contract_flag': penny_flag, 'penny_product_types': 'DISPENSER',
                'penny_man_contract_ids': 'MCKB DISPENSERS', 'penny_mck_contract_ids': '87918',
                'penny_contract_match_count': 1, 'penny_contract_ambiguous': False,
                'sell_uom': 'EA', 'buy_uom': 'CS', 'prmry_uom': 'EA',
                'sell_to_buy_divisor': '10', 'prmry_to_sell_divisor': '1'})

        eligible, excluded = partition_penny_contract_lines(lines, references)

        self.assertEqual([row['line_key'] for row in excluded], [1])
        self.assertEqual(excluded[0]['penny_contract_required_flag'], 'Y')
        self.assertEqual(excluded[0]['normalized_requested_price'], '10')
        self.assertEqual([row['line_key'] for row in eligible], [2, 3])
        self.assertEqual(eligible[0]['penny_contract_required_flag'], 'UNKNOWN')
        self.assertEqual(eligible[0]['penny_price_test_status'], 'UNKNOWN_UNSUPPORTED_UOM_CONVERSION')

    def test_all_tiers_preserved_unchanged_price_and_impact_signs(self) -> None:
        inputs, context, references = fixture()
        self.assertEqual(inputs[0]['item'], '654321')
        rows = calculate_scenarios(context, references)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]['fixed_price_if_approved'], '10.2500')
        self.assertEqual(rows[0]['cost_change_3m'], '100.00')
        self.assertEqual(rows[0]['annualized_cost_change'], '400.00')
        self.assertEqual(rows[0]['annualized_customer_spend_change'], '500.00')
        self.assertEqual(rows[0]['annualized_gross_profit_change_proxy'], '100.00')
        self.assertIsNone(rows[0]['recommended_tier'])
        self.assertEqual(rows[0]['approval_status'], 'NOT_APPROVED')

    def test_uom_missing_data_ambiguity_and_non_ec_never_become_valid(self) -> None:
        for change in ({'sell_uom': 'BX'}, {'customer_matches': 2}, {'bus_pltfrm': 'PC'},
                       {'tier_cost': None}, {'item_matches': None}):
            with self.subTest(change=change):
                _, context, references = fixture()
                references[0].update(change)
                row = calculate_scenarios(context, references)[0]
                self.assertTrue(row['validation_errors'])
                self.assertIsNone(row['fixed_price_if_approved'])
                self.assertIsNone(row['annualized_cost_change'])

    def test_no_sales_zero_usage_and_mixed_uoms_do_not_produce_zero_impact(self) -> None:
        for change in ({'ipc_all_rows': None, 'qty_3_mth': None, 'sls_3_mth': None},
                       {'qty_3_mth': Decimal('0')}, {'ipc_matched_rows': 1},
                       {'attachment_matches': 2}, {'negative_activity_rows': 1}):
            with self.subTest(change=change):
                _, context, references = fixture()
                references[0].update(change)
                row = calculate_scenarios(context, references)[0]
                self.assertIsNone(row['annualized_cost_change'])
                self.assertTrue(row['impact_exceptions'])

    def test_missing_account_keeps_item_margin_but_never_fabricates_customer_impact(self) -> None:
        _, context, references = fixture()
        context[0]['source_errors'] = ['MISSING_ACCOUNT_REFERENCE']
        for reference in references:
            reference.update(customer_matches=None, pex_matches=None, bus_pltfrm=None,
                             current_tier=None, current_tier_cost=None, attachment_matches=None,
                             ipc_all_rows=None, ipc_matched_rows=None, qty_3_mth=None, sls_3_mth=None)
        row = calculate_scenarios(context, references)[0]
        self.assertEqual(row['requested_margin_at_candidate_cost'], '0.317073')
        self.assertIsNone(row['annualized_cost_change'])
        self.assertIsNone(row['fixed_price_if_approved'])
        self.assertIn('MISSING_ACCOUNT_REFERENCE', row['validation_errors'])

    def test_missing_requested_uom_uses_sell_uom_and_records_assumption(self) -> None:
        _, context, references = fixture()
        context[0]['requested_uom'] = None
        rows = calculate_scenarios(context, references)
        self.assertTrue(all(row['uom_assumed_from_sell_uom'] for row in rows))
        self.assertTrue(all(not row['validation_errors'] for row in rows))
        self.assertEqual(rows[0]['requested_margin_at_candidate_cost'], '0.317073')
        self.assertEqual(rows[0]['fixed_price_if_approved'], '10.2500')

    def test_missing_or_fanned_out_tiers_fail_reconciliation(self) -> None:
        _, context, references = fixture()
        with self.assertRaises(ValueError):
            calculate_scenarios(context, references[:-1])
        with self.assertRaises(ValueError):
            calculate_scenarios(context, references + references[:1])

    def test_group_does_not_double_count_repeated_requested_item(self) -> None:
        _, context, references = fixture()
        rows = calculate_scenarios(context, references)
        group = group_scenarios(rows)[0]
        self.assertEqual(group['annualized_cost_change'], '400.00')
        duplicate = dict(rows[0], line_key=2)
        group = group_scenarios(rows + [duplicate])[0]
        self.assertIsNone(group['annualized_cost_change'])
        self.assertEqual(group['aggregation_exception'], 'REPEATED_ITEM_IN_REQUEST_GROUP')

    def test_ambiguous_headers_not_arbitrarily_selected_and_csv_is_safe(self) -> None:
        preview = {'source': {'headers': [{'row_id': 'request-1', 'cust_num_prca': '123'},
                                         {'row_id': 'request-1', 'cust_num_prca': '456'}],
                              'lines': [{'row_id': 'request-1', 'item_e1_num': '123',
                                         'req_price': '10', 'req_uom': 'EA'}]}}
        inputs, context = prepare_requests(preview)
        self.assertIsNone(inputs[0]['account_number'])
        self.assertIn('MISSING_OR_AMBIGUOUS_HEADER', context[0]['source_errors'])
        self.assertEqual(spreadsheet_cell('=HYPERLINK("x")'), '\'=HYPERLINK("x")')
        self.assertEqual(spreadsheet_cell('-100.25'), '-100.25')


if __name__ == '__main__':
    unittest.main()