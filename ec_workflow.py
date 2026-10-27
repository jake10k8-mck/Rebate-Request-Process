"""Local approval foundation; no pricing policy, network calls, or production loads."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: str
    roles: frozenset[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def line_errors(line: dict, proposed_tier: int) -> list[str]:
    errors = list(line.get('validation_errors', []))
    if not str(line.get('item', '')).isascii() or not str(line.get('item', '')).isdigit():
        errors.append('INVALID_ITEM')
    try:
        price = Decimal(str(line.get('requested_price', '')))
        if not price.is_finite() or price <= 0:
            errors.append('INVALID_PRICE')
    except InvalidOperation:
        errors.append('INVALID_PRICE')
    if not line.get('reference_uom'):
        errors.append('MISSING_REFERENCE_UOM')
    elif not line.get('requested_uom') or line['requested_uom'] != line['reference_uom']:
        errors.append('UOM_MISMATCH')
    if line.get('recommended_tier') != proposed_tier:
        errors.append('CATEGORY_TIER_CONFLICT')
    return sorted(set(errors))


class Workflow:
    def __init__(self, database: str = ':memory:') -> None:
        self.connection = sqlite3.connect(database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        schema = Path(__file__).parent / 'sql' / 'ec_workflow.sqlite.sql'
        self.connection.executescript(schema.read_text(encoding='utf-8'))

    def close(self) -> None:
        self.connection.close()

    def add_evaluation(self, snapshot: dict) -> str:
        required = ('request_id', 'prca', 'category', 'requester_id', 'reviewer_id',
                    'source_hash', 'reference_hash', 'rule_version')
        if any(not isinstance(snapshot.get(key), str) or not snapshot[key].strip() for key in required):
            raise WorkflowError('Required source, identity, and rule metadata are missing')
        revision = snapshot.get('revision')
        tier = snapshot.get('proposed_tier')
        if type(revision) is not int or revision < 1 or type(tier) is not int or not 1 <= tier <= 10:
            raise WorkflowError('Revision must be positive and tier must be 1 through 10')
        expires_at = snapshot.get('expires_at', '')
        try:
            parsed_expiry = datetime.strptime(expires_at, '%Y-%m-%dT%H:%M:%SZ')
            if parsed_expiry.strftime('%Y-%m-%dT%H:%M:%SZ') != expires_at:
                raise ValueError('Noncanonical timestamp')
        except (ValueError, TypeError) as error:
            raise WorkflowError('Expiry must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ') from error
        lines = snapshot.get('lines', [])
        if not isinstance(lines, list) or not lines:
            raise WorkflowError('Evaluation must retain at least one requested source line')
        for line in lines:
            if not isinstance(line, dict):
                raise WorkflowError('Each source line must be an object')
            errors = line.get('validation_errors', [])
            if not isinstance(errors, list) or any(not isinstance(error, str) or not error.strip()
                                                   for error in errors):
                raise WorkflowError('Validation errors must be a list of nonempty codes')
        identifiers = [line.get('source_line_id') for line in lines]
        if any(not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers):
            raise WorkflowError('Each line requires a durable source_line_id')
        if len(identifiers) != len(set(identifiers)):
            raise WorkflowError('Duplicate source line identifiers')
        payload = canonical_json(snapshot)
        evaluation_id = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            if self.connection.execute('SELECT 1 FROM ec_evaluation WHERE evaluation_id = ?',
                                       (evaluation_id,)).fetchone():
                self.connection.commit()
                return evaluation_id
            previous = self.connection.execute(
                'SELECT evaluation.revision, decision.action FROM v_ec_latest_evaluation evaluation '
                'LEFT JOIN ec_decision decision ON decision.evaluation_id = evaluation.evaluation_id '
                'WHERE request_id = ? AND prca = ? AND category = ?',
                (snapshot['request_id'], snapshot['prca'], snapshot['category']),
            ).fetchone()
            if previous and (revision <= previous['revision'] or previous['action'] == 'APPROVE'):
                raise WorkflowError('Use a newer revision; approved groups require an amendment workflow')
            self.connection.execute(
                'INSERT INTO ec_evaluation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (evaluation_id, snapshot['request_id'], revision, snapshot['prca'], snapshot['category'],
                 snapshot['requester_id'], snapshot['reviewer_id'], snapshot.get('current_tier'),
                 tier, int(snapshot.get('rule_approved') is True), expires_at, utc_now(), payload),
            )
            for line in lines:
                self.connection.execute(
                    'INSERT INTO ec_line VALUES (?, ?, ?, ?, ?, ?)',
                    (evaluation_id, line['source_line_id'], str(line.get('item', '')),
                     str(line.get('requested_uom', '')), str(line.get('requested_price', '')),
                     '|'.join(line_errors(line, tier))),
                )
            self.connection.commit()
            return evaluation_id
        except Exception:
            self.connection.rollback()
            raise

    def decide(self, actor: Actor, evaluation_id: str, command_id: str, action: str,
               reason: str, approved_tier: int | None = None) -> None:
        if 'reviewer' not in actor.roles or not actor.user_id.strip():
            raise PermissionError('Reviewer role required from a trusted identity provider')
        if action not in {'APPROVE', 'REJECT', 'REQUEST_CORRECTION'}:
            raise WorkflowError('Unknown decision action')
        if not command_id.strip() or not reason.strip():
            raise WorkflowError('Command ID and rep-safe explanation are required')
        if action == 'APPROVE':
            if type(approved_tier) is not int or not 1 <= approved_tier <= 10:
                raise WorkflowError('Approval requires a tier from 1 through 10')
        elif approved_tier is not None:
            raise WorkflowError('Only approval can assign a tier')
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            existing = self.connection.execute(
                'SELECT * FROM ec_decision WHERE command_id = ?', (command_id,),
            ).fetchone()
            if existing:
                expected = (evaluation_id, action, approved_tier, actor.user_id, reason)
                actual = tuple(existing[key] for key in
                               ('evaluation_id', 'action', 'approved_tier', 'actor_id', 'rep_reason'))
                if expected != actual:
                    raise WorkflowError('Command ID was already used for a different decision')
                self.connection.commit()
                return
            evaluation = self.connection.execute(
                'SELECT * FROM v_ec_latest_evaluation WHERE evaluation_id = ?',
                (evaluation_id,),
            ).fetchone()
            if not evaluation or evaluation['expires_at'] <= utc_now():
                raise WorkflowError('Evaluation is superseded, missing, or expired')
            if evaluation['reviewer_id'] != actor.user_id:
                raise PermissionError('Only the assigned reviewer may decide this group')
            if self.connection.execute('SELECT 1 FROM ec_decision WHERE evaluation_id = ?',
                                       (evaluation_id,)).fetchone():
                raise WorkflowError('Evaluation already has a decision')
            if action == 'APPROVE':
                if approved_tier != evaluation['proposed_tier']:
                    raise WorkflowError('Tier override requires a new evaluated snapshot')
                invalid = self.connection.execute(
                    "SELECT 1 FROM ec_line WHERE evaluation_id = ? AND errors <> ''",
                    (evaluation_id,),
                ).fetchone()
                if invalid or not evaluation['rule_approved']:
                    raise WorkflowError('Invalid lines or unapproved pricing policy block approval')
                conflict = self.connection.execute(
                    "SELECT 1 FROM ec_decision decision JOIN ec_evaluation evaluation "
                    "ON evaluation.evaluation_id = decision.evaluation_id "
                    "WHERE evaluation.prca = ? AND evaluation.category = ? AND decision.action = 'APPROVE'",
                    (evaluation['prca'], evaluation['category']),
                ).fetchone()
                if conflict:
                    raise WorkflowError('Customer/category already has an unresolved approval')
            self.connection.execute(
                'INSERT INTO ec_decision VALUES (?, ?, ?, ?, ?, ?, ?)',
                (evaluation_id, command_id, action, approved_tier, actor.user_id, reason, utc_now()),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def review_queue(self, actor: Actor) -> list[dict]:
        if 'reviewer' not in actor.roles:
            raise PermissionError('Reviewer role required')
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM v_ec_review_queue WHERE reviewer_id = ? ORDER BY request_id, category',
            (actor.user_id,),
        )]

    def review_evaluation(self, actor: Actor, evaluation_id: str) -> dict:
        if 'reviewer' not in actor.roles:
            raise PermissionError('Reviewer role required')
        evaluation = self.connection.execute(
            'SELECT snapshot_json FROM ec_evaluation WHERE evaluation_id = ? AND reviewer_id = ?',
            (evaluation_id, actor.user_id),
        ).fetchone()
        if not evaluation:
            raise PermissionError('Evaluation is not assigned to this reviewer')
        return {'snapshot': json.loads(evaluation['snapshot_json']),
                'validated_lines': [dict(row) for row in self.connection.execute(
                    'SELECT * FROM ec_line WHERE evaluation_id = ? ORDER BY source_line_id',
                    (evaluation_id,),
                )]}

    def rep_requests(self, actor: Actor) -> list[dict]:
        if 'rep' not in actor.roles:
            raise PermissionError('Representative role required')
        return [dict(row) for row in self.connection.execute(
            'SELECT report.* FROM v_ec_rep_requests report WHERE requester_id = ? OR EXISTS '
            '(SELECT 1 FROM ec_account_access access WHERE access.user_id = ? AND access.prca = report.prca) '
            'ORDER BY request_id, category', (actor.user_id, actor.user_id),
        )]

    def rep_lines(self, actor: Actor, evaluation_id: str) -> list[dict]:
        self._require_rep_access(actor, evaluation_id)
        return [dict(row) for row in self.connection.execute(
            'SELECT source_line_id, item, requested_uom, requested_price, errors '
            'FROM ec_line WHERE evaluation_id = ? ORDER BY source_line_id', (evaluation_id,),
        )]

    def rep_history(self, actor: Actor, request_id: str) -> list[dict]:
        if 'rep' not in actor.roles:
            raise PermissionError('Representative role required')
        return [dict(row) for row in self.connection.execute(
            'SELECT history.request_id, history.prca, history.category, history.revision, '
            'history.action, history.approved_tier, history.rep_reason, history.decided_at '
            'FROM v_ec_decision_history history JOIN ec_evaluation evaluation '
            'ON evaluation.evaluation_id = history.evaluation_id WHERE history.request_id = ? '
            'AND (evaluation.requester_id = ? OR EXISTS (SELECT 1 FROM ec_account_access access '
            'WHERE access.user_id = ? AND access.prca = evaluation.prca)) '
            'ORDER BY history.revision, history.category', (request_id, actor.user_id, actor.user_id),
        )]

    def _require_rep_access(self, actor: Actor, evaluation_id: str) -> None:
        if 'rep' not in actor.roles:
            raise PermissionError('Representative role required')
        allowed = self.connection.execute(
            'SELECT 1 FROM ec_evaluation evaluation WHERE evaluation_id = ? '
            'AND (requester_id = ? OR EXISTS (SELECT 1 FROM ec_account_access access '
            'WHERE access.user_id = ? AND access.prca = evaluation.prca))',
            (evaluation_id, actor.user_id, actor.user_id),
        ).fetchone()
        if not allowed:
            raise PermissionError('Evaluation is not available to this representative')


def demo() -> dict:
    workflow = Workflow()
    try:
        snapshot = {
            'request_id': 'DEMO-001', 'revision': 1, 'prca': 'DEMO-CUSTOMER',
            'category': 'DEMO-CATEGORY', 'requester_id': 'demo-rep',
            'reviewer_id': 'demo-reviewer', 'source_hash': 'synthetic-source',
            'reference_hash': 'synthetic-reference', 'rule_version': 'UNAPPROVED-DEMO',
            'rule_approved': False, 'current_tier': 3, 'proposed_tier': 4,
            'expires_at': '2099-01-01T00:00:00Z',
            'lines': [{'source_line_id': 'demo-attachment:row-1', 'item': '123456',
                       'requested_uom': 'EA', 'reference_uom': 'EA',
                       'requested_price': '10.25', 'recommended_tier': 4}],
        }
        evaluation_id = workflow.add_evaluation(snapshot)
        reviewer = Actor('demo-reviewer', frozenset({'reviewer'}))
        rep = Actor('demo-rep', frozenset({'rep'}))
        queue = workflow.review_queue(reviewer)
        workflow.decide(reviewer, evaluation_id, 'demo-correction', 'REQUEST_CORRECTION',
                        'Pricing policy needs confirmation before this request can be approved.')
        return {'mode': 'LOCAL SYNTHETIC DEMO - no external systems contacted',
                'initial_review_queue': queue, 'rep_requests': workflow.rep_requests(rep),
                'rep_lines': workflow.rep_lines(rep, evaluation_id),
                'rep_decision_history': workflow.rep_history(rep, 'DEMO-001')}
    finally:
        workflow.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['demo'])
    parser.parse_args()
    print(json.dumps(demo(), indent=2))