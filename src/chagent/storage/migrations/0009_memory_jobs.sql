-- Detach memory extraction from the run's critical path: the completion
-- transaction only enqueues a job, and a bounded worker performs the model call.
CREATE TABLE memory_jobs (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE,
    session_id uuid NOT NULL,
    source_message_id uuid NOT NULL,
    prompt_version text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    ended_at timestamptz,
    FOREIGN KEY (session_id, run_id) REFERENCES runs(session_id, id),
    FOREIGN KEY (source_message_id) REFERENCES messages(id),
    CHECK (status <> 'running' OR lease_expires_at IS NOT NULL)
);

CREATE INDEX memory_jobs_pending_idx ON memory_jobs(available_at, created_at) WHERE status = 'queued';
CREATE INDEX memory_jobs_lease_idx ON memory_jobs(lease_expires_at) WHERE status = 'running';
