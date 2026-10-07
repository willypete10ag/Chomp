-- Dashboard edits that can be undone (#54). Each row holds the touched meals, entries, foods
-- and targets as JSON before and after the edit; undo restores `before` only while the rows
-- still match `after`, so an edit made since (by Claude or another tab) is never overwritten.
-- subject is always 'owner' for now, like web_sessions.subject.
CREATE TABLE undo_actions (
    id          INTEGER PRIMARY KEY,
    subject     TEXT NOT NULL DEFAULT 'owner',
    summary     TEXT NOT NULL,              -- 'Deleted Egg, whole', shown with the Undo button
    before      TEXT NOT NULL,              -- JSON
    after       TEXT NOT NULL,              -- JSON
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL            -- unix seconds
);
