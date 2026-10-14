# Migrations

`SCHEMA.sql` in the repo root is baseline version 1. Each later change is a file here named
`NNNN_<short_name>.sql` (starting at `0002_`), applied in order by `chomp/db.py` inside a
transaction and recorded in `PRAGMA user_version`. Foreign keys are off while a migration runs,
so it can rebuild a table (create new, copy, drop old, rename), and `PRAGMA foreign_key_check`
must pass before it commits. Never edit a migration that has run against
a real database; add a new one. Update `SCHEMA.sql`'s comments or ARCHITECTURE.md in the same
commit when a migration changes the design.

| File | Change |
|---|---|
| `0002_foods_saved.sql` | `foods.saved` for barcode-scanned foods |
| `0003_oauth_tokens.sql` | `oauth_tokens`: hashed OAuth tokens for the connector (#24) |
| `0004_web_sessions.sql` | `web_sessions`: hashed browser sign-ins for the dashboard |
| `0005_undo_actions.sql` | `undo_actions`: before/after rows of dashboard edits, for Undo (#54) |
| `0006_users.sql` | `users`; `user_id` on meals, targets, aliases, templates, undo, sessions, tokens (#46) |
| `0007_micronutrients.sql` | 17 vitamin and mineral columns on `foods` and `entries` (#53) |
| `0008_foods_deleted.sql` | `foods.deleted`: foods deleted from the dashboard, kept for past entries |
| `0009_label_details.sql` | Fat subtypes, cholesterol, added sugars, sugar alcohols on `foods` and `entries` (#63) |
| `0010_portions.sql` | `portions` replace `aliases` (nicknames with grams carried over); `entries.estimated` (#75) |
| `0011_off_saved.sql` | Open Food Facts foods so far marked saved: OFF search hits (#88) are a lookup cache like FDC's |
| `0012_recipes.sql` | `recipes` and `recipe_ingredients`; each recipe backed by a food row for logging (#82) |
| `0013_weights.sql` | `weights`: each person's weigh-ins in kg; `ingest_tokens`: hashed phone tokens for `/ingest/health` (#94, #101) |
