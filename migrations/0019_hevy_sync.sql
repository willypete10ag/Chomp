-- Hevy API sync (#113): workouts pulled from Hevy's API carry Hevy's workout id, so edits and
-- deletes made in Hevy find the right row. Rows from the CSV import have none until the first
-- sync adopts them by start time.
ALTER TABLE workouts ADD COLUMN hevy_id TEXT;
CREATE UNIQUE INDEX idx_workouts_hevy_id ON workouts(user_id, hevy_id) WHERE hevy_id IS NOT NULL;

-- Each person's sync with Hevy: `since` is the time the next run asks Hevy's workout events
-- feed from (NULL until the first full pull), plus how the last run went.
CREATE TABLE hevy_sync (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    since        TEXT,                              -- UTC ISO 8601
    last_run_at  TEXT,                              -- UTC, the last run that finished
    last_ok_at   TEXT,                              -- UTC, the last run without an error
    last_error   TEXT,                              -- NULL when the last run worked
    added        INTEGER NOT NULL DEFAULT 0,        -- what the last successful run changed
    updated      INTEGER NOT NULL DEFAULT 0,
    deleted      INTEGER NOT NULL DEFAULT 0
);
