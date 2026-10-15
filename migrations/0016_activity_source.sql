-- Where an activity reading came from (#94): 'phone' (posted to /ingest/health) or 'export'
-- (Apple Health's export.zip, imported for history). Only the phone's readings date its last
-- send, and an import never replaces a day the phone sent, but a later export may refresh a
-- day an earlier export brought in.
ALTER TABLE activity ADD COLUMN source TEXT NOT NULL DEFAULT 'phone'
    CHECK (source IN ('phone', 'export'));
