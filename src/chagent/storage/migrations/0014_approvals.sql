-- Human approval for host commands (K batch, first slice).
--
-- A row is one question about one exact invocation: run, tool record, argument
-- fingerprint, directory. The partial unique index makes "one pending request
-- per tool attempt" a database rule instead of a convention, so a retry of the
-- same call cannot silently open a second question.
--
-- RunStatus is deliberately unchanged: a waiting run stays ``running`` and the
-- API projects ``waiting_for=approval`` from this table. Adding a new terminal
-- state for a wait would touch the status CHECK, the active-session index and
-- every cancel path for no gain.
CREATE TABLE approval_requests (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    tool_record_id uuid NOT NULL REFERENCES tool_calls(id),
    fingerprint text NOT NULL,
    tool_name text NOT NULL,
    cwd text NOT NULL,
    subject text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')),
    note text,
    owner_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    decided_at timestamptz,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX approval_requests_one_pending
    ON approval_requests (tool_record_id) WHERE status = 'pending';
CREATE INDEX approval_requests_run_idx ON approval_requests (run_id, created_at);
CREATE INDEX approval_requests_pending_idx ON approval_requests (status, expires_at);
