-- Mark foods the user deliberately added (barcode scans) so search_food ranks them as saved.
-- Foods cached only because they appeared in an FDC search keep saved = 0.
ALTER TABLE foods ADD COLUMN saved INTEGER NOT NULL DEFAULT 0;
