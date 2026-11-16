-- Each account already owns a separate schema; no client-supplied owner id.
CREATE TABLE user_skills (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL,
    content text NOT NULL,
    files jsonb NOT NULL,
    sha256 text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);
