-- Separate users (#46): each person has their own meals, targets, aliases, meal templates,
-- undo history, dashboard sessions and connector tokens. Foods stay shared.
-- Everything logged so far belongs to user 1, the owner, who signs in with AUTH_PASSWORD.
-- Other users are added with `chomp user add` and have their own (scrypt-hashed) password.
-- Tables that gain user_id are rebuilt (SQLite can't add a NOT NULL column without a
-- default); db.py runs migrations with foreign keys off and checks them before committing.
CREATE TABLE users (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT,                  -- NULL for the owner (id 1): AUTH_PASSWORD signs in
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO users (id, name) VALUES (1, 'owner');

-- The view reads meals, so it goes while meals is rebuilt
DROP VIEW daily_totals;

CREATE TABLE meals_new (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    eaten_at    TEXT NOT NULL,              -- UTC ISO 8601
    label       TEXT,                       -- breakfast / lunch / post-workout...
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO meals_new (id, user_id, eaten_at, label, note, created_at)
    SELECT id, 1, eaten_at, label, note, created_at FROM meals;
DROP TABLE meals;
ALTER TABLE meals_new RENAME TO meals;
CREATE INDEX idx_meals_user_eaten_at ON meals(user_id, eaten_at);

CREATE TABLE aliases_new (
    user_id        INTEGER NOT NULL REFERENCES users(id),
    alias          TEXT NOT NULL COLLATE NOCASE,
    food_id        INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    default_grams  REAL,
    PRIMARY KEY (user_id, alias)
);
INSERT INTO aliases_new (user_id, alias, food_id, default_grams)
    SELECT 1, alias, food_id, default_grams FROM aliases;
DROP TABLE aliases;
ALTER TABLE aliases_new RENAME TO aliases;

CREATE TABLE meal_templates_new (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    name     TEXT NOT NULL COLLATE NOCASE,   -- 'usual breakfast'
    UNIQUE (user_id, name)
);
INSERT INTO meal_templates_new (id, user_id, name) SELECT id, 1, name FROM meal_templates;
DROP TABLE meal_templates;
ALTER TABLE meal_templates_new RENAME TO meal_templates;

CREATE TABLE targets_new (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    kcal        REAL,
    protein_g   REAL,
    carbs_g     REAL,
    fat_g       REAL
);
INSERT INTO targets_new (user_id, kcal, protein_g, carbs_g, fat_g)
    SELECT 1, kcal, protein_g, carbs_g, fat_g FROM targets;
DROP TABLE targets;
ALTER TABLE targets_new RENAME TO targets;

CREATE TABLE oauth_tokens_new (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    grant_id    TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scopes      TEXT NOT NULL,              -- space-separated
    resource    TEXT,                       -- RFC 8707 resource indicator
    expires_at  INTEGER NOT NULL,           -- unix seconds
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO oauth_tokens_new
    (token_hash, kind, grant_id, client_id, user_id, scopes, resource, expires_at, created_at)
    SELECT token_hash, kind, grant_id, client_id, 1, scopes, resource, expires_at, created_at
    FROM oauth_tokens;
DROP TABLE oauth_tokens;
ALTER TABLE oauth_tokens_new RENAME TO oauth_tokens;
CREATE INDEX idx_oauth_tokens_grant ON oauth_tokens(grant_id);

-- web_sessions.subject and undo_actions.subject were always 'owner'
CREATE TABLE web_sessions_new (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  INTEGER NOT NULL,           -- unix seconds
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO web_sessions_new (token_hash, user_id, expires_at, created_at)
    SELECT token_hash, 1, expires_at, created_at FROM web_sessions;
DROP TABLE web_sessions;
ALTER TABLE web_sessions_new RENAME TO web_sessions;

CREATE TABLE undo_actions_new (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary     TEXT NOT NULL,              -- 'Deleted Egg, whole', shown with the Undo button
    before      TEXT NOT NULL,              -- JSON
    after       TEXT NOT NULL,              -- JSON
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL            -- unix seconds
);
INSERT INTO undo_actions_new (id, user_id, summary, before, after, used, created_at)
    SELECT id, 1, summary, before, after, used, created_at FROM undo_actions;
DROP TABLE undo_actions;
ALTER TABLE undo_actions_new RENAME TO undo_actions;

-- Approximate daily totals per user for ad-hoc queries only (see SCHEMA.sql)
CREATE VIEW daily_totals AS
SELECT m.user_id,
       date(m.eaten_at, '-4 hours') AS day,
       ROUND(SUM(e.kcal), 0)      AS kcal,
       ROUND(SUM(e.protein_g), 1) AS protein_g,
       ROUND(SUM(e.carbs_g), 1)   AS carbs_g,
       ROUND(SUM(e.fat_g), 1)     AS fat_g
FROM entries e JOIN meals m ON m.id = e.meal_id
GROUP BY m.user_id, day;
