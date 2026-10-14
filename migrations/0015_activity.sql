-- Activity from Apple Health (#94): steps, active and resting energy, walking + running
-- distance, exercise minutes and flights climbed, posted by the phone to /ingest/health.

-- One reading: a metric's amount for the time bucket starting at start_at, either a whole
-- day (period 'day': the Shortcut, or Health Auto Export grouped by day) or part of one
-- ('part': grouped by hour). A day's total is its 'day' reading or the sum of its parts,
-- whichever the phone sent last (chomp/logic.py). The phone resends the same buckets as they
-- fill up, so a bucket already saved is replaced; updated_at is when it was last sent.
CREATE TABLE activity (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    metric      TEXT NOT NULL CHECK (metric IN
                    ('steps', 'active_kcal', 'resting_kcal', 'distance_m', 'exercise_min',
                     'flights')),
    period      TEXT NOT NULL CHECK (period IN ('day', 'part')),
    start_at    TEXT NOT NULL,                                -- UTC ISO 8601, second precision
    qty         REAL NOT NULL CHECK (qty >= 0),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, metric, period, start_at)
);
CREATE INDEX activity_user_start ON activity (user_id, start_at);
