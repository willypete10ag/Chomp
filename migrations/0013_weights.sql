-- Health data (#94): body weight per user, and the per-user tokens that let a phone post
-- Apple Health readings to /ingest/health (decided on #101).

-- One weigh-in. Stored in kg; tools and pages convert. A phone resending the same reading
-- (same person and second) updates it instead of adding a duplicate.
CREATE TABLE weights (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    measured_at  TEXT NOT NULL,                               -- UTC ISO 8601, second precision
    kg           REAL NOT NULL CHECK (kg > 0 AND kg < 700),
    source       TEXT NOT NULL CHECK (source IN ('health', 'chat', 'app')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, measured_at)
);

-- A person's phone token for /ingest/health: SHA-256 of the token, one live token each.
-- It can only add that person's health readings; it is refused everywhere else.
CREATE TABLE ingest_tokens (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at  TEXT
);
