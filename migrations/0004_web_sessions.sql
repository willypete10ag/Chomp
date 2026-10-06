-- Browser sign-ins for the web dashboard at /app. Only SHA-256 hashes of the session cookie
-- are stored. subject is always 'owner' for now; it names the signed-in user once Chomp has
-- more than one.
CREATE TABLE web_sessions (
    token_hash  TEXT PRIMARY KEY,
    subject     TEXT NOT NULL DEFAULT 'owner',
    expires_at  INTEGER NOT NULL,           -- unix seconds
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
