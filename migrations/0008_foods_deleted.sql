-- Foods deleted from the dashboard (#59 follow-up). Logged entries keep pointing at the row
-- (their nutrients are snapshots), so a deleted food stays in the table, hidden from search
-- and lists, with its barcode released.
ALTER TABLE foods ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0;
