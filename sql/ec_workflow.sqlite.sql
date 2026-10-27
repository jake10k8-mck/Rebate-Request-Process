PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ec_evaluation (
    evaluation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    prca TEXT NOT NULL,
    category TEXT NOT NULL,
    requester_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    current_tier INTEGER CHECK (current_tier BETWEEN 1 AND 10),
    proposed_tier INTEGER NOT NULL CHECK (proposed_tier BETWEEN 1 AND 10),
    rule_approved INTEGER NOT NULL CHECK (rule_approved IN (0, 1)),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    UNIQUE (request_id, prca, category, revision)
);

CREATE TABLE IF NOT EXISTS ec_line (
    evaluation_id TEXT NOT NULL REFERENCES ec_evaluation(evaluation_id),
    source_line_id TEXT NOT NULL,
    item TEXT NOT NULL,
    requested_uom TEXT NOT NULL,
    requested_price TEXT NOT NULL,
    errors TEXT NOT NULL,
    PRIMARY KEY (evaluation_id, source_line_id)
);

CREATE TABLE IF NOT EXISTS ec_decision (
    evaluation_id TEXT PRIMARY KEY REFERENCES ec_evaluation(evaluation_id),
    command_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK (action IN ('APPROVE', 'REJECT', 'REQUEST_CORRECTION')),
    approved_tier INTEGER CHECK (approved_tier BETWEEN 1 AND 10),
    actor_id TEXT NOT NULL,
    rep_reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    CHECK ((action = 'APPROVE' AND approved_tier IS NOT NULL)
        OR (action <> 'APPROVE' AND approved_tier IS NULL))
);

CREATE TABLE IF NOT EXISTS ec_account_access (
    user_id TEXT NOT NULL,
    prca TEXT NOT NULL,
    PRIMARY KEY (user_id, prca)
);

CREATE VIEW IF NOT EXISTS v_ec_latest_evaluation AS
SELECT evaluation.*
FROM ec_evaluation evaluation
WHERE NOT EXISTS (
    SELECT 1 FROM ec_evaluation newer
    WHERE newer.request_id = evaluation.request_id
      AND newer.prca = evaluation.prca
      AND newer.category = evaluation.category
      AND newer.revision > evaluation.revision
);

CREATE VIEW IF NOT EXISTS v_ec_review_queue AS
SELECT evaluation.evaluation_id, evaluation.request_id, evaluation.revision,
       evaluation.prca, evaluation.category, evaluation.reviewer_id,
       evaluation.current_tier, evaluation.proposed_tier, evaluation.expires_at,
       COUNT(line.source_line_id) AS line_count,
       SUM(CASE WHEN line.errors <> '' THEN 1 ELSE 0 END) AS invalid_line_count,
       CASE
           WHEN decision.action = 'APPROVE' THEN 'Approved - not sent for loading'
           WHEN decision.action = 'REJECT' THEN 'Rejected'
           WHEN decision.action = 'REQUEST_CORRECTION' THEN 'Correction requested'
           WHEN evaluation.expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now') THEN 'Needs reevaluation'
           WHEN evaluation.rule_approved = 0 THEN 'Awaiting pricing policy approval'
           WHEN SUM(CASE WHEN line.errors <> '' THEN 1 ELSE 0 END) > 0 THEN 'Needs correction'
           ELSE 'Ready for review'
       END AS status,
       decision.rep_reason, decision.decided_at
FROM v_ec_latest_evaluation evaluation
JOIN ec_line line ON line.evaluation_id = evaluation.evaluation_id
LEFT JOIN ec_decision decision ON decision.evaluation_id = evaluation.evaluation_id
GROUP BY evaluation.evaluation_id, evaluation.request_id, evaluation.revision,
         evaluation.prca, evaluation.category, evaluation.reviewer_id,
         evaluation.current_tier, evaluation.proposed_tier, evaluation.expires_at,
         evaluation.rule_approved, decision.action, decision.rep_reason, decision.decided_at;

CREATE VIEW IF NOT EXISTS v_ec_rep_requests AS
SELECT queue.request_id, queue.revision, queue.prca, queue.category,
    evaluation.requester_id, queue.evaluation_id,
      CASE WHEN decision.action = 'REQUEST_CORRECTION' THEN evaluation.requester_id
          WHEN decision.action IS NULL THEN queue.reviewer_id
          ELSE NULL END AS next_owner,
       queue.status, queue.line_count, queue.invalid_line_count,
       decision.approved_tier, queue.rep_reason, queue.decided_at,
       evaluation.created_at AS evaluated_at
FROM v_ec_review_queue queue
JOIN ec_evaluation evaluation ON evaluation.evaluation_id = queue.evaluation_id
LEFT JOIN ec_decision decision ON decision.evaluation_id = queue.evaluation_id;

CREATE VIEW IF NOT EXISTS v_ec_review_lines AS
SELECT evaluation.request_id, evaluation.revision, evaluation.prca,
       evaluation.category, evaluation.requester_id, evaluation.reviewer_id,
       evaluation.proposed_tier, line.*
FROM ec_evaluation evaluation
JOIN ec_line line ON line.evaluation_id = evaluation.evaluation_id;

CREATE VIEW IF NOT EXISTS v_ec_decision_history AS
SELECT evaluation.request_id, evaluation.prca, evaluation.category,
       evaluation.revision, decision.*
FROM ec_decision decision
JOIN ec_evaluation evaluation ON evaluation.evaluation_id = decision.evaluation_id;

CREATE TRIGGER IF NOT EXISTS ec_evaluation_no_update
BEFORE UPDATE ON ec_evaluation BEGIN SELECT RAISE(ABORT, 'Immutable evaluation'); END;
CREATE TRIGGER IF NOT EXISTS ec_evaluation_no_delete
BEFORE DELETE ON ec_evaluation BEGIN SELECT RAISE(ABORT, 'Immutable evaluation'); END;
CREATE TRIGGER IF NOT EXISTS ec_line_no_update
BEFORE UPDATE ON ec_line BEGIN SELECT RAISE(ABORT, 'Immutable line'); END;
CREATE TRIGGER IF NOT EXISTS ec_line_no_delete
BEFORE DELETE ON ec_line BEGIN SELECT RAISE(ABORT, 'Immutable line'); END;
CREATE TRIGGER IF NOT EXISTS ec_decision_no_update
BEFORE UPDATE ON ec_decision BEGIN SELECT RAISE(ABORT, 'Immutable decision'); END;
CREATE TRIGGER IF NOT EXISTS ec_decision_no_delete
BEFORE DELETE ON ec_decision BEGIN SELECT RAISE(ABORT, 'Immutable decision'); END;