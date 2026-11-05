-- One page decision for several writes in the same model turn (batch 1A).
--
-- A batch is not a second kind of question. Every write still has its own row:
-- its own tool record, its own fingerprint, its own single-use consumption.
-- What the batch adds is the decision: the page answers once, and this column is
-- how the answer is applied atomically to the rows that were shown together.
--
-- batch_id is NULL for every single question (host commands, and any write that
-- happens to be alone), so nothing about the existing path changes.
ALTER TABLE approval_requests ADD COLUMN batch_id uuid;
ALTER TABLE approval_requests ADD COLUMN batch_index smallint;

CREATE INDEX approval_requests_batch_idx
    ON approval_requests (batch_id) WHERE batch_id IS NOT NULL;

-- The page renders one card per batch, in the order the model asked for.
CREATE UNIQUE INDEX approval_requests_batch_position
    ON approval_requests (batch_id, batch_index) WHERE batch_id IS NOT NULL;
