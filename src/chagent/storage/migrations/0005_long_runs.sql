ALTER TABLE session_summaries ADD COLUMN source_run_id uuid REFERENCES runs(id);
ALTER TABLE session_summaries ADD COLUMN protected_input_id uuid REFERENCES messages(id);

CREATE TABLE tool_artifacts (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id),
    run_id uuid NOT NULL,
    tool_record_id uuid NOT NULL UNIQUE REFERENCES tool_calls(id),
    content text NOT NULL,
    char_count bigint NOT NULL,
    byte_count bigint NOT NULL,
    sha256 text NOT NULL,
    source_truncated boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, id)
);
CREATE INDEX tool_artifacts_session_idx ON tool_artifacts(session_id);
