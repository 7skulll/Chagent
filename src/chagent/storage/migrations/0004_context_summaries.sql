CREATE TABLE session_summaries (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id),
    revision integer NOT NULL CHECK (revision > 0),
    previous_summary_id uuid,
    covered_through_sequence bigint NOT NULL,
    first_kept_message_id uuid NOT NULL REFERENCES messages(id),
    content text NOT NULL CHECK (length(trim(content)) > 0),
    model text NOT NULL,
    provider text NOT NULL,
    prompt_version text NOT NULL,
    compaction_id uuid NOT NULL UNIQUE,
    source_message_ids jsonb NOT NULL,
    usage jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, revision),
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id, previous_summary_id) REFERENCES session_summaries(session_id, id)
);

-- Independent of task model_calls: summaries never enter the chat transcript.
CREATE TABLE compaction_calls (
    id uuid PRIMARY KEY,
    compaction_id uuid NOT NULL,
    run_id uuid NOT NULL REFERENCES runs(id),
    iteration integer NOT NULL CHECK (iteration >= 0),
    purpose text NOT NULL DEFAULT 'summary' CHECK (purpose = 'summary'),
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
    request jsonb NOT NULL,
    response jsonb,
    error text,
    duration_ms bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    UNIQUE (compaction_id, iteration)
);
CREATE INDEX compaction_calls_run_idx ON compaction_calls(run_id);
