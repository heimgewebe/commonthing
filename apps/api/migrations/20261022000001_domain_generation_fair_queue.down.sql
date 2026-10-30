-- Remove only this migration's fair writer admission point.
-- Existing AFTER ROW outbox triggers and generation-row updates remain intact.
DROP TRIGGER IF EXISTS domain_projection_state_generation_fair_writer
ON domain_projection_state;

DROP FUNCTION IF EXISTS weltgewebe_domain_writer_fair_generation();
