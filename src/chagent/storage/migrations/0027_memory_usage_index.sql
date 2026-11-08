-- Usage is derived from immutable request snapshots when the user opens a
-- memory's detail; no write is added to the model-call path.
CREATE INDEX model_calls_memory_references_idx ON model_calls
    USING gin ((context_snapshot->'memory_references')) WHERE iteration = 0;
