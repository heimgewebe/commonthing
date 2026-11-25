-- A fresh reservation token fences completion across lease expiry and requeue.
-- Existing rows remain valid; the next new-worker claim assigns a token.
ALTER TABLE domain_outbox ADD COLUMN claim_token UUID;
