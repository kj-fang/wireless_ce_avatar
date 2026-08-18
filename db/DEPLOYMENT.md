# IntelAvatar telemetry deployment on PostgreSQL

This procedure uses the DBaaS connection supplied by
`feature/db_setup_guide`, while keeping the telemetry schema and database
drivers outside the IntelAvatar EXE.

## 1. Prepare one ingestion worker

Use a Windows or Linux host that can reach both the Gather SMB share and the
PostgreSQL server.  The PostgreSQL database server itself does not need to run
these Python jobs.

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r db\requirements.txt
Copy-Item db\.env.example db\.env
```

Edit `db/.env` and set `TELEMETRY_DB_PASSWORD`.  The file is ignored by Git.
Do not put a password in source code, `IntelAvatar.spec`, a slide deck, or a
scheduled-task command line.

## 2. Read-only connection, privilege, and size check

```powershell
python -m db.check_postgres
```

This reports the PostgreSQL version, current database/user, database size,
CREATE privileges, target-schema existence, and the largest telemetry tables.
It never creates or modifies an object.

The supplied service-owner login must be able to create the `bronze`, `silver`,
and `gold` schemas for initialisation.  If it only has CREATE TABLE in `public`,
ask the DBA either to run the schema file or grant the required database-level
CREATE permission temporarily.

## 3. Initialise the schema

Preflight first:

```powershell
python -m db.apply_schema
```

The command refuses to continue if PostgreSQL is older than 12, the login lacks
CREATE permission, or any target schema name already exists.  Apply only after
the preflight is clean:

```powershell
python -m db.apply_schema --apply
```

The DDL is executed in one transaction.  An error rolls the initialisation
back.  It creates the Bronze raw-event layer, the normalised Silver tables, the
Gold reporting views, indexes, constraints, and reference rows.

For least privilege, ask the DBA to create a separate `avatar_ingest` login for
the scheduled job after initialisation.  The service-owner login should not be
used by the nightly task indefinitely.

## 4. Validate the Gather source without touching PostgreSQL

```powershell
python -m db.sync_share --dry-run
```

Confirm that files are visible, unreadable is zero, event counts are plausible,
and event-id collisions are zero.

## 5. First load and idempotency check

```powershell
python -m db.sync_share --full
python -m db.sync_share --full
```

The first run should report accepted events.  The immediate second run should
report those events as duplicates, with no extra database rows or spend.

Then inspect the database again:

```powershell
python -m db.check_postgres
python -m db.reprice --dry-run
```

Only remove `--dry-run` from `reprice` after its model, token, and USD totals
have been reviewed.

## 6. Schedule incremental sync

Schedule this command nightly on the ingestion worker:

```powershell
python -m db.sync_share
```

Configure the scheduler to use the virtual-environment Python, capture stdout
and stderr, alert on a non-zero exit code, and never start a second instance
while the previous one is still running.  The sync watermark and six-hour
overlap make later runs incremental without losing records written mid-scan.

## Commands that are intentionally not used

Do not run the reference branch's `02_create_table_example.py` or
`03_crud_operations.py` against the production database.  They create, update,
and delete demonstration data in `public`; they are unrelated to the telemetry
schema.
