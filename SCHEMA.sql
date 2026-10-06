-- Chomp schema (SQLite)
PRAGMA foreign_keys = ON;

CREATE TABLE foods (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    brand         TEXT,
    gtin          TEXT UNIQUE,                 -- normalized 14-digit GTIN, NULL for generic foods
    source        TEXT NOT NULL CHECK (source IN ('fdc','off','label','manual')),
    source_id     TEXT,                        -- FDC fdcId / OFF code
    verified      INTEGER NOT NULL DEFAULT 0,  -- 1 once confirmed by me
    -- macros per 100 g
    kcal          REAL NOT NULL,
    protein_g     REAL NOT NULL,
    carbs_g       REAL NOT NULL,
    fat_g         REAL NOT NULL,
    fiber_g       REAL,
    sugar_g       REAL,
    sodium_mg     REAL,
    serving_g     REAL,                        -- label serving size, for display
    serving_desc  TEXT,                        -- e.g. '3 tbsp (46 g)'
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_foods_name ON foods(name);

CREATE TABLE aliases (
    alias          TEXT PRIMARY KEY COLLATE NOCASE,  -- 'my egg whites'
    food_id        INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    default_grams  REAL
);

CREATE TABLE meals (
    id          INTEGER PRIMARY KEY,
    eaten_at    TEXT NOT NULL,              -- UTC ISO 8601
    label       TEXT,                       -- breakfast / lunch / post-workout...
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_meals_eaten_at ON meals(eaten_at);

CREATE TABLE entries (
    id         INTEGER PRIMARY KEY,
    meal_id    INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    food_id    INTEGER NOT NULL REFERENCES foods(id),
    grams      REAL NOT NULL CHECK (grams > 0),
    -- snapshot at log time (food edits never rewrite history)
    kcal       REAL NOT NULL,
    protein_g  REAL NOT NULL,
    carbs_g    REAL NOT NULL,
    fat_g      REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_entries_meal ON entries(meal_id);

CREATE TABLE meal_templates (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE    -- 'usual breakfast'
);

CREATE TABLE meal_template_items (
    template_id  INTEGER NOT NULL REFERENCES meal_templates(id) ON DELETE CASCADE,
    food_id      INTEGER NOT NULL REFERENCES foods(id),
    grams        REAL NOT NULL,
    PRIMARY KEY (template_id, food_id)
);

CREATE TABLE targets (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    kcal        REAL,
    protein_g   REAL,
    carbs_g     REAL,
    fat_g       REAL
);

-- Daily totals in local time
CREATE VIEW daily_totals AS
SELECT date(m.eaten_at, '-4 hours') AS day,   -- adjust for DST in app code; view is a convenience
       ROUND(SUM(e.kcal), 0)      AS kcal,
       ROUND(SUM(e.protein_g), 1) AS protein_g,
       ROUND(SUM(e.carbs_g), 1)   AS carbs_g,
       ROUND(SUM(e.fat_g), 1)     AS fat_g
FROM entries e JOIN meals m ON m.id = e.meal_id
GROUP BY day;
