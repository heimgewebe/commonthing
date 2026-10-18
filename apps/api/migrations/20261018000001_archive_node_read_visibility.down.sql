-- No automatic online downgrade: dropping an archived visibility snapshot
-- would re-publish private, hidden and revoked conversation history. Historic
-- unknown archives also lack enough evidence to default to public.
-- Recovery requires an authorized forward fix; reject before mutating state.
DO $$
BEGIN
    RAISE EXCEPTION 'Migration 20261018000001 is not automatically reversible: the archive visibility snapshot is a privacy boundary. Restore by a reviewed roll-forward migration, never drop classification.';
END $$;
