-- An old container path is only a runtime mount point. It cannot prove which
-- host directory held the session when it was created.
INSERT INTO workspace_bindings (workspace_id, host_path, device, inode, state)
SELECT id, root_path, 0, 0, 'rebind_required' FROM workspaces
WHERE root_path = '/workspaces' OR root_path LIKE '/workspaces/%';
