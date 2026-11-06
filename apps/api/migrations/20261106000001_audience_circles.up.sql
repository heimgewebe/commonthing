-- Universal, consent-based audience circles. No map coordinates or account roles
-- participate in membership, and no existing node gains visibility by this migration.
CREATE TABLE domain_audience_circles (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('household', 'community')),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    state TEXT NOT NULL DEFAULT 'forming'
        CHECK (state IN ('forming', 'active', 'inactive')),
    created_by_account_id TEXT REFERENCES domain_accounts (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE domain_audience_circle_members (
    circle_id UUID NOT NULL
        REFERENCES domain_audience_circles (id) ON DELETE CASCADE,
    account_id TEXT NOT NULL
        REFERENCES domain_accounts (id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('invited', 'active')),
    invited_by_account_id TEXT
        REFERENCES domain_accounts (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    joined_at TIMESTAMPTZ,
    PRIMARY KEY (circle_id, account_id),
    CHECK ((status = 'active') = (joined_at IS NOT NULL))
);

CREATE INDEX domain_audience_circle_members_by_account
    ON domain_audience_circle_members (account_id, status, circle_id);

-- Canonical membership predicate used by future content-read policies.
-- An invitation, same geographic position, or unconfirmed founding circle
-- never grants content access. Caller must still enforce node visibility.
CREATE FUNCTION weltgewebe_active_circle_member(
    circle_uuid UUID, reader_account_id TEXT
) RETURNS BOOLEAN
LANGUAGE SQL STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM domain_audience_circle_members m
        JOIN domain_audience_circles c ON c.id = m.circle_id
        JOIN domain_accounts a ON a.id = m.account_id
        WHERE m.circle_id = circle_uuid
          AND a.disabled = FALSE
          AND m.account_id = reader_account_id
          AND m.status = 'active'
          AND c.state = 'active'
    );
$$;
