import copy
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ec_workflow import Actor, Workflow, WorkflowError, demo


def example() -> dict:
    return {
        'request_id': 'demo-request', 'revision': 1, 'prca': 'demo-customer',
        'category': 'GAUZE', 'requester_id': 'rep-a', 'reviewer_id': 'reviewer-a',
        'source_hash': 'demo-source', 'reference_hash': 'demo-reference',
        'rule_version': 'TEST-ONLY', 'rule_approved': True,
        'current_tier': 3, 'proposed_tier': 4, 'expires_at': '2099-01-01T00:00:00Z',
        'lines': [{'source_line_id': 'attachment-1:row-8', 'item': '123456',
                   'requested_uom': 'EA', 'reference_uom': 'EA',
                   'requested_price': '10.25', 'recommended_tier': 4}],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = Workflow()
        self.addCleanup(self.workflow.close)
        self.reviewer = Actor('reviewer-a', frozenset({'reviewer'}))

    def approve(self, evaluation_id: str, command: str = 'command-1') -> None:
        self.workflow.decide(self.reviewer, evaluation_id, command, 'APPROVE', 'Approved test case', 4)

    def test_approval_is_versioned_idempotent_and_not_loaded(self) -> None:
        snapshot = example()
        evaluation_id = self.workflow.add_evaluation(snapshot)
        self.assertEqual(evaluation_id, self.workflow.add_evaluation(snapshot))
        self.approve(evaluation_id)
        self.approve(evaluation_id)
        rows = self.workflow.rep_requests(Actor('rep-a', frozenset({'rep'})))
        self.assertEqual(rows[0]['status'], 'Approved - not sent for loading')
        self.assertEqual(rows[0]['approved_tier'], 4)
        self.assertNotIn('current_tier', rows[0])
        with self.assertRaises(WorkflowError):
            self.workflow.decide(self.reviewer, evaluation_id, 'command-1', 'REJECT', 'Changed mind')

    def test_invalid_lines_and_policy_block_even_override(self) -> None:
        for change in ({'requested_price': '#N/A'}, {'requested_price': 'NaN'},
                       {'requested_price': '0'}, {'requested_uom': 'BX'},
                       {'reference_uom': None}, {'recommended_tier': 5},
                       {'validation_errors': ['MISSING_CONTRACT_COST']}):
            with self.subTest(change=change):
                snapshot = example()
                snapshot['request_id'] = repr(change)
                snapshot['lines'][0].update(change)
                evaluation_id = self.workflow.add_evaluation(snapshot)
                with self.assertRaises(WorkflowError):
                    self.approve(evaluation_id)
        snapshot = example()
        snapshot.pop('rule_approved')
        evaluation_id = self.workflow.add_evaluation(snapshot)
        with self.assertRaises(WorkflowError):
            self.approve(evaluation_id)

    def test_superseded_expired_and_changed_revisions_block(self) -> None:
        snapshot = example()
        original = self.workflow.add_evaluation(snapshot)
        snapshot['source_hash'] = 'changed'
        with self.assertRaises(WorkflowError):
            self.workflow.add_evaluation(snapshot)
        snapshot['revision'] = 2
        self.workflow.add_evaluation(snapshot)
        with self.assertRaises(WorkflowError):
            self.approve(original)
        snapshot['revision'] = 3
        snapshot['expires_at'] = '2000-01-01T00:00:00Z'
        expired = self.workflow.add_evaluation(snapshot)
        with self.assertRaises(WorkflowError):
            self.approve(expired)

    def test_conflicting_customer_category_approval_is_blocked(self) -> None:
        snapshot = example()
        self.approve(self.workflow.add_evaluation(snapshot))
        snapshot['request_id'] = 'second-request'
        other = self.workflow.add_evaluation(snapshot)
        with self.assertRaises(WorkflowError):
            self.approve(other, 'command-2')
        snapshot['request_id'] = 'demo-request'
        snapshot['revision'] = 2
        with self.assertRaises(WorkflowError):
            self.workflow.add_evaluation(snapshot)

    def test_rep_scope_and_reviewer_assignment(self) -> None:
        evaluation_id = self.workflow.add_evaluation(example())
        stranger = Actor('rep-b', frozenset({'rep'}))
        self.assertEqual(self.workflow.rep_requests(stranger), [])
        self.workflow.connection.execute('INSERT INTO ec_account_access VALUES (?, ?)',
                                         ('rep-b', 'demo-customer'))
        self.assertEqual(len(self.workflow.rep_requests(stranger)), 1)
        with self.assertRaises(PermissionError):
            self.workflow.review_queue(stranger)
        with self.assertRaises(PermissionError):
            self.workflow.decide(Actor('reviewer-b', frozenset({'reviewer'})),
                                 evaluation_id, 'other', 'REJECT', 'Not assigned')

    def test_duplicate_looking_lines_retained_and_tables_immutable(self) -> None:
        snapshot = example()
        other = copy.deepcopy(snapshot['lines'][0])
        other['source_line_id'] = 'attachment-1:row-9'
        snapshot['lines'].append(other)
        self.workflow.add_evaluation(snapshot)
        self.assertEqual(self.workflow.review_queue(self.reviewer)[0]['line_count'], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.workflow.connection.execute('DELETE FROM ec_evaluation')

    def test_correction_then_resubmission_and_partial_request(self) -> None:
        snapshot = example()
        evaluation_id = self.workflow.add_evaluation(snapshot)
        self.workflow.decide(self.reviewer, evaluation_id, 'correction', 'REQUEST_CORRECTION',
                             'Please confirm the requested UOM')
        snapshot['revision'] = 2
        self.approve(self.workflow.add_evaluation(snapshot))
        snapshot['category'] = 'GLOVES'
        rejected = self.workflow.add_evaluation(snapshot)
        self.workflow.decide(self.reviewer, rejected, 'reject', 'REJECT', 'Not eligible under test policy')
        rows = self.workflow.rep_requests(Actor('rep-a', frozenset({'rep'})))
        self.assertEqual({row['status'] for row in rows}, {'Rejected', 'Approved - not sent for loading'})
        history = self.workflow.connection.execute('SELECT COUNT(*) FROM v_ec_decision_history').fetchone()[0]
        self.assertEqual(history, 3)

    def test_rep_line_and_history_scope_cannot_be_bypassed_by_identifiers(self) -> None:
        evaluation_id = self.workflow.add_evaluation(example())
        self.approve(evaluation_id)
        stranger = Actor('rep-b', frozenset({'rep'}))
        with self.assertRaises(PermissionError):
            self.workflow.rep_lines(stranger, evaluation_id)
        self.assertEqual(self.workflow.rep_history(stranger, 'demo-request'), [])
        rep = Actor('rep-a', frozenset({'rep'}))
        self.assertEqual(self.workflow.rep_lines(rep, evaluation_id)[0]['requested_price'], '10.25')
        history = self.workflow.rep_history(rep, 'demo-request')
        self.assertEqual(history[0]['rep_reason'], 'Approved test case')
        self.assertNotIn('actor_id', history[0])

    def test_rejects_noncanonical_dates_and_malformed_validation_errors(self) -> None:
        snapshot = example()
        snapshot['expires_at'] = '2099-1-1T00:00:00Z'
        with self.assertRaises(WorkflowError):
            self.workflow.add_evaluation(snapshot)
        snapshot = example()
        snapshot['lines'][0]['validation_errors'] = 'MISSING_COST'
        with self.assertRaises(WorkflowError):
            self.workflow.add_evaluation(snapshot)

    def test_demo_never_approves_unvalidated_pricing_policy(self) -> None:
        result = demo()
        self.assertEqual(result['initial_review_queue'][0]['status'], 'Awaiting pricing policy approval')
        self.assertEqual(result['rep_requests'][0]['status'], 'Correction requested')
        self.assertEqual(result['rep_requests'][0]['next_owner'], 'demo-rep')
        self.assertEqual(len(result['rep_decision_history']), 1)

    def test_unevaluated_override_blocked_and_reviewer_can_inspect_snapshot(self) -> None:
        evaluation_id = self.workflow.add_evaluation(example())
        detail = self.workflow.review_evaluation(self.reviewer, evaluation_id)
        self.assertEqual(detail['snapshot']['rule_version'], 'TEST-ONLY')
        self.assertEqual(detail['validated_lines'][0]['errors'], '')
        with self.assertRaises(WorkflowError):
            self.workflow.decide(self.reviewer, evaluation_id, 'override', 'APPROVE', 'Try tier 5', 5)
        with self.assertRaises(PermissionError):
            self.workflow.review_evaluation(Actor('rep-a', frozenset({'rep'})), evaluation_id)

    def test_concurrent_category_approvals_only_one_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / 'workflow.sqlite3')
            seed = Workflow(database)
            try:
                snapshot = example()
                first = seed.add_evaluation(snapshot)
                snapshot['request_id'] = 'other-request'
                second = seed.add_evaluation(snapshot)
            finally:
                seed.close()
            barrier = threading.Barrier(2)

            def attempt(evaluation_id: str) -> bool:
                worker = Workflow(database)
                try:
                    barrier.wait(timeout=10)
                    worker.decide(self.reviewer, evaluation_id, evaluation_id,
                                  'APPROVE', 'Concurrent test approval', 4)
                    return True
                except WorkflowError:
                    return False
                finally:
                    worker.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, [first, second]))
            self.assertEqual(sorted(results), [False, True])


if __name__ == '__main__':
    unittest.main()