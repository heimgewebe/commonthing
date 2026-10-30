-- New published nodes have one canonical semantic kind.
-- NOT VALID leaves existing production rows untouched until a separately
-- verified inventory and cleanup; PostgreSQL enforces this for new/updated rows.
ALTER TABLE domain_nodes
    ALTER COLUMN kind DROP DEFAULT;

ALTER TABLE domain_nodes
    ADD CONSTRAINT domain_nodes_kind_v2_check
    CHECK (kind IN ('place', 'resource', 'project', 'event', 'need', 'offer'))
    NOT VALID;
