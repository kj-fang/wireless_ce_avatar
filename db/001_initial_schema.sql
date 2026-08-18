-- =============================================================================
-- IntelAvatar telemetry — PostgreSQL schema
--
-- Layering
--   bronze : raw_event      immutable JSONB, the replay source
--   silver : normalised entities with real constraints (this file's bulk)
--   gold   : star-shaped VIEWS for analytics (bottom of this file)
--
-- Gold is views, not tables. At the measured volume (~1,300 bytes per record,
-- ~1,000 conversations/year) a view over the silver tables answers in
-- milliseconds. Materialise one only after it is measured slow — a
-- materialized view is a refresh schedule and a staleness question, and
-- neither is worth taking on speculatively.
--
-- Sizing this was designed against, so the next reader can check the
-- assumption rather than inherit it:
--   131 conversations over 7 weeks, 14 users, 1,315 bytes average per record.
--   ~2.5 MB/year today; under 1 GB even at 100x. Do not partition. Do not
--   shard. Revisit both only past ~50M rows in fact_turn.
--
-- Requires PostgreSQL 12 or later, for the STORED generated columns on
-- turn.total_tokens and ai_invocation.total_tokens.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;


-- =============================================================================
-- BRONZE — every event exactly as the client sent it
-- =============================================================================

CREATE TABLE bronze.raw_event (
    event_id        uuid        PRIMARY KEY,
    event_type      text        NOT NULL,
    schema_version  smallint    NOT NULL,
    environment     text        NOT NULL,
    -- occurred_at comes off a laptop and cannot be trusted for ordering or
    -- bucketing: a machine with a wrong clock lands in the wrong fortnight.
    -- received_at is set by the server and is what analytics group by.
    occurred_at     timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    user_name       text        NOT NULL,
    app_version     text        NOT NULL DEFAULT '',
    payload         jsonb       NOT NULL,
    source_ref      text        NOT NULL DEFAULT '',   -- legacy file path or checksum
    CONSTRAINT raw_event_environment_ck
        CHECK (environment IN ('production', 'sim', 'format_check', 'dev'))
);

COMMENT ON TABLE bronze.raw_event IS
'Append-only. The ingestion API writes here and to silver in ONE transaction,
so silver can never contain a row whose source event is missing. Safe to drop
rows older than the retention window once silver is trusted; that is the only
deletion this table should ever see.';

-- Watermark for the share sync job. One row per source root. Written in the
-- same transaction as the events it covers, so a crash re-reads the window
-- rather than skipping it.
CREATE TABLE bronze.sync_state (
    source_root          text        PRIMARY KEY,
    last_run_started_at  timestamptz NOT NULL,
    last_run_finished_at timestamptz NOT NULL,
    files_seen           integer     NOT NULL DEFAULT 0,
    events_emitted       integer     NOT NULL DEFAULT 0
);

CREATE INDEX raw_event_received_idx ON bronze.raw_event (received_at);
CREATE INDEX raw_event_type_idx     ON bronze.raw_event (event_type, received_at);
-- No GIN on payload. Add one only when a real query needs it — it roughly
-- doubles write cost and the payload is not queried today.


-- =============================================================================
-- SILVER — dimensions
-- =============================================================================

-- The case technology. Two values, ever.
CREATE TABLE silver.technology (
    technology_id   smallint    PRIMARY KEY,
    code            text        NOT NULL UNIQUE,
    label           text        NOT NULL
);
INSERT INTO silver.technology VALUES
    (1, 'wifi', 'Wi-Fi'),
    (2, 'bt',   'Bluetooth'),
    (0, 'unknown', 'Unknown');

-- The agent that ran. NW is a tool that operates on Wi-Fi cases, so it is a
-- third agent but not a third technology — this FK is what keeps the two
-- questions ("what tech was the case?" / "which agent ran?") separable while
-- still reconciling to the same totals.
CREATE TABLE silver.agent (
    agent_id        smallint    PRIMARY KEY,
    code            text        NOT NULL UNIQUE,
    technology_id   smallint    NOT NULL REFERENCES silver.technology
);
INSERT INTO silver.agent VALUES
    (1, 'wifi', 1),
    (2, 'bt',   2),
    (3, 'nw',   1),          -- NW analyses Wi-Fi cases
    (0, 'unknown', 0);

CREATE TABLE silver.app_user (
    user_id     int         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_name   text        NOT NULL UNIQUE,
    first_seen  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL
);

-- case_nbr is absent on roughly half of real traffic (people analyse a log
-- without opening a case). That is a legitimate state, not missing data, so
-- the reference from a conversation is NULLABLE and carries its own source.
CREATE TABLE silver.support_case (
    case_id     int         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_nbr    text        NOT NULL UNIQUE,
    subject     text        NOT NULL DEFAULT '',
    issue_type  text        NOT NULL DEFAULT '',
    technology_id smallint  NOT NULL DEFAULT 0 REFERENCES silver.technology,
    first_seen  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL
);

CREATE TABLE silver.llm_model (
    model_id            int      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_name          text     NOT NULL UNIQUE,
    -- Rates live here for reference only. The rate actually charged is copied
    -- onto each turn at write time, because a rate change must not silently
    -- restate last quarter's spend.
    rate_input_per_mtok numeric(12,6),
    rate_output_per_mtok numeric(12,6),
    pricing_version     text     NOT NULL DEFAULT ''
);

CREATE TABLE silver.feature (
    feature_id  smallint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        text        NOT NULL UNIQUE,
    label       text        NOT NULL DEFAULT ''
);
INSERT INTO silver.feature (code, label) VALUES
    ('chatbot_turn',                  'Chat turn'),
    ('select_attachments_ai_summary', 'Attachment click-AI'),
    ('issue_time_prepass',            'Issue-time extraction'),
    ('sleepstudy_analysis',           'Sleep-study analysis');

-- Turn status carries a rank because two unordered threads write it: the route
-- reports the outcome it saw, the usage worker settles tokens milliseconds
-- later with its own default. The more specific outcome must win regardless of
-- arrival order, and the DB is the last place that can still enforce it.
CREATE TABLE silver.turn_status (
    status  text        PRIMARY KEY,
    rank    smallint    NOT NULL UNIQUE
);
INSERT INTO silver.turn_status VALUES
    ('started', 0), ('completed', 1), ('failed', 2), ('cancelled', 3);

-- Files are identified by content hash, never by name. Two users download the
-- same attachment to different paths; the same path is reused for different
-- content across runs.
CREATE TABLE silver.log_file (
    file_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256      bytea       NOT NULL UNIQUE,
    byte_size   bigint,
    file_name   text        NOT NULL DEFAULT '',
    -- The URI of the bytes, which stay in file storage. The database holds the
    -- reference, never the log itself.
    storage_uri text        NOT NULL DEFAULT '',
    first_seen  timestamptz NOT NULL
);


-- =============================================================================
-- SILVER — entities
-- =============================================================================

-- One per support case worked on. Client-generated UUID is the primary key:
-- it is globally unique, stable across retries, and is exactly what idempotent
-- ingestion has to upsert on anyway. A surrogate would add a lookup per insert
-- and buy nothing at this volume.
CREATE TABLE silver.workflow (
    workflow_id     uuid        PRIMARY KEY,
    user_id         int         NOT NULL REFERENCES silver.app_user,
    case_id         int             NULL REFERENCES silver.support_case,
    technology_id   smallint    NOT NULL DEFAULT 0 REFERENCES silver.technology,
    environment     text        NOT NULL,
    app_version     text        NOT NULL DEFAULT '',
    started_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    CONSTRAINT workflow_environment_ck
        CHECK (environment IN ('production', 'sim', 'format_check', 'dev'))
);

-- The conversation is the grain, NOT session_id. In production data the two are
-- currently 1:1 (131 of each), but session_id is the Flask session — it starts
-- spanning conversations the moment someone opens two chats in one browser
-- session. Modelled 1:N from the start so that day changes nothing.
CREATE TABLE silver.conversation (
    conversation_id uuid        PRIMARY KEY,
    workflow_id     uuid            NULL REFERENCES silver.workflow,
    http_session_id text        NOT NULL DEFAULT '',
    user_id         int         NOT NULL REFERENCES silver.app_user,
    case_id         int             NULL REFERENCES silver.support_case,
    -- Whether the case number was stated or guessed. Without this a value
    -- derived from a folder name is indistinguishable from one the user gave,
    -- and only 15 of 64 case-less sessions can be recovered from the path at
    -- all — the rest genuinely have no case.
    case_ref_source text        NOT NULL DEFAULT 'absent',
    agent_id        smallint    NOT NULL DEFAULT 0 REFERENCES silver.agent,
    technology_id   smallint    NOT NULL DEFAULT 0 REFERENCES silver.technology,
    environment     text        NOT NULL,
    app_version     text        NOT NULL DEFAULT '',
    started_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    issue_time      timestamptz     NULL,
    issue_window_minutes smallint   NULL,
    primary_file_id bigint          NULL REFERENCES silver.log_file,
    CONSTRAINT conversation_case_ref_ck
        CHECK (case_ref_source IN ('explicit', 'derived_from_path', 'absent')),
    CONSTRAINT conversation_environment_ck
        CHECK (environment IN ('production', 'sim', 'format_check', 'dev')),
    -- A derived or explicit source must actually have a case attached.
    CONSTRAINT conversation_case_consistency_ck
        CHECK ((case_ref_source = 'absent') = (case_id IS NULL))
);

-- One row per Send. Conversation totals are NOT stored — they are a SUM over
-- this table. That removes an entire bug class: the app previously accumulated
-- tokens and cost in place on the conversation record, which is what made a
-- late-arriving write able to corrupt an earlier settled figure.
CREATE TABLE silver.turn (
    turn_id         uuid        PRIMARY KEY,
    conversation_id uuid        NOT NULL REFERENCES silver.conversation
                                    ON DELETE CASCADE,
    seq             int             NULL,
    status          text        NOT NULL REFERENCES silver.turn_status,
    error_code      text        NOT NULL DEFAULT '',
    model_id        int             NULL REFERENCES silver.llm_model,
    input_tokens        bigint  NOT NULL DEFAULT 0,
    cache_read_tokens   bigint  NOT NULL DEFAULT 0,
    cache_write_tokens  bigint  NOT NULL DEFAULT 0,
    output_tokens       bigint  NOT NULL DEFAULT 0,
    -- Generated, so the total can never disagree with its parts.
    total_tokens    bigint      GENERATED ALWAYS AS
                        (input_tokens + cache_read_tokens
                         + cache_write_tokens + output_tokens) STORED,
    -- numeric, never float: money does not round the way binary floats do.
    -- NULL means "not priced", which is different from 0.00 meaning "free".
    cost_usd        numeric(12,6)   NULL,
    unpriced_model  text        NOT NULL DEFAULT '',
    -- The rate that was actually charged, snapshotted. Never recomputed.
    rate_input_per_mtok  numeric(12,6) NULL,
    rate_output_per_mtok numeric(12,6) NULL,
    pricing_version text        NOT NULL DEFAULT '',
    latency_ms      int             NULL,
    started_at      timestamptz NOT NULL,
    settled_at      timestamptz     NULL,
    CONSTRAINT turn_tokens_nonneg_ck CHECK (
        input_tokens >= 0 AND cache_read_tokens >= 0
        AND cache_write_tokens >= 0 AND output_tokens >= 0),
    CONSTRAINT turn_cost_nonneg_ck CHECK (cost_usd IS NULL OR cost_usd >= 0),
    -- An unpriced turn must say which model it could not price, and a priced
    -- one must not claim it was unpriced.
    CONSTRAINT turn_unpriced_ck CHECK (
        (cost_usd IS NULL) = (unpriced_model <> ''))
);

CREATE INDEX turn_conversation_idx ON silver.turn (conversation_id);
CREATE INDEX turn_started_idx      ON silver.turn (started_at);
CREATE INDEX turn_model_idx        ON silver.turn (model_id) WHERE model_id IS NOT NULL;

-- Every AI call made outside a chat turn: attachment click-AI, issue-time
-- prepass, sleep-study. Same shape as a turn but hangs off the workflow,
-- because these happen before an agent has even been chosen.
CREATE TABLE silver.ai_invocation (
    invocation_id   uuid        PRIMARY KEY,
    workflow_id     uuid        NOT NULL REFERENCES silver.workflow
                                    ON DELETE CASCADE,
    conversation_id uuid            NULL REFERENCES silver.conversation,
    feature_id      smallint    NOT NULL REFERENCES silver.feature,
    agent_id        smallint    NOT NULL DEFAULT 0 REFERENCES silver.agent,
    model_id        int             NULL REFERENCES silver.llm_model,
    input_tokens        bigint  NOT NULL DEFAULT 0,
    cache_read_tokens   bigint  NOT NULL DEFAULT 0,
    cache_write_tokens  bigint  NOT NULL DEFAULT 0,
    output_tokens       bigint  NOT NULL DEFAULT 0,
    total_tokens    bigint      GENERATED ALWAYS AS
                        (input_tokens + cache_read_tokens
                         + cache_write_tokens + output_tokens) STORED,
    cost_usd        numeric(12,6)   NULL,
    unpriced_model  text        NOT NULL DEFAULT '',
    pricing_version text        NOT NULL DEFAULT '',
    status          text        NOT NULL DEFAULT 'success',
    error_code      text        NOT NULL DEFAULT '',
    latency_ms      int             NULL,
    occurred_at     timestamptz NOT NULL,
    CONSTRAINT invocation_unpriced_ck CHECK (
        (cost_usd IS NULL) = (unpriced_model <> ''))
);

CREATE INDEX invocation_workflow_idx ON silver.ai_invocation (workflow_id);
CREATE INDEX invocation_feature_idx  ON silver.ai_invocation (feature_id, occurred_at);

-- One row per file the tool discovered for a case, with what happened to it.
-- The claim ("Log files attached: Yes") is on the workflow; the reality is
-- these rows. The gap between them is the whole point of the audit.
CREATE TABLE silver.attachment_event (
    attachment_event_id uuid    PRIMARY KEY,
    workflow_id     uuid        NOT NULL REFERENCES silver.workflow
                                    ON DELETE CASCADE,
    file_id         bigint          NULL REFERENCES silver.log_file,
    declared_name   text        NOT NULL DEFAULT '',
    was_selected    boolean     NOT NULL DEFAULT false,
    download_status text        NOT NULL DEFAULT 'not_attempted',
    error_code      text        NOT NULL DEFAULT '',
    occurred_at     timestamptz NOT NULL,
    CONSTRAINT attachment_status_ck CHECK (download_status IN
        ('not_attempted', 'succeeded', 'failed', 'cancelled', 'already_present'))
);

CREATE INDEX attachment_workflow_idx ON silver.attachment_event (workflow_id);

-- Join keys only. The feedback text itself stays in the feedback store; this
-- table exists so the two can be joined later without telemetry ever holding
-- free-form user writing.
CREATE TABLE silver.feedback_event (
    feedback_event_id uuid      PRIMARY KEY,
    conversation_id uuid            NULL REFERENCES silver.conversation,
    turn_id         uuid            NULL REFERENCES silver.turn,
    workflow_id     uuid            NULL REFERENCES silver.workflow,
    case_id         int             NULL REFERENCES silver.support_case,
    user_id         int         NOT NULL REFERENCES silver.app_user,
    environment     text        NOT NULL,
    submitted_at    timestamptz NOT NULL
);

CREATE INDEX feedback_turn_idx ON silver.feedback_event (turn_id)
    WHERE turn_id IS NOT NULL;


-- =============================================================================
-- IDEMPOTENT INGESTION — the upsert patterns
--
-- Every one of these is safe to run twice. The client retries on any
-- unacknowledged batch, so "twice" is the normal case, not the edge case.
-- =============================================================================

-- Bronze: the first line of defence. If this says DO NOTHING and reports 0
-- rows, the event was already processed and silver must not be touched.
--
--   INSERT INTO bronze.raw_event (event_id, event_type, ...)
--   VALUES (...)
--   ON CONFLICT (event_id) DO NOTHING
--   RETURNING event_id;
--
-- Turn: the one upsert that is not a plain overwrite. A late usage write must
-- never downgrade a status that a route already reported, so the rank decides.
--
--   INSERT INTO silver.turn AS t (turn_id, conversation_id, status, ...)
--   VALUES (...)
--   ON CONFLICT (turn_id) DO UPDATE SET
--       status = CASE
--           WHEN (SELECT rank FROM silver.turn_status WHERE status = EXCLUDED.status)
--              >= (SELECT rank FROM silver.turn_status WHERE status = t.status)
--           THEN EXCLUDED.status ELSE t.status END,
--       -- token and cost columns are last-write-wins: they are settled once by
--       -- the usage worker and never revised
--       input_tokens  = GREATEST(t.input_tokens,  EXCLUDED.input_tokens),
--       output_tokens = GREATEST(t.output_tokens, EXCLUDED.output_tokens),
--       cost_usd      = COALESCE(EXCLUDED.cost_usd, t.cost_usd),
--       settled_at    = COALESCE(EXCLUDED.settled_at, t.settled_at);
--
-- Backfilling the 131 legacy records: they predate event_id, so the importer
-- must synthesise a DETERMINISTIC one, e.g.
--     uuid_generate_v5(namespace, record_type || conversation_id || updated_at)
-- A random UUID per import run would defeat the deduplication entirely and the
-- reconciliation job would re-insert every legacy row on every pass.


-- =============================================================================
-- GOLD — analytics views. Every one filters to production.
-- =============================================================================

CREATE VIEW gold.v_conversation_spend AS
SELECT c.conversation_id,
       c.user_id, c.case_id, c.agent_id, c.technology_id,
       c.started_at::date            AS activity_date,
       count(t.turn_id)              AS turns,
       count(*) FILTER (WHERE t.status = 'cancelled') AS cancelled_turns,
       count(*) FILTER (WHERE t.status = 'failed')    AS failed_turns,
       coalesce(sum(t.total_tokens), 0)               AS total_tokens,
       sum(t.cost_usd)                                AS cost_usd,
       -- A conversation with any unpriced turn has a cost that is a floor,
       -- not a bill. Reporting must be able to say so.
       count(*) FILTER (WHERE t.cost_usd IS NULL)     AS unpriced_turns
FROM silver.conversation c
LEFT JOIN silver.turn t USING (conversation_id)
WHERE c.environment = 'production'
GROUP BY c.conversation_id, c.user_id, c.case_id, c.agent_id,
         c.technology_id, c.started_at;

-- The two-dimension reconciliation: agent-side and technology-side totals must
-- match, because they partition the same spend two different ways.
CREATE VIEW gold.v_spend_by_agent AS
SELECT a.code AS agent, count(*) AS conversations,
       sum(s.total_tokens) AS tokens, sum(s.cost_usd) AS cost_usd
FROM gold.v_conversation_spend s JOIN silver.agent a USING (agent_id)
GROUP BY a.code;

CREATE VIEW gold.v_spend_by_technology AS
SELECT tech.code AS technology, count(*) AS conversations,
       sum(s.total_tokens) AS tokens, sum(s.cost_usd) AS cost_usd
FROM gold.v_conversation_spend s JOIN silver.technology tech USING (technology_id)
GROUP BY tech.code;

CREATE VIEW gold.v_feature_spend AS
SELECT f.code AS feature, a.code AS agent,
       count(*) AS invocations,
       sum(i.total_tokens) AS tokens,
       sum(i.cost_usd) AS cost_usd,
       count(*) FILTER (WHERE i.cost_usd IS NULL) AS unpriced_invocations
FROM silver.ai_invocation i
JOIN silver.feature f USING (feature_id)
JOIN silver.agent   a USING (agent_id)
JOIN silver.workflow w USING (workflow_id)
WHERE w.environment = 'production'
GROUP BY f.code, a.code;

-- Fortnightly buckets anchored on one fixed Monday and counted in days.
-- Deriving parity from the ISO week number instead would flip the bucket
-- boundary at every year that has 53 weeks.
CREATE VIEW gold.v_fortnightly_usage AS
SELECT DATE '2026-06-22'
         + (floor((c.started_at::date - DATE '2026-06-22') / 14.0)::int * 14)
                                            AS fortnight_start,
       count(*)                             AS conversations,
       count(DISTINCT c.user_id)            AS active_users,
       count(DISTINCT c.case_id)            AS cases_touched,
       count(*) FILTER (WHERE c.case_id IS NULL) AS conversations_without_case,
       count(*) FILTER (WHERE c.issue_time IS NOT NULL) AS with_issue_time
FROM silver.conversation c
WHERE c.environment = 'production'
GROUP BY 1 ORDER BY 1;

-- The data-quality KPI, as a query rather than a spreadsheet someone maintains.
CREATE VIEW gold.v_data_quality AS
SELECT date_trunc('month', c.started_at) AS month,
       count(*) AS conversations,
       round(100.0 * count(*) FILTER (WHERE c.case_id IS NOT NULL) / count(*), 1)
                                                        AS pct_with_case,
       round(100.0 * count(*) FILTER (WHERE c.case_ref_source = 'derived_from_path')
             / count(*), 1)                             AS pct_case_derived,
       round(100.0 * count(*) FILTER (WHERE c.issue_time IS NOT NULL) / count(*), 1)
                                                        AS pct_with_issue_time,
       round(100.0 * count(*) FILTER (WHERE c.app_version <> '') / count(*), 1)
                                                        AS pct_with_app_version,
       round(100.0 * count(*) FILTER (WHERE EXISTS (
             SELECT 1 FROM silver.turn t
             WHERE t.conversation_id = c.conversation_id AND t.cost_usd IS NULL))
             / count(*), 1)                             AS pct_with_unpriced_turn
FROM silver.conversation c
WHERE c.environment = 'production'
GROUP BY 1 ORDER BY 1;
