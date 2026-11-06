-- Derived, source-linked task history. It is historical evidence, never an
-- active memory or an instruction. Superseded answers are hidden at read time.
CREATE TABLE task_recaps (
    run_id uuid PRIMARY KEY REFERENCES runs(id),
    session_id uuid NOT NULL REFERENCES sessions(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    user_message_id uuid NOT NULL REFERENCES messages(id),
    answer_message_id uuid NOT NULL REFERENCES messages(id),
    request_excerpt text NOT NULL,
    answer_excerpt text NOT NULL,
    task_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, id)
);
CREATE INDEX task_recaps_workspace_recent_idx ON task_recaps(workspace_id, created_at DESC, run_id DESC);
