-- Persist privacy classification when a node with conversation messages is
-- deleted. An ordinary PUBLIC conversation is not proof that its deleted node
-- was public. Historical archives have no reconstructable source node;
-- their NULL classification stays fail-closed until an audited migration.
ALTER TABLE domain_conversations
    ADD COLUMN archived_node_visibility TEXT;
ALTER TABLE domain_conversations
    ADD CONSTRAINT domain_conversations_archived_node_visibility_valid
    CHECK (archived_node_visibility IS NULL OR
           archived_node_visibility IN ('public', 'private', 'hidden', 'revoked'));

-- Keep the existing trigger and deletion/archival transaction. Only record
-- the server-owned node visibility at the point of canonical deletion.
CREATE OR REPLACE FUNCTION weltgewebe_protect_node_conversation_history()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    conversation_identifier UUID;
    archive_timestamp TIMESTAMPTZ;
BEGIN
    SELECT id INTO conversation_identifier
    FROM domain_conversations
    WHERE node_id = OLD.id
    FOR UPDATE;

    IF conversation_identifier IS NOT NULL AND EXISTS (
        SELECT 1 FROM domain_messages
        WHERE conversation_id = conversation_identifier
    ) THEN
        archive_timestamp := clock_timestamp();
        UPDATE domain_conversations
        SET node_id = NULL,
            node_id_snapshot = OLD.id,
            node_title_snapshot = OLD.title,
            archived_node_visibility = OLD.search_visibility,
            archived_at = archive_timestamp,
            updated_at = GREATEST(updated_at, archive_timestamp)
        WHERE id = conversation_identifier;
    END IF;

    RETURN OLD;
END;
$$;
