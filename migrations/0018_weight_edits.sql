-- Weigh-ins edited on the dashboard (#105). A phone resending the reading it first sent would
-- otherwise put the old number back, so an edited weigh-in keeps what the person typed: the
-- phone ("health") leaves rows with edited = 1 alone, as it leaves deleted ones.
ALTER TABLE weights ADD COLUMN edited INTEGER NOT NULL DEFAULT 0;
