-- Retired transcript records (WK2).
--
-- Regenerating an answer and editing a sent message both keep the original row
-- and add a new one. ``superseded_by`` names the run that replaced it, which is
-- what lets the transcript stay append-only while the model sees a linear story:
-- replay filters on ``superseded_by IS NULL``, the history view does not, so the
-- old answer can still be unfolded and compared.
--
-- Retirement always covers a suffix of the session (everything from the replaced
-- record to the newest), because a message cannot be replaced without the turns
-- that followed it becoming stale. ``edited`` is derived, not stored: a retired
-- user message was edited, a retired assistant message was regenerated.
ALTER TABLE messages ADD COLUMN superseded_by uuid REFERENCES runs(id);

-- The model's replay reads only live rows; the history view reads all of them.
CREATE INDEX messages_live_sequence_idx ON messages (session_id, sequence)
    WHERE superseded_by IS NULL;
