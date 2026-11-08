-- The site's start directory must outlive the state file inside the container.
-- Without it a restart fell back to "." (the workspace root) and `python3
-- server.py` failed with exit code 2; measured on a real workspace where the
-- project lives in a subdirectory.
ALTER TABLE workspace_services ADD COLUMN workdir text NOT NULL DEFAULT '.';
