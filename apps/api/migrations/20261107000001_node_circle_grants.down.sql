-- Removing access grants is a non-transparent change. Refuse to discard
-- accepted, live owner intentions silently; use a reviewed roll-forward.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM domain_node_circle_grants) THEN
        RAISE EXCEPTION 'Active node-circle grants require an explicit reviewed rollback; refusing silent loss of consent records';
    END IF;
END;
$$;
DROP TRIGGER domain_nodes_clear_circle_grants_on_visibility_change ON domain_nodes;
DROP FUNCTION weltgewebe_clear_node_circle_grants_on_visibility_change();
DROP TRIGGER audience_circle_state_acl_generation ON domain_audience_circles;
DROP TRIGGER circle_members_acl_generation ON domain_audience_circle_members;
DROP TRIGGER node_circle_grants_acl_generation ON domain_node_circle_grants;
DROP FUNCTION weltgewebe_bump_node_circle_acl_generation();
DROP FUNCTION weltgewebe_node_readable_to(TEXT, TEXT, TEXT, TEXT);
DROP TABLE domain_node_circle_grants;
