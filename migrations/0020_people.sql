-- Adding people from the dashboard (#109).
-- An invite is a one-time link the owner gives someone: opening it lets them pick their own
-- password and signs them in. Only a hash of the link's token is kept. A new invite for the
-- same person replaces the old one; a password reset is a new invite.
CREATE TABLE user_invites (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  INTEGER NOT NULL,              -- unix seconds
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Each person's own Hevy API key (Hevy Pro), pasted on Integrations. The key is sent to Hevy
-- on every sync, so it is kept as given, like HEVY_API_KEY in .env (which stays the owner's,
-- or HEVY_USER's, when that person has none here).
CREATE TABLE hevy_keys (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    api_key     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Server-wide settings the owner changes on the dashboard: `health_shortcut_url` is the
-- iCloud link of the shared Send to Chomp Shortcut that Connect iPhone offers.
CREATE TABLE app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
