CREATE TABLE model_calls (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    iteration integer NOT NULL CHECK (iteration >= 0),
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
    request jsonb NOT NULL,
    context_snapshot jsonb NOT NULL,
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    UNIQUE (run_id, iteration),
    UNIQUE (run_id, id)
);

CREATE TABLE tool_calls (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    model_call_id uuid NOT NULL,
    call_id text NOT NULL,
    name text NOT NULL,
    arguments jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'unknown')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    FOREIGN KEY (run_id, model_call_id) REFERENCES model_calls(run_id, id),
    UNIQUE (model_call_id, call_id)
);

CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES runs(id),
    sequence bigint NOT NULL,
    type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, sequence)
);
