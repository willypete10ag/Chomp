-- Recipes (#82): each user's homemade recipes. A recipe is ingredients (foods with grams),
-- steps, and how many servings it makes. Its nutrition is the ingredients' summed, kept on a
-- food row (brand 'Recipe', serving = one serving) so it is found and logged like any food.
CREATE TABLE recipes (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL COLLATE NOCASE,          -- 'chili'
    servings    REAL NOT NULL CHECK (servings > 0),    -- how many servings the recipe makes
    steps       TEXT,                                   -- one step per line
    food_id     INTEGER REFERENCES foods(id),           -- the recipe as a loggable food
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, name)
);

CREATE TABLE recipe_ingredients (
    id         INTEGER PRIMARY KEY,                     -- a food may appear twice
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    food_id    INTEGER NOT NULL REFERENCES foods(id),
    grams      REAL NOT NULL CHECK (grams > 0)
);
CREATE INDEX idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id);
CREATE INDEX idx_recipe_ingredients_food ON recipe_ingredients(food_id);
