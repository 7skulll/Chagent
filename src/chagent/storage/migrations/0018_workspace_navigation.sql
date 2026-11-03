ALTER TABLE workspaces ADD COLUMN last_opened_at timestamptz NOT NULL DEFAULT now();
