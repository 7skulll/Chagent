-- Keep uncertain container cleanup blocked across controller restarts.
ALTER TABLE workspace_containers ADD COLUMN cleanup_pending boolean NOT NULL DEFAULT false;

CREATE TABLE container_invocations (
    invocation uuid PRIMARY KEY,
    container_name text NOT NULL,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    control_path text NOT NULL,
    environment jsonb NOT NULL,
    cleanup_confirmed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX container_invocations_unfinished ON container_invocations(container_name,run_id)
    WHERE NOT cleanup_confirmed;
