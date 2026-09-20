# Migrations

Empty on purpose. The whole schema is one baseline, `db/schema_pg.sql`, applied
once when the database is created; there is no history to replay.

**New migrations start at `001` on top of that baseline.** Name a file
`001_<short_description>.sql` and, if it can be undone, put the inverse in
`001_<short_description>.rollback.sql` beside it. `python -m db.migrate` applies
every `*.sql` here in filename order, each in its own transaction, and records
what it applied in `schema_migrations`; `*.rollback.sql` is never applied
automatically.

Two rules follow from how the runner works:

- **A file that has been applied is never edited or renamed.** A database that
  already ran it will not run it again, so the edit reaches new installs only
  and the two schemas drift apart. Write another migration instead.
- **`db/schema_pg.sql` is edited to match.** It is what a fresh install applies,
  so a change that lands only in a migration leaves new databases without it.
  Change both, in the same commit.

`db/local_grants.sql` grants on every table in `public`, including the ones a
migration adds later, so a new table does not need its own GRANT — but it does
need `ENABLE ROW LEVEL SECURITY` and a policy with both `USING` and
`WITH CHECK`, or it will be readable and writable across tenants.
