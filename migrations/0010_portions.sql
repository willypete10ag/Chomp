-- Portions (#75): each user's named amounts of a food ("handful" of Cheez-Its = 30 g), learned
-- from what they tell the agent, replace nicknames (aliases). Entries whose grams were
-- estimated from a count rather than weighed or taken from a known portion are flagged.

CREATE TABLE portions (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    food_id     INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    name        TEXT NOT NULL COLLATE NOCASE,   -- 'handful', 'Birria taco'
    grams       REAL NOT NULL CHECK (grams > 0),  -- one portion
    estimated   INTEGER NOT NULL DEFAULT 0,     -- 1: the agent's estimate, not yet weighed
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, food_id, name)
);

-- Nicknames with a usual amount become portions, without a leading "1 " ("1 Birria taco"
-- -> "Birria taco"). Ones without grams only pointed a phrase at a food and go with the table.
INSERT INTO portions (user_id, food_id, name, grams)
    SELECT user_id, food_id,
           CASE WHEN alias LIKE '1 %' THEN trim(substr(alias, 3)) ELSE trim(alias) END,
           default_grams
    FROM aliases
    WHERE default_grams > 0
    ON CONFLICT (user_id, food_id, name) DO NOTHING;
DROP TABLE aliases;

ALTER TABLE entries ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0;
-- A saved meal keeps an item's estimate, so logging it again marks that entry too
ALTER TABLE meal_template_items ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0;

-- Undo records hold whole entry rows; ones saved before this column existed would never
-- match the current rows again, so they go (they only last a day anyway).
DELETE FROM undo_actions;
