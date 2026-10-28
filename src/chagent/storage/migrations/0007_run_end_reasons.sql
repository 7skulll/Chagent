ALTER TABLE runs ADD COLUMN end_reason text;

-- Historical terminal rows have no evidence of their exact stopping reason.
UPDATE runs SET end_reason = 'legacy_terminal'
WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted');

ALTER TABLE runs ADD CONSTRAINT runs_end_reason_check CHECK (
    (status IN ('queued', 'running') AND end_reason IS NULL) OR
    (end_reason IS NOT NULL AND (
    (status = 'completed' AND end_reason IN ('model_stop', 'tool_requested_stop', 'legacy_terminal')) OR
    (status = 'cancelled' AND end_reason IN ('user_cancelled', 'model_aborted', 'legacy_terminal')) OR
    (status = 'failed' AND end_reason IN (
        'turn_limit', 'run_timeout', 'context_budget_exhausted', 'output_limit',
        'model_error', 'model_protocol_error', 'storage_error', 'execution_error', 'legacy_terminal')) OR
    (status = 'interrupted' AND end_reason IN ('owner_lost', 'legacy_terminal'))
    ))
);
