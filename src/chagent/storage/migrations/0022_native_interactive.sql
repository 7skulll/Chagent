-- Interactive native host processes: a launch approval starts a long-lived
-- child whose stdin stays open; every later write needs its own page approval
-- and is audited beside the execution it fed. 'running' rows are alive in the
-- controller's process manager; a restart cannot prove their fate, so recovery
-- marks them 'unknown' and never retries them.
ALTER TABLE native_executions DROP CONSTRAINT native_executions_status_check;
ALTER TABLE native_executions ADD CONSTRAINT native_executions_status_check
    CHECK (status IN ('starting', 'running', 'completed', 'failed', 'timed_out', 'stopped', 'unknown'));
ALTER TABLE native_executions ADD COLUMN interactive boolean NOT NULL DEFAULT false;
ALTER TABLE native_executions ADD COLUMN process_id text;
ALTER TABLE native_executions ADD COLUMN child_pid integer;

CREATE TABLE native_stdin_writes (
    approval_id uuid PRIMARY KEY REFERENCES approval_requests(id),
    execution_id uuid NOT NULL REFERENCES native_executions(approval_id),
    run_id uuid NOT NULL REFERENCES runs(id),
    process_id text NOT NULL,
    chars text NOT NULL,
    close_stdin boolean NOT NULL DEFAULT false,
    -- 'pending' claims the one-time grant before a byte moves; 'written' and
    -- 'failed' are settled outcomes, 'unknown' is a restart's honest answer.
    status text NOT NULL CHECK (status IN ('pending', 'written', 'failed', 'unknown')),
    created_at timestamptz NOT NULL DEFAULT now()
);
