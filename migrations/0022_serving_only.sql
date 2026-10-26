-- Foods with no serving weight (#141): restaurants publish nutrition per order ("1 appetizer")
-- without grams. Such a food's serving_g is a stand-in 100 g, so its per-100 g values are its
-- per-serving values and its entries' grams are servings x 100. Everything shown counts it in
-- servings, never grams.
ALTER TABLE foods ADD COLUMN serving_only INTEGER NOT NULL DEFAULT 0;
