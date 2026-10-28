ALTER TABLE tool_calls DROP CONSTRAINT tool_calls_status_check;
ALTER TABLE tool_calls ADD CONSTRAINT tool_calls_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'failed', 'unknown', 'cancelled'));

ALTER TABLE tool_calls ADD COLUMN call_index integer;
WITH positions AS (
    SELECT id, row_number() OVER (PARTITION BY model_call_id ORDER BY created_at, id) - 1 AS position
    FROM tool_calls
)
UPDATE tool_calls t SET call_index = p.position FROM positions p WHERE t.id = p.id;
ALTER TABLE tool_calls ALTER COLUMN call_index SET NOT NULL;
ALTER TABLE tool_calls ADD CONSTRAINT tool_calls_index_check CHECK (call_index >= 0);
ALTER TABLE tool_calls ADD CONSTRAINT tool_calls_model_index_unique UNIQUE (model_call_id, call_index);
