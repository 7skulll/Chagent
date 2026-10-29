-- A refused repeated tool call ends the run as failed. Without this reason,
-- such a run could only report turn_limit, which misdescribes what happened:
-- the loop did not run out of turns, it stopped making progress.
ALTER TABLE runs DROP CONSTRAINT runs_end_reason_check;

ALTER TABLE runs ADD CONSTRAINT runs_end_reason_check CHECK (
    (status IN ('queued', 'running') AND end_reason IS NULL) OR
    (end_reason IS NOT NULL AND (
    (status = 'completed' AND end_reason IN (
        'model_stop', 'tool_requested_stop', 'explicit_memory', 'legacy_terminal')) OR
    (status = 'cancelled' AND end_reason IN ('user_cancelled', 'model_aborted', 'legacy_terminal')) OR
    (status = 'failed' AND end_reason IN (
        'turn_limit', 'repeated_tool_call', 'run_timeout', 'context_budget_exhausted', 'output_limit',
        'model_error', 'model_protocol_error', 'storage_error', 'execution_error', 'legacy_terminal')) OR
    (status = 'interrupted' AND end_reason IN ('owner_lost', 'legacy_terminal'))
    ))
);
