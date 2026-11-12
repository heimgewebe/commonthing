-- Never silently discard delivered private messages to roll back P3a.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM domain_conversations WHERE conversation_type='account_event') THEN
        RAISE EXCEPTION USING ERRCODE='23503',
            MESSAGE='cannot roll back personal event conversations with persisted account mail';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS circle_members_personal_events ON domain_audience_circle_members;
DROP FUNCTION IF EXISTS weltgewebe_circle_membership_personal_event();
DROP FUNCTION IF EXISTS weltgewebe_deliver_circle_event(TEXT, UUID, TEXT, TEXT, TIMESTAMPTZ);
DROP TABLE domain_personal_event_messages;
DROP INDEX domain_conversations_account_event_owner;

ALTER TABLE domain_conversations
    DROP CONSTRAINT domain_conversations_visibility_check,
    DROP CONSTRAINT domain_conversations_subject_kind,
    DROP COLUMN account_event_owner_id;

-- Restore the precise pre-P3a subject / visibility contract.
ALTER TABLE domain_conversations ADD CONSTRAINT domain_conversations_visibility_check CHECK (
    (conversation_type IN ('node','governance_proposal','webgemeindezentrum') AND visibility='public')
    OR (conversation_type='direct' AND visibility='participants')
);
ALTER TABLE domain_conversations ADD CONSTRAINT domain_conversations_subject_kind CHECK (
    (
        conversation_type='node' AND visibility='public'
        AND proposal_id IS NULL AND webgemeindezentrum_id IS NULL AND direct_pair_key IS NULL
        AND (
            (node_id IS NOT NULL AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL)
            OR (node_id IS NULL AND node_id_snapshot IS NOT NULL AND node_title_snapshot IS NOT NULL AND archived_at IS NOT NULL)
        )
    ) OR (
        conversation_type='governance_proposal' AND visibility='public'
        AND node_id IS NULL AND proposal_id IS NOT NULL AND webgemeindezentrum_id IS NULL
        AND direct_pair_key IS NULL AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    ) OR (
        conversation_type='webgemeindezentrum' AND visibility='public'
        AND node_id IS NULL AND proposal_id IS NULL AND webgemeindezentrum_id IS NOT NULL
        AND direct_pair_key IS NULL AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    ) OR (
        conversation_type='direct' AND visibility='participants'
        AND node_id IS NULL AND proposal_id IS NULL AND webgemeindezentrum_id IS NULL
        AND direct_pair_key IS NOT NULL AND char_length(direct_pair_key)>0
        AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    )
);
