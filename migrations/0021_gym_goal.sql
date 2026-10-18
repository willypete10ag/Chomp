-- A weekly gym-day goal beside the body goals: how many days a week the person means to
-- train (Will's is 6). A day with at least one workout counts; weeks run Monday to Sunday.
-- NULL is no goal.
ALTER TABLE targets ADD COLUMN gym_days_goal INTEGER CHECK (gym_days_goal BETWEEN 1 AND 7);
