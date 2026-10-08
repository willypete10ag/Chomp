-- Label details under fat and carbs (#63): fat subtypes, cholesterol, added sugars and sugar
-- alcohols. Nullable columns on foods (per 100 g) and entries (snapshot at log time), like
-- fiber, sugar and sodium. NULL means the source didn't report it.

ALTER TABLE foods ADD COLUMN sat_fat_g REAL;
ALTER TABLE foods ADD COLUMN trans_fat_g REAL;
ALTER TABLE foods ADD COLUMN poly_fat_g REAL;
ALTER TABLE foods ADD COLUMN mono_fat_g REAL;
ALTER TABLE foods ADD COLUMN cholesterol_mg REAL;
ALTER TABLE foods ADD COLUMN added_sugar_g REAL;
ALTER TABLE foods ADD COLUMN sugar_alcohol_g REAL;

ALTER TABLE entries ADD COLUMN sat_fat_g REAL;
ALTER TABLE entries ADD COLUMN trans_fat_g REAL;
ALTER TABLE entries ADD COLUMN poly_fat_g REAL;
ALTER TABLE entries ADD COLUMN mono_fat_g REAL;
ALTER TABLE entries ADD COLUMN cholesterol_mg REAL;
ALTER TABLE entries ADD COLUMN added_sugar_g REAL;
ALTER TABLE entries ADD COLUMN sugar_alcohol_g REAL;

-- Undo records hold whole entry rows; ones saved before these columns existed would
-- never match the current rows again, so they go (they only last a day anyway).
DELETE FROM undo_actions;
