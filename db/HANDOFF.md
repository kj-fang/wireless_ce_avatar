# IntelAvatar telemetry — PostgreSQL handoff

Everything the database owner needs to stand this up. Nothing in this directory
has ever been executed against a live PostgreSQL instance — it is parse-checked
and unit-checked only, so please treat the verification section as required
rather than optional.

**Contact:** Tu Yuan Yuan (yuan-yuan.tu@intel.com) · repo `kj-fang/wireless_ce_avatar`

---

## 1. What this is

A support engineering tool (IntelAvatar) writes one JSON file per telemetry
record to an SMB share. This project moves that data into PostgreSQL so it can
be queried, constrained and reported on.

Current live volume, measured 2026-08-18:

| | |
|---|---|
| Records on the share | 175 files (150 sessions, 23 workflows, 2 feedback) |
| Average record size | 1,315 bytes |
| Rate | ~19 conversations/week, 14 users |
| Projected | **~2.5 MB/year today, under 1 GB at 100× growth** |

This is a small database. The design deliberately avoids partitioning,
sharding, and a warehouse tier; see §7 for what *not* to add.

---

## 2. What to create

```bash
python -m db.check_postgres       # read-only connection/size/privilege check
python -m db.apply_schema         # read-only schema preflight
python -m db.apply_schema --apply # initialise in one transaction
```

One file, idempotent at the schema level (`CREATE SCHEMA IF NOT EXISTS`), but
the tables are not `IF NOT EXISTS` — it is meant to run once on an empty
database. Requires **PostgreSQL 12+** for the `GENERATED ALWAYS AS ... STORED`
columns on `turn.total_tokens` and `ai_invocation.total_tokens`.

It creates:

| Schema | Contents |
|---|---|
| `bronze` | `raw_event` (immutable JSONB), `sync_state` (job watermark) |
| `silver` | 8 dimensions + 6 entity tables, with real FKs and CHECKs |
| `gold` | 6 views for reporting |

Reference data (`technology`, `agent`, `turn_status`, `feature`) is inserted by
the same file.

---

## 3. Roles and grants

Two principals, least privilege:

```sql
-- the sync job / ingestion API
CREATE ROLE avatar_ingest LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA bronze, silver TO avatar_ingest;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA bronze, silver TO avatar_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA silver TO avatar_ingest;

-- reporting (Power BI, analysts)
CREATE ROLE avatar_read LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA gold TO avatar_read;
GRANT SELECT ON ALL TABLES IN SCHEMA gold TO avatar_read;
```

`avatar_ingest` needs UPDATE because ingestion is upsert-based. It never needs
DELETE — nothing in this design deletes rows except the retention job in §6.

Readers should be given **`gold` only**. Every gold view already filters
`environment = 'production'`, which keeps test and simulation records out of
reports without anyone having to remember to exclude them.

---

## 4. The data model

Full diagram: the DDL is commented table by table. The shape in one paragraph:

A **workflow** is one support case being worked on. It has zero or more
**conversations** (the grain — chat sessions), each with zero or more **turns**
(one per message sent). Separately a workflow accumulates **ai_invocations**
(AI calls made outside a chat turn — attachment triage, issue-time extraction)
and **attachment_events** (one per file the tool tried to fetch).
**feedback_events** carry join keys only, never feedback text.

Three things that are easy to get wrong and are enforced in the schema:

1. **`conversation.case_id` is nullable and that is correct.** Roughly half of
   real traffic analyses a log without any case attached. `case_ref_source`
   records whether the case number was stated by the user, derived from the
   file path, or genuinely absent, and a CHECK keeps the two consistent.

2. **`turn.status` is ranked, not last-write-wins.** Two unordered threads
   write a turn: the route reports the outcome it saw, and a usage worker
   settles tokens milliseconds later with its own default of `completed`.
   `silver.turn_status.rank` (`started` 0 < `completed` 1 < `failed` 2 <
   `cancelled` 3) decides which survives. The application's upsert relies on
   this table existing with those exact ranks.

3. **`cost_usd` is `numeric`, and NULL means "could not be priced"** — which is
   different from `0.00` meaning free. `unpriced_model` names the model whose
   rate was missing, and a CHECK requires the two to agree. This is live today:
   see §8.

Conversation and workflow totals are **not stored**. They are `SUM()` in the
gold views, which is what makes a replayed event harmless.

---

## 5. Getting data in

Two paths, both already written. Only the first is needed to start.

### 5a. Scheduled share sync (recommended first step)

```bash
pip install -r db/requirements.txt
# Copy db/.env.example to db/.env and add the password on the worker.
python -m db.sync_share
```

Reads the SMB share and lands everything. Run it nightly (Windows Task
Scheduler or cron on any machine that can reach both the share and the
database). No change to the client application is required.

* First run: pass `--full`, or simply let it run with no watermark — same thing.
* Later runs: reads only files modified since the last run, minus a 6-hour
  overlap. Measured on the live share: **36 of 175 files for a 7-day window**.
* `--dry-run` builds the events and prints a summary without touching the
  database. Safe to run before pointing it at production.

Timing on the live share from a laptop: metadata scan 38 s, full read 380 s,
incremental read a few seconds. The share is slow per-file (~2.2 s to open one
file), which is precisely why the job exists.

### 5b. HTTP ingestion API (later)

