-- Location is an optional node projection. Existing coordinate columns already
-- allow NULL; enforce complete, valid pairs for future writes without rewriting
-- or inventing positions for legacy rows. NOT VALID leaves legacy data intact.
ALTER TABLE domain_nodes ADD CONSTRAINT domain_nodes_location_pair CHECK (
    (lat IS NULL AND lon IS NULL)
    OR (lat IS NOT NULL AND lon IS NOT NULL
        AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180)
) NOT VALID;
