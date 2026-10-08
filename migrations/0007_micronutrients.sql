-- Vitamins and minerals (#53): nullable columns on foods (per 100 g) and entries (snapshot
-- at log time), like fiber, sugar and sodium. NULL means the source didn't report it.
-- Units are in the names: vitamin A as mcg RAE, folate as mcg DFE.

ALTER TABLE foods ADD COLUMN vitamin_d_mcg REAL;
ALTER TABLE foods ADD COLUMN calcium_mg REAL;
ALTER TABLE foods ADD COLUMN iron_mg REAL;
ALTER TABLE foods ADD COLUMN potassium_mg REAL;
ALTER TABLE foods ADD COLUMN vitamin_a_mcg REAL;
ALTER TABLE foods ADD COLUMN vitamin_c_mg REAL;
ALTER TABLE foods ADD COLUMN vitamin_e_mg REAL;
ALTER TABLE foods ADD COLUMN vitamin_k_mcg REAL;
ALTER TABLE foods ADD COLUMN thiamin_mg REAL;
ALTER TABLE foods ADD COLUMN riboflavin_mg REAL;
ALTER TABLE foods ADD COLUMN niacin_mg REAL;
ALTER TABLE foods ADD COLUMN vitamin_b6_mg REAL;
ALTER TABLE foods ADD COLUMN folate_mcg REAL;
ALTER TABLE foods ADD COLUMN vitamin_b12_mcg REAL;
ALTER TABLE foods ADD COLUMN phosphorus_mg REAL;
ALTER TABLE foods ADD COLUMN magnesium_mg REAL;
ALTER TABLE foods ADD COLUMN zinc_mg REAL;

ALTER TABLE entries ADD COLUMN vitamin_d_mcg REAL;
ALTER TABLE entries ADD COLUMN calcium_mg REAL;
ALTER TABLE entries ADD COLUMN iron_mg REAL;
ALTER TABLE entries ADD COLUMN potassium_mg REAL;
ALTER TABLE entries ADD COLUMN vitamin_a_mcg REAL;
ALTER TABLE entries ADD COLUMN vitamin_c_mg REAL;
ALTER TABLE entries ADD COLUMN vitamin_e_mg REAL;
ALTER TABLE entries ADD COLUMN vitamin_k_mcg REAL;
ALTER TABLE entries ADD COLUMN thiamin_mg REAL;
ALTER TABLE entries ADD COLUMN riboflavin_mg REAL;
ALTER TABLE entries ADD COLUMN niacin_mg REAL;
ALTER TABLE entries ADD COLUMN vitamin_b6_mg REAL;
ALTER TABLE entries ADD COLUMN folate_mcg REAL;
ALTER TABLE entries ADD COLUMN vitamin_b12_mcg REAL;
ALTER TABLE entries ADD COLUMN phosphorus_mg REAL;
ALTER TABLE entries ADD COLUMN magnesium_mg REAL;
ALTER TABLE entries ADD COLUMN zinc_mg REAL;

-- Undo records hold whole entry rows; ones saved before these columns existed would
-- never match the current rows again, so they go (they only last a day anyway).
DELETE FROM undo_actions;
