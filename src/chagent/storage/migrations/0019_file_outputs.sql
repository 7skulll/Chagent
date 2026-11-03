CREATE TABLE file_outputs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    session_id uuid REFERENCES sessions(id),
    run_id uuid REFERENCES runs(id),
    tool_call_id text,
    source text NOT NULL,
    path text NOT NULL,
    payload bytea,
    content_hash text NOT NULL,
    size integer NOT NULL,
    status text NOT NULL CHECK (status IN ('staged','pending','committed','cancelled','expired','failed')),
    token uuid NOT NULL,
    expected_hash text,
    expected_identity text,
    output_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    decided_at timestamptz,
    UNIQUE (run_id, tool_call_id)
);
CREATE INDEX file_outputs_pending ON file_outputs(workspace_id, status, created_at);
CREATE TABLE file_operations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source text NOT NULL,
    operation text NOT NULL,
    path text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
