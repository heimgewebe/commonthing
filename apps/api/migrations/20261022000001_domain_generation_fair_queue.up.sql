-- Public->private visibility writes must not starve behind newly admitted
-- FOR SHARE readers. PostgreSQL row FOR SHARE allows a later compatible
-- reader to pass a waiting UPDATE; an advisory rw-lock uses the fair
-- heavyweight waiter queue instead. A BEFORE ROW gate runs for each affected
-- row, including no-op updates, but not for updates matching zero rows. Readers acquire the
-- SAME key in shared mode through their response-lifetime generation fence.
-- The original singleton FOR SHARE check remains necessary as an invariant.
CREATE FUNCTION weltgewebe_domain_writer_fair_generation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('weltgewebe.domain.generation.fair_queue.v1', 0)
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER domain_nodes_generation_fair_writer
BEFORE INSERT OR UPDATE OR DELETE ON domain_nodes
FOR EACH ROW EXECUTE FUNCTION weltgewebe_domain_writer_fair_generation();

CREATE TRIGGER domain_edges_generation_fair_writer
BEFORE INSERT OR UPDATE OR DELETE ON domain_edges
FOR EACH ROW EXECUTE FUNCTION weltgewebe_domain_writer_fair_generation();

CREATE TRIGGER domain_accounts_generation_fair_writer
BEFORE INSERT OR UPDATE OR DELETE ON domain_accounts
FOR EACH ROW EXECUTE FUNCTION weltgewebe_domain_writer_fair_generation();