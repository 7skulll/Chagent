CREATE TABLE memories (
    id uuid PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('user', 'workspace')),
    workspace_id uuid REFERENCES workspaces(id),
    key text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'candidate', 'deleted', 'rejected')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    pinned boolean NOT NULL DEFAULT false,
    origin text NOT NULL CHECK (origin IN ('manual', 'explicit_user', 'inferred')),
    source_session_id uuid REFERENCES sessions(id),
    source_run_id uuid,
    source_message_ids jsonb NOT NULL DEFAULT '[]',
    evidence text NOT NULL DEFAULT '',
    conflict_with uuid REFERENCES memories(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope = 'user' AND workspace_id IS NULL) OR (scope = 'workspace' AND workspace_id IS NOT NULL)),
    CHECK (source_run_id IS NULL OR source_session_id IS NOT NULL),
    FOREIGN KEY (source_session_id, source_run_id) REFERENCES runs(session_id, id)
);
CREATE UNIQUE INDEX memories_active_key_idx ON memories(scope, COALESCE(workspace_id, '00000000-0000-0000-0000-000000000000'::uuid), key) WHERE status = 'active';
CREATE INDEX memories_scope_idx ON memories(workspace_id, status, created_at);

CREATE TABLE memory_revisions (
    memory_id uuid NOT NULL REFERENCES memories(id),
    revision integer NOT NULL,
    action text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, revision)
);

CREATE TABLE memory_extractions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES runs(id),
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
    prompt_version text NOT NULL,
    request jsonb NOT NULL,
    response jsonb,
    error text,
    duration_ms bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