```bash
TELEMETRY_DSN="postgresql+psycopg://..." TELEMETRY_TOKEN="..." \
  uvicorn db.api:app --host 0.0.0.0 --port 8080
```

`POST /v1/events` takes a batch and returns which ids were committed.
`GET /healthz` is liveness (does not touch the database); `GET /readyz` probes
it. This is for when clients push directly rather than via the share — not
needed on day one.

### Idempotency contract

Every event carries a UUID `event_id`, derived deterministically from the
record's own fields. Ingestion is:

```sql
INSERT INTO bronze.raw_event ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id;
-- no row returned  =>  already applied, silver is not touched
```

bronze and silver are written **in one transaction**, and each event sits in
its own SAVEPOINT so one malformed event neither rolls back the batch nor
leaves a bronze row whose silver never landed. Re-running the sync is always
safe; re-running it twice in a row should report everything as `duplicate`.

---

## 6. Operations

| Topic | Recommendation |
|---|---|
| **Backup** | Standard nightly dump. At this size a full `pg_dump` is seconds. `bronze.raw_event` is the replay source — if silver is ever wrong, it can be rebuilt from bronze without touching the share. |
| **Retention** | Keep `silver` indefinitely (it is tiny). `bronze.raw_event` may be pruned after ~12 months once silver is trusted; that is the only table anything should ever delete from. |
| **PII** | Rows carry Intel usernames, case numbers and original log file paths. The paths contain machine names (e.g. `LUS-5CG4503BMZ`). **Please confirm this is acceptable under the applicable data policy before go-live**, and decide whether `conversation.log_path` should be stored, hashed, or dropped. Log *contents* never enter the database. |
| **Monitoring** | `bronze.sync_state.last_run_finished_at` going stale is the signal that the sync job has died. One alert on that is sufficient. |
| **Migrations** | Alembic is in `db/requirements.txt` but not yet configured. If you prefer to own schema changes yourself, say so and we will send DDL patches instead. Whatever the mechanism, migrations must run **once before** the service starts, never concurrently from multiple workers. |

---

## 7. Deliberately not doing

Please push back if you disagree, but these are conscious choices given the
measured volume, not oversights:

* **No partitioning.** Revisit past ~50M rows in `silver.turn`; current
  trajectory reaches that in several centuries.
* **No GIN index on `raw_event.payload`.** It roughly doubles write cost and
  nothing queries the payload today. Add one when a query needs it.
* **No materialized views.** Gold is plain views. Materialise an individual one
  only after it is measured slow — a matview is a refresh schedule and a
  staleness question.
* **No warehouse tier / Kafka / Spark.** 2.5 MB a year.

---

## 8. Verification (please run these)

The application-side code has been checked as far as it can be without a
server: SQL compiles to the expected statements, event ids are deterministic,
and the record→event mapping is verified against the live share. **What has
never run is the SQL itself.** Specifically worth confirming:

1. `001_initial_schema.sql` applies cleanly on an empty database.
2. The CHECK constraints reject what they should:
   ```sql
   -- must fail: case_ref_source says explicit but case_id is NULL
   INSERT INTO silver.conversation (conversation_id, user_id, case_ref_source,
                                    environment, started_at, updated_at)
   VALUES (gen_random_uuid(), 1, 'explicit', 'production', now(), now());

   -- must fail: priced row still naming an unpriced model
   -- (cost_usd IS NULL) = (unpriced_model <> '')
   ```
3. The ranked turn upsert really refuses to downgrade:
   ```sql
   -- insert a turn as 'cancelled', then upsert the same turn_id as 'completed'
   -- expected: status stays 'cancelled', tokens take the larger value
   ```
4. `numeric(12,6)` round-trips money without float error.
5. A full `python -m db.sync_share --dsn ...` followed immediately by a second
   run: the second must report `accepted=0` and everything as `duplicate`.
6. Foreign key insert ordering holds under a real batch (the code inserts
   dimensions before entities within each event's savepoint).

If any of these fail, the fix belongs in `db/ingest.py` or the DDL — please
send the error rather than working around it locally, so the repository stays
the source of truth.

---

## 9. Unpriced rows, and settling them later

`cost_usd IS NULL` means the model name was not in the rate table, so the
tokens were recorded and no figure was guessed. `gold.v_conversation_spend`
exposes `unpriced_turns` alongside the total, so a partial total is never
mistaken for a complete bill.

This was live: the deployed client reported its model as `claude-4-6-sonnet`
while the table was keyed `claude-sonnet-4-6`, leaving 481,001 tokens across 26
invocations unpriced. `configs/llm_pricing.py` now derives the transposed
spelling from the rate table itself, so new records price correctly and the two
cannot drift apart.

Rows already stored are settled by replaying the usage bronze already holds:

```bash
python -m db.reprice --dry-run   # report only
python -m db.reprice             # apply
```

**It only fills NULLs. A settled figure is never recomputed** — every statement
carries `WHERE cost_usd IS NULL`. That distinction matters: filling a gap is
not the same as restating spend that has already been quoted, and this tool
must never become a "recalculate everything at today's rates" button. Running
it twice is a no-op.

Expect it to report roughly **$1.90 across 26 invocations** on the first run
against the current share contents. Anything it cannot price is listed by model
name so the rate table can be extended.
