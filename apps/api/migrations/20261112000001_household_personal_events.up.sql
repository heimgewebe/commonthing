-- P3a: account-owned system conversations reuse the canonical domain_messages
-- and domain.message.created transactional outbox. No synthetic human sender.
-- Existing direct conversations and their Push subscriptions stay unchanged.
-- Removed accounts cannot physically delete their historical message
-- conversation (the canonical conversation history guard forbids this).
-- Sever the live account binding instead, preventing reused account IDs
-- from inheriting an older mailbox. NULL means retired/inaccessible.
ALTER TABLE domain_conversations
    ADD COLUMN account_event_owner_id TEXT REFERENCES domain_accounts(id) ON DELETE SET NULL;

ALTER TABLE domain_conversations
    DROP CONSTRAINT domain_conversations_visibility_check,
    DROP CONSTRAINT domain_conversations_subject_kind;

ALTER TABLE domain_conversations ADD CONSTRAINT domain_conversations_visibility_check CHECK (
    (conversation_type IN ('node','governance_proposal','webgemeindezentrum') AND visibility='public')
    OR (conversation_type IN ('direct','account_event') AND visibility='participants')
);

ALTER TABLE domain_conversations ADD CONSTRAINT domain_conversations_subject_kind CHECK (
    (
        conversation_type='node' AND visibility='public'
        AND proposal_id IS NULL AND webgemeindezentrum_id IS NULL AND direct_pair_key IS NULL
        AND account_event_owner_id IS NULL
        AND (
            (node_id IS NOT NULL AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL)
            OR (node_id IS NULL AND node_id_snapshot IS NOT NULL AND node_title_snapshot IS NOT NULL AND archived_at IS NOT NULL)
        )
    ) OR (
        conversation_type='governance_proposal' AND visibility='public'
        AND node_id IS NULL AND proposal_id IS NOT NULL AND webgemeindezentrum_id IS NULL
        AND direct_pair_key IS NULL AND account_event_owner_id IS NULL
        AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    ) OR (
        conversation_type='webgemeindezentrum' AND visibility='public'
        AND node_id IS NULL AND proposal_id IS NULL AND webgemeindezentrum_id IS NOT NULL
        AND direct_pair_key IS NULL AND account_event_owner_id IS NULL
        AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    ) OR (
        conversation_type='direct' AND visibility='participants'
        AND node_id IS NULL AND proposal_id IS NULL AND webgemeindezentrum_id IS NULL
        AND direct_pair_key IS NOT NULL AND char_length(direct_pair_key)>0
        AND account_event_owner_id IS NULL
        AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    ) OR (
        conversation_type='account_event' AND visibility='participants'
        AND node_id IS NULL AND proposal_id IS NULL AND webgemeindezentrum_id IS NULL
        AND direct_pair_key IS NULL
        -- NULL after account deletion is an intentionally inaccessible
        -- retired mailbox, not permission to rebind it to a new account.
        AND node_id_snapshot IS NULL AND node_title_snapshot IS NULL AND archived_at IS NULL
    )
);

-- Exactly one private system inbox per live account; never a direct 1:1 pair.
CREATE UNIQUE INDEX domain_conversations_account_event_owner
    ON domain_conversations (account_event_owner_id)
    WHERE conversation_type='account_event';

-- A typed, private envelope around canonical domain_messages; the message row
-- remains the only content and outbox truth. This table holds routing/read state.
-- Deleting a circle clears its link, never the recipient's historical mail.
CREATE TABLE domain_personal_event_messages (
    message_id UUID PRIMARY KEY REFERENCES domain_messages(id) ON DELETE CASCADE,
    circle_id UUID REFERENCES domain_audience_circles(id) ON DELETE SET NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'circle_invited', 'circle_invitation_sent', 'circle_joined', 'circle_declined',
        'circle_withdrawn', 'circle_left'
    )),
    subject_account_id TEXT REFERENCES domain_accounts(id) ON DELETE SET NULL,
    -- Bind actionable invitations to the exact membership row's creation
    -- instant. Old mail must stay inert if a person is invited again later.
    invitation_created_at TIMESTAMPTZ,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX domain_personal_event_messages_circle
    ON domain_personal_event_messages(circle_id, created_at DESC);

