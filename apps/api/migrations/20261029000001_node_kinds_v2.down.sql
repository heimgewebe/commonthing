ALTER TABLE domain_nodes
    DROP CONSTRAINT IF EXISTS domain_nodes_kind_v2_check;

-- Rollback to the historical schema only. Runtime v2 never uses this default.
ALTER TABLE domain_nodes
    ALTER COLUMN kind SET DEFAULT 'Unknown';
