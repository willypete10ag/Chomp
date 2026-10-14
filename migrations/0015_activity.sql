-- Activity from Apple Health (#94): steps, active and resting energy, walking + running
-- distance, exercise minutes and flights climbed, posted by the phone to /ingest/health.

-- One reading: a metric's amount for the time bucket starting at start_at (a day when the
-- phone groups by day, an hour when by hour). A day's total is the sum of its buckets. The
-- phone resends the same buckets as they fill up, so a bucket already saved is replaced.
CREATE TABLE activity (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    metric      TEXT NOT NULL CHECK (metric IN
                    ('steps', 'active_kcal', 'resting_kcal', 'distance_m', 'exercise_min',
                     'flights')),
    start_at    TEXT NOT NULL,                                -- UTC ISO 8601, second precision
    qty         REAL NOT NULL CHECK (qty >= 0),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, metric, start_at)
);
CREATE INDEX activity_user_start ON activity (user_id, start_at);
