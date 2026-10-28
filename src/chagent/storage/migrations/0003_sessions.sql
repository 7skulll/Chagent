ALTER TABLE sessions ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE sessions ADD COLUMN archived_at timestamptz;
CREATE INDEX sessions_created_page_idx ON sessions (created_at DESC, id DESC);

CREATE TABLE task_states (
    session_id uuid PRIMARY KEY REFERENCES sessions(id),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    payload jsonb NOT NULL DEFAULT '{"goal":"","constraints":[],"decisions":[],"pending":[],"source_message_ids":[]}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO task_states (session_id) SELECT id FROM sessions;
