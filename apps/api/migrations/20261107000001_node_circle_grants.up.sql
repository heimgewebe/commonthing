-- A node remains the one canonical resource. Private owner nodes may be
-- explicitly granted to confirmed audience circles; no location-based access.
-- Existing nodes receive zero grants and retain their previous visibility.
CREATE TABLE domain_node_circle_grants (
    node_id TEXT NOT NULL REFERENCES domain_nodes(id) ON DELETE CASCADE,
    circle_id UUID NOT NULL REFERENCES domain_audience_circles(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (node_id, circle_id)
);

CREATE INDEX domain_node_circle_grants_by_circle
    ON domain_node_circle_grants (circle_id, node_id);

-- The same policy is applied BEFORE pagination, ranking, counts and response
-- assembly. Hidden is deliberately owner-only, revoked is never readable.
-- Only explicitly private nodes with a grant may be read by active members.
CREATE FUNCTION weltgewebe_node_readable_to(
    node_identifier TEXT, visibility TEXT, owner_account_id TEXT,
    reader_account_id TEXT
) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT COALESCE(
        visibility = 'public'
        OR (
            reader_account_id IS NOT NULL
            AND (
                (visibility IN ('private', 'hidden')
                 AND owner_account_id = reader_account_id)
                OR (
                    visibility = 'private'
                    AND owner_account_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM domain_accounts owner
                        WHERE owner.id = owner_account_id
                          AND owner.disabled = FALSE
                    )
                    AND EXISTS (
                        SELECT 1 FROM domain_node_circle_grants grant_record
                        WHERE grant_record.node_id = node_identifier
                          AND weltgewebe_active_circle_member(
                              grant_record.circle_id, reader_account_id
                          )
                    )
                )
            )
        ), FALSE
    );
$$;

-- The existing PostgreSQL domain-generation fence is held through the entire
-- response on node/edge/account/conversation GETs. Membership/grant mutations
-- must invalidate this fence as well, so a departure revokes cached reads.
-- Do not write a fictitious node-update history or a second node projection.
CREATE FUNCTION weltgewebe_bump_node_circle_acl_generation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN
        RETURN NEW;
    END IF;
    UPDATE domain_projection_state SET version = version + 1
      WHERE singleton = TRUE;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER node_circle_grants_acl_generation
AFTER INSERT OR DELETE ON domain_node_circle_grants
FOR EACH ROW EXECUTE FUNCTION weltgewebe_bump_node_circle_acl_generation();

CREATE TRIGGER circle_members_acl_generation
AFTER INSERT OR DELETE OR UPDATE OF status ON domain_audience_circle_members
FOR EACH ROW EXECUTE FUNCTION weltgewebe_bump_node_circle_acl_generation();

CREATE TRIGGER audience_circle_state_acl_generation
AFTER UPDATE OF state ON domain_audience_circles
FOR EACH ROW EXECUTE FUNCTION weltgewebe_bump_node_circle_acl_generation();

-- Reusing the existing node PATCH/PUT to leave private mode cannot silently
-- reactivate previously authorized grants on a subsequent private transition.
CREATE FUNCTION weltgewebe_clear_node_circle_grants_on_visibility_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.search_visibility IS DISTINCT FROM OLD.search_visibility
       OR (OLD.payload -> 'created_by_account_id')
             IS DISTINCT FROM (NEW.payload -> 'created_by_account_id')
    THEN
        DELETE FROM domain_node_circle_grants WHERE node_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER domain_nodes_clear_circle_grants_on_visibility_change
AFTER UPDATE OF search_visibility, payload ON domain_nodes
FOR EACH ROW EXECUTE FUNCTION weltgewebe_clear_node_circle_grants_on_visibility_change();
