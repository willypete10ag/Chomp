# Migrations

`SCHEMA.sql` in the repo root is baseline version 1. Each later change is a file here named
`NNNN_<short_name>.sql` (starting at `0002_`), applied in order by `chomp/db.py` inside a
transaction and recorded in `PRAGMA user_version`. Never edit a migration that has run against
a real database; add a new one. Update `SCHEMA.sql`'s comments or ARCHITECTURE.md in the same
commit when a migration changes the design.

| File | Change |
|---|---|
| `0002_foods_saved.sql` | `foods.saved` for barcode-scanned foods |
| `0003_oauth_tokens.sql` | `oauth_tokens`: hashed OAuth tokens for the connector (#24) |
