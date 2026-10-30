-- Fair admission before any actual domain generation-row UPDATE.
-- Each domain AFTER ROW outbox trigger updates this singleton only when it
-- enqueues a real change. No-op domain UPDATEs and failed INSERT conflicts
-- do not need a global writer barrier.
-- A BEFORE STATEMENT trigger acquires the exclusive advisory queue lock
-- before PostgreSQL attempts the singleton version-row lock. This prevents
-- new privacy GETs from overtaking a waiting public-to-private writer.
CREATE FUNCTION weltgewebe_domain_writer_fair_generation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('weltgewebe.domain.generation.fair_queue.v1', 0)
    );
    RETURN NULL; -- ignored for a statement-level trigger
END;
$$;

CREATE TRIGGER domain_projection_state_generation_fair_writer
BEFORE UPDATE ON domain_projection_state
FOR EACH STATEMENT EXECUTE FUNCTION weltgewebe_domain_writer_fair_generation();
