-- Optional body goals beside the daily nutrition goals (#105): a goal weight in kg, drawn as
-- a line on the Weight chart, and a daily step goal, drawn on Activity's steps chart. NULL is
-- no goal. They live on the same row but aren't part of the macro targets the tools report.
ALTER TABLE targets ADD COLUMN goal_weight_kg REAL CHECK (goal_weight_kg > 0 AND goal_weight_kg < 700);
ALTER TABLE targets ADD COLUMN step_goal REAL CHECK (step_goal > 0 AND step_goal <= 200000);
