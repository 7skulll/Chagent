-- Existing paths do not prove which host directory was mounted there. Keep
-- their sessions and memories for audit, but never attach them to a new mount.
ALTER TABLE workspaces ADD COLUMN host_path text;
ALTER TABLE workspaces ADD COLUMN binding_status text NOT NULL DEFAULT 'unbound'
    CHECK (binding_status IN ('bound', 'unbound'));
ALTER TABLE workspaces ADD CONSTRAINT workspaces_bound_has_host_check
    CHECK (binding_status <> 'bound' OR host_path IS NOT NULL);
ALTER TABLE workspaces DROP CONSTRAINT workspaces_root_path_key;
CREATE UNIQUE INDEX workspaces_bound_host_path_idx ON workspaces(host_path)
    WHERE binding_status = 'bound';
CREATE INDEX workspaces_bound_runtime_idx ON workspaces(root_path)
    WHERE binding_status = 'bound';

-- User-scope memories have no workspace_id, so isolating workspaces alone
-- would still inject facts learned under an unverified old mount.
ALTER TABLE memories DROP CONSTRAINT memories_status_check;
ALTER TABLE memories ADD CONSTRAINT memories_status_check
    CHECK (status IN ('active', 'candidate', 'deleted', 'rejected', 'quarantined'));
UPDATE memories SET status = 'quarantined', revision = revision + 1,
    updated_at = now() WHERE status IN ('active', 'candidate');
INSERT INTO memory_revisions (memory_id, revision, action, payload)
SELECT id, revision, 'workspace_identity_quarantine',
       jsonb_build_object('reason', 'source workspace binding cannot be verified')
FROM memories WHERE status = 'quarantined';
