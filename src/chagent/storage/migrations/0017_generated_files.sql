CREATE TABLE generated_files (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id),
    run_id uuid NOT NULL,
    tool_call_id text NOT NULL,
    name text NOT NULL,
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, id),
    UNIQUE (run_id, tool_call_id)
);
CREATE INDEX generated_files_session_idx ON generated_files(session_id, created_at DESC);
