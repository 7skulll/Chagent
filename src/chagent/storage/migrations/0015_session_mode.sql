-- Per-session mode (WK0).
--
-- ``work`` is the only mode this build enables: it reads the workspace and the
-- web, and has no write or host-execution tool at all. ``code`` is stored so a
-- build with CHAGENT_CODE_MODE=on can use it, but the row alone grants nothing:
-- the tool face a run gets is resolved from the session row *and* the build
-- switch, and a code session in a work-only build is refused at POST /runs.
--
-- Existing rows become ``work`` through the DEFAULT, which is the conservative
-- direction: a session created by an older build had no declared mode, so it
-- must not silently gain the write face.
ALTER TABLE sessions ADD COLUMN mode text NOT NULL DEFAULT 'work'
    CHECK (mode IN ('work', 'code'));
