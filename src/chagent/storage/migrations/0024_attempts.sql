-- How many times this exact invocation has been attempted in this run (batch 1B).
--
-- An attempt is one execution of one call. The model may retry the same tool with
-- the same arguments after a failure; that is a NEW row with a new attempt number,
-- a new tool record and — when it is gated — a new approval question. Nothing is
-- transferred from the earlier attempt: not the ticket, not the decision.
--
-- Distinct from `tools/progress.py`'s repeat guard, which counts consecutive
-- identical calls to stop a loop. This column is audit, that one is a brake.
ALTER TABLE tool_calls ADD COLUMN attempt smallint NOT NULL DEFAULT 1;

CREATE INDEX tool_calls_attempt_idx ON tool_calls (run_id, name, attempt);
