CREATE TABLE workspace_containers (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    container_name text NOT NULL,
    image_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('ready','running','idle','reaped','stale_image')),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX workspace_containers_last_used_idx ON workspace_containers (last_used_at);
