-- Workouts (#94): each person's strength workouts, imported from Hevy's CSV export (Hevy has
-- no API without Pro). A workout is identified by its start time, so re-importing an export
-- replaces those workouts' sets instead of adding duplicates.
CREATE TABLE workouts (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at   TEXT NOT NULL,                     -- UTC ISO 8601
    ended_at     TEXT,
    title        TEXT NOT NULL,                     -- 'Push day'
    description  TEXT,
    source       TEXT NOT NULL CHECK (source IN ('hevy')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, started_at)
);

-- One set, in the order Hevy lists them. Weight in kg, distance in metres; NULL when the
-- exercise doesn't use it (bodyweight, timed or cardio sets).
CREATE TABLE workout_sets (
    id              INTEGER PRIMARY KEY,
    workout_id      INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,               -- order of the set in the workout
    exercise        TEXT NOT NULL,                  -- 'Bench Press (Barbell)'
    exercise_notes  TEXT,
    superset_id     INTEGER,
    set_index       INTEGER NOT NULL,               -- 0-based within the exercise
    set_type        TEXT NOT NULL DEFAULT 'normal', -- normal, warmup, failure, dropset
    weight_kg       REAL,
    reps            INTEGER,
    distance_m      REAL,
    duration_s      REAL,
    rpe             REAL
);
CREATE INDEX idx_workout_sets_workout ON workout_sets(workout_id, position);
CREATE INDEX idx_workout_sets_exercise ON workout_sets(exercise);