CREATE FUNCTION weltgewebe_deliver_circle_event(
    recipient_account TEXT, target_circle UUID, event_type TEXT, subject_account TEXT,
    invited_at TIMESTAMPTZ DEFAULT NULL
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    inbox_id UUID;
    item_id UUID;
    notice TEXT;
BEGIN
    IF recipient_account IS NULL OR NOT EXISTS(
        SELECT 1 FROM domain_accounts WHERE id=recipient_account AND disabled=FALSE
    ) THEN
        RETURN;
    END IF;
    CASE event_type
        WHEN 'circle_invited' THEN notice := 'Du hast eine Einladung zu einer Gemeinschaft erhalten.';
        WHEN 'circle_invitation_sent' THEN notice := 'Du hast eine Einladung zu einer Gemeinschaft verschickt.';
        WHEN 'circle_joined' THEN notice := 'Ein Mitglied ist einer Gemeinschaft beigetreten.';
        WHEN 'circle_declined' THEN notice := 'Eine Einladung zu einer Gemeinschaft wurde abgelehnt.';
        WHEN 'circle_withdrawn' THEN notice := 'Eine Einladung zu einer Gemeinschaft wurde zurückgezogen.';
        WHEN 'circle_left' THEN notice := 'Ein Mitglied hat eine Gemeinschaft verlassen.';
        ELSE RAISE EXCEPTION 'unsupported circle event kind: %', event_type;
    END CASE;

    -- Database uniqueness and the surrounding membership transaction prevent
    -- inbox duplication under concurrent writers or a retried HTTP invitation.
    INSERT INTO domain_conversations(
        id, conversation_type, visibility, account_event_owner_id
    ) VALUES (gen_random_uuid(), 'account_event', 'participants', recipient_account)
    ON CONFLICT (account_event_owner_id) WHERE conversation_type='account_event'
    DO UPDATE SET updated_at=domain_conversations.updated_at
    RETURNING id INTO inbox_id;

    item_id := gen_random_uuid();
    INSERT INTO domain_messages(
        id, conversation_id, author_account_id, author_title, content, idempotency_key
    ) VALUES (item_id, inbox_id, NULL, 'commonThing', notice, gen_random_uuid());
    INSERT INTO domain_personal_event_messages(
        message_id, circle_id, event_kind, subject_account_id, invitation_created_at, read_at
    ) VALUES (
        item_id, target_circle, event_type, subject_account, invited_at,
        CASE WHEN event_type='circle_invitation_sent' THEN NOW() ELSE NULL END
    );
END;
$$;

-- PostgreSQL lifecycle transitions, not HTTP 2xx responses, produce messages.
-- They are committed with the state change and the existing message outbox.
CREATE FUNCTION weltgewebe_circle_membership_personal_event()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    recipient TEXT;
    withdrawn BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status='invited' THEN
            PERFORM weltgewebe_deliver_circle_event(
                NEW.account_id, NEW.circle_id, 'circle_invited', NEW.invited_by_account_id, NEW.created_at
            );
            -- Keep a read status in the sender's own existing inbox so they
            -- can withdraw this exact pending invitation without a new panel.
            PERFORM weltgewebe_deliver_circle_event(
                NEW.invited_by_account_id, NEW.circle_id,
                'circle_invitation_sent', NEW.account_id, NEW.created_at
            );
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.status='invited' AND NEW.status='active' THEN
            FOR recipient IN
                SELECT DISTINCT account_id
                FROM domain_audience_circle_members
                WHERE circle_id=NEW.circle_id AND status='active'
                  AND account_id<>NEW.account_id
            LOOP
                PERFORM weltgewebe_deliver_circle_event(
                    recipient, NEW.circle_id, 'circle_joined', NEW.account_id
                );
            END LOOP;
        END IF;
        RETURN NEW;
    END IF;

    -- Circle or account deletion can cascade membership rows; do not create
    -- notifications for a circle that is disappearing.
    IF NOT EXISTS (SELECT 1 FROM domain_audience_circles WHERE id=OLD.circle_id) THEN
        RETURN OLD;
    END IF;
    IF OLD.status='invited' THEN
        withdrawn := current_setting('weltgewebe.circle_invitation_withdrawal', true)='yes';
        IF withdrawn THEN
            PERFORM weltgewebe_deliver_circle_event(
                OLD.account_id, OLD.circle_id, 'circle_withdrawn', OLD.invited_by_account_id
            );
        ELSE
            PERFORM weltgewebe_deliver_circle_event(
                OLD.invited_by_account_id, OLD.circle_id, 'circle_declined', OLD.account_id
            );
        END IF;
    ELSIF OLD.status='active' THEN
        FOR recipient IN
            SELECT DISTINCT account_id
            FROM domain_audience_circle_members
            WHERE circle_id=OLD.circle_id AND status='active'
              AND account_id<>OLD.account_id
        LOOP
            PERFORM weltgewebe_deliver_circle_event(
                recipient, OLD.circle_id, 'circle_left', OLD.account_id
            );
        END LOOP;
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER audience_circle_personal_events
AFTER INSERT OR UPDATE OF status OR DELETE ON domain_audience_circle_members
FOR EACH ROW EXECUTE FUNCTION weltgewebe_circle_membership_personal_event();

-- One-time reconciliation for pending invitations created before P3a. These
-- people should see their open invitation in /nachrichten after rollout.
-- The migration and its canonical message/outbox inserts commit atomically.
DO $$
DECLARE
    prior_invitation RECORD;
BEGIN
    FOR prior_invitation IN
        SELECT m.circle_id, m.account_id, m.invited_by_account_id, m.created_at
        FROM domain_audience_circle_members m
        JOIN domain_audience_circles c ON c.id=m.circle_id
        WHERE m.status='invited' AND c.state<>'inactive'
    LOOP
        PERFORM weltgewebe_deliver_circle_event(
            prior_invitation.account_id, prior_invitation.circle_id,
            'circle_invited', prior_invitation.invited_by_account_id,
            prior_invitation.created_at
        );
        PERFORM weltgewebe_deliver_circle_event(
            prior_invitation.invited_by_account_id, prior_invitation.circle_id,
            'circle_invitation_sent', prior_invitation.account_id,
            prior_invitation.created_at
        );
    END LOOP;
END;
$$;
