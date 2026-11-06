-- Host paths are authoritative; old /workspaces rows remain unbound until
-- their original host mount can be proved and explicitly rebound.
CREATE TABLE workspace_bindings (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    host_path text NOT NULL UNIQUE,
    device bigint NOT NULL,
    inode bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'rebind_required')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE native_executions (
    approval_id uuid PRIMARY KEY REFERENCES approval_requests(id),
    run_id uuid NOT NULL REFERENCES runs(id),
    command text NOT NULL,
    cwd text NOT NULL,
    status text NOT NULL CHECK (status IN ('starting', 'completed', 'failed', 'timed_out', 'unknown')),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    exit_code integer
);

CREATE TABLE workspace_services (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    container_name text NOT NULL,
    command text NOT NULL,
    internal_port integer NOT NULL CHECK (internal_port BETWEEN 1 AND 65535),
    host_port integer NOT NULL CHECK (host_port BETWEEN 1 AND 65535),
    status text NOT NULL CHECK (status IN ('starting', 'running', 'stopped', 'failed', 'unknown')),
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
