-- OAuth tokens issued to Claude's connector (#24), so a restart doesn't sign the connector out.
-- Only SHA-256 hashes are stored; the raw tokens exist only in Claude's connector.
-- A grant groups the access and refresh tokens from one sign-in; refreshing replaces the
-- whole grant's tokens, and revoking deletes the grant.
CREATE TABLE oauth_tokens (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    grant_id    TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    scopes      TEXT NOT NULL,              -- space-separated
    resource    TEXT,                       -- RFC 8707 resource indicator
    expires_at  INTEGER NOT NULL,           -- unix seconds
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_oauth_tokens_grant ON oauth_tokens(grant_id);
