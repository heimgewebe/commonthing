-- Remove only this migration's generation writer admission triggers.
-- The original AFTER ROW outbox triggers/version updates stay untouched.
DROP TRIGGER IF EXISTS domain_accounts_generation_fair_writer ON domain_accounts;
DROP TRIGGER IF EXISTS domain_edges_generation_fair_writer ON domain_edges;
DROP TRIGGER IF EXISTS domain_nodes_generation_fair_writer ON domain_nodes;

DROP FUNCTION IF EXISTS weltgewebe_domain_writer_fair_generation();
