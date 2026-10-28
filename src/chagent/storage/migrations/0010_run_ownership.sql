-- Existing active runs remain unowned; recovery is a separate operation.
ALTER TABLE runs ADD COLUMN owner_token uuid;
ALTER TABLE runs ADD COLUMN lease_expires_at timestamptz;
CREATE INDEX runs_lease_idx ON runs (lease_expires_at) WHERE status = 'running';
