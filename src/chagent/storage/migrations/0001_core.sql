CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    root_path text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    title text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_workspace_idx ON sessions (workspace_id, created_at);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    ended_at timestamptz,
    UNIQUE (session_id, id)
);
CREATE INDEX runs_session_idx ON runs (session_id, created_at);
CREATE UNIQUE INDEX runs_one_active_per_session_idx ON runs (session_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE messages (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id),
    run_id uuid,
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    role text NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'toolResult')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, id)
);
CREATE INDEX messages_session_sequence_idx ON messages (session_id, sequence);
