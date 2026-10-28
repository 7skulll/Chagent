-- A claim token binds a 'running' job to the attempt that took it. A lease only
-- says when the attempt may be reaped; it cannot tell a late worker that its
-- lease was already recycled and that a newer attempt now owns the row.
ALTER TABLE memory_jobs ADD COLUMN claim_token uuid;

-- NOT VALID: a job that is already running when this migration applies keeps
-- its lease and is reaped normally; every later write must carry a token.
ALTER TABLE memory_jobs ADD CONSTRAINT memory_jobs_running_claim_check
    CHECK (status <> 'running' OR (lease_expires_at IS NOT NULL AND claim_token IS NOT NULL)) NOT VALID;
