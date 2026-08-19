-- Split how a record was produced from where it belongs, and record which
-- build produced it.
--
-- WHY
-- ---
-- `environment` was carrying two unrelated ideas at once:
--
--     production / dev          where the data belongs
--     sim / format_check        how the record came to exist
--
-- Those are orthogonal. A format-check record produced by a developer build is
-- both "format_check" and "dev", and one column cannot say so. This is the same
-- conflation v6 removed when it split the overloaded `domain` into case_domain
-- and agent_domain, and it is worth removing before Developer-mode collection
-- starts writing to the share rather than after.
--
-- Three columns replace one:
--
--     execution_mode   exe | developer     how the program was running
--     environment      production | dev    where the data belongs
--     record_kind      real | sim | format_check
--                                          whether this describes real work
--
-- Backfill is a statement of fact, not a guess: before this change Developer
-- mode never wrote to the share at all, so every existing record is `exe`.
-- Records previously marked `sim` or `format_check` keep that meaning in
-- record_kind and become `dev` in environment, because neither describes
-- production traffic.
--
-- Reporting should filter on `record_kind = 'real' AND environment =
-- 'production'`. The gold views are updated here to do exactly that, so a
-- Developer-mode record can never reach a production number by default.

BEGIN;

-- ---------------------------------------------------------------- bronze --
ALTER TABLE avatar_bronze_raw_event
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'exe',
    ADD COLUMN record_kind    text NOT NULL DEFAULT 'real',
    ADD CONSTRAINT avatar_bronze_raw_event_execution_mode_ck
        CHECK (execution_mode IN ('exe', 'developer')),
    ADD CONSTRAINT avatar_bronze_raw_event_record_kind_ck
        CHECK (record_kind IN ('real', 'sim', 'format_check'));

UPDATE avatar_bronze_raw_event
   SET record_kind = environment
 WHERE environment IN ('sim', 'format_check');

UPDATE avatar_bronze_raw_event
   SET environment = 'dev'
 WHERE environment IN ('sim', 'format_check');

ALTER TABLE avatar_bronze_raw_event
    DROP CONSTRAINT IF EXISTS raw_event_environment_ck,
    DROP CONSTRAINT IF EXISTS avatar_bronze_raw_event_environment_ck;
ALTER TABLE avatar_bronze_raw_event
    ADD CONSTRAINT avatar_bronze_raw_event_environment_ck
        CHECK (environment IN ('production', 'dev'));

-- ---------------------------------------------------------------- silver --
ALTER TABLE avatar_silver_workflow
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'exe',
    ADD COLUMN record_kind    text NOT NULL DEFAULT 'real',
    ADD CONSTRAINT avatar_silver_workflow_execution_mode_ck
        CHECK (execution_mode IN ('exe', 'developer')),
    ADD CONSTRAINT avatar_silver_workflow_record_kind_ck
        CHECK (record_kind IN ('real', 'sim', 'format_check'));

UPDATE avatar_silver_workflow SET record_kind = environment
 WHERE environment IN ('sim', 'format_check');
UPDATE avatar_silver_workflow SET environment = 'dev'
 WHERE environment IN ('sim', 'format_check');

ALTER TABLE avatar_silver_workflow
    DROP CONSTRAINT IF EXISTS avatar_silver_workflow_environment_ck;
ALTER TABLE avatar_silver_workflow
    ADD CONSTRAINT avatar_silver_workflow_environment_ck
        CHECK (environment IN ('production', 'dev'));

ALTER TABLE avatar_silver_conversation
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'exe',
    ADD COLUMN record_kind    text NOT NULL DEFAULT 'real',
    ADD CONSTRAINT avatar_silver_conversation_execution_mode_ck
        CHECK (execution_mode IN ('exe', 'developer')),
    ADD CONSTRAINT avatar_silver_conversation_record_kind_ck
        CHECK (record_kind IN ('real', 'sim', 'format_check'));

UPDATE avatar_silver_conversation SET record_kind = environment
 WHERE environment IN ('sim', 'format_check');
UPDATE avatar_silver_conversation SET environment = 'dev'
 WHERE environment IN ('sim', 'format_check');

ALTER TABLE avatar_silver_conversation
    DROP CONSTRAINT IF EXISTS avatar_silver_conversation_environment_ck;
ALTER TABLE avatar_silver_conversation
    ADD CONSTRAINT avatar_silver_conversation_environment_ck
        CHECK (environment IN ('production', 'dev'));

-- The conversation carries its own copy rather than reading the workflow's.
-- Every conversation currently in the database has workflow_id IS NULL —
-- pre-v6 records have no workflow concept and roughly half of live traffic
-- never opens a case — so resolving the mode through the parent would leave
-- most rows with no execution context at all.
ALTER TABLE avatar_silver_feedback_event
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'exe',
    ADD COLUMN record_kind    text NOT NULL DEFAULT 'real',
    ADD CONSTRAINT avatar_silver_feedback_event_execution_mode_ck
        CHECK (execution_mode IN ('exe', 'developer')),
    ADD CONSTRAINT avatar_silver_feedback_event_record_kind_ck
        CHECK (record_kind IN ('real', 'sim', 'format_check'));

UPDATE avatar_silver_feedback_event SET record_kind = environment
 WHERE environment IN ('sim', 'format_check');
UPDATE avatar_silver_feedback_event SET environment = 'dev'
 WHERE environment IN ('sim', 'format_check');

ALTER TABLE avatar_silver_feedback_event
    DROP CONSTRAINT IF EXISTS avatar_silver_feedback_event_environment_ck;
ALTER TABLE avatar_silver_feedback_event
    ADD CONSTRAINT avatar_silver_feedback_event_environment_ck
        CHECK (environment IN ('production', 'dev'));

CREATE INDEX avatar_bronze_raw_event_mode_idx
    ON avatar_bronze_raw_event (execution_mode, record_kind, received_at);
CREATE INDEX avatar_silver_conversation_mode_idx
    ON avatar_silver_conversation (execution_mode, record_kind);

-- ------------------------------------------------------------------ gold --
-- Every view now excludes Developer-mode and non-real records explicitly.
-- Previously `environment = 'production'` did that job implicitly; after the
-- split it no longer would, so leaving the views untouched would have silently
-- let Developer traffic into production figures.
DROP VIEW IF EXISTS avatar_gold_spend_by_agent;
DROP VIEW IF EXISTS avatar_gold_spend_by_technology;
DROP VIEW IF EXISTS avatar_gold_conversation_spend;
DROP VIEW IF EXISTS avatar_gold_feature_spend;
DROP VIEW IF EXISTS avatar_gold_fortnightly_usage;
DROP VIEW IF EXISTS avatar_gold_data_quality;

CREATE VIEW avatar_gold_conversation_spend AS
SELECT c.conversation_id,
       c.user_id, c.case_id, c.agent_id, c.technology_id,
       c.execution_mode,
       c.started_at::date            AS activity_date,
       count(t.turn_id)              AS turns,
       count(*) FILTER (WHERE t.status = 'cancelled') AS cancelled_turns,
       count(*) FILTER (WHERE t.status = 'failed')    AS failed_turns,
       coalesce(sum(t.total_tokens), 0)               AS total_tokens,
       sum(t.cost_usd)                                AS cost_usd,
       count(*) FILTER (WHERE t.cost_usd IS NULL)     AS unpriced_turns
FROM avatar_silver_conversation c
LEFT JOIN avatar_silver_turn t USING (conversation_id)
WHERE c.environment = 'production'
  AND c.record_kind = 'real'
  AND c.execution_mode = 'exe'
GROUP BY c.conversation_id, c.user_id, c.case_id, c.agent_id,
         c.technology_id, c.execution_mode, c.started_at;

CREATE VIEW avatar_gold_spend_by_agent AS
SELECT a.code AS agent, count(*) AS conversations,
       sum(s.total_tokens) AS tokens, sum(s.cost_usd) AS cost_usd
FROM avatar_gold_conversation_spend s
JOIN avatar_silver_agent a USING (agent_id)
GROUP BY a.code;

CREATE VIEW avatar_gold_spend_by_technology AS
SELECT tech.code AS technology, count(*) AS conversations,
       sum(s.total_tokens) AS tokens, sum(s.cost_usd) AS cost_usd
FROM avatar_gold_conversation_spend s
JOIN avatar_silver_technology tech USING (technology_id)
GROUP BY tech.code;

CREATE VIEW avatar_gold_feature_spend AS
SELECT f.code AS feature, a.code AS agent,
       count(*) AS invocations,
       sum(i.total_tokens) AS tokens,
       sum(i.cost_usd) AS cost_usd,
       count(*) FILTER (WHERE i.cost_usd IS NULL) AS unpriced_invocations
FROM avatar_silver_ai_invocation i
JOIN avatar_silver_feature f USING (feature_id)
JOIN avatar_silver_agent   a USING (agent_id)
JOIN avatar_silver_workflow w USING (workflow_id)
WHERE w.environment = 'production'
  AND w.record_kind = 'real'
  AND w.execution_mode = 'exe'
GROUP BY f.code, a.code;

CREATE VIEW avatar_gold_fortnightly_usage AS
SELECT DATE '2026-06-22'
         + (floor((c.started_at::date - DATE '2026-06-22') / 14.0)::int * 14)
                                            AS fortnight_start,
       count(*)                             AS conversations,
       count(DISTINCT c.user_id)            AS active_users,
       count(DISTINCT c.case_id)            AS cases_touched,
       count(*) FILTER (WHERE c.case_id IS NULL) AS conversations_without_case,
       count(*) FILTER (WHERE c.issue_time IS NOT NULL) AS with_issue_time
FROM avatar_silver_conversation c
WHERE c.environment = 'production'
  AND c.record_kind = 'real'
  AND c.execution_mode = 'exe'
GROUP BY 1 ORDER BY 1;

CREATE VIEW avatar_gold_data_quality AS
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
             SELECT 1 FROM avatar_silver_turn t
             WHERE t.conversation_id = c.conversation_id AND t.cost_usd IS NULL))
             / count(*), 1)                             AS pct_with_unpriced_turn
FROM avatar_silver_conversation c
WHERE c.environment = 'production'
  AND c.record_kind = 'real'
  AND c.execution_mode = 'exe'
GROUP BY 1 ORDER BY 1;

-- Developer activity is not hidden, only kept out of the production numbers.
-- Comparing the two modes is the point of collecting Developer data at all.
CREATE VIEW avatar_gold_usage_by_mode AS
SELECT c.execution_mode,
       c.record_kind,
       c.environment,
       count(*)                                 AS conversations,
       count(DISTINCT c.user_id)                AS users,
       coalesce(sum(s.total_tokens), 0)         AS total_tokens,
       sum(s.cost_usd)                          AS cost_usd
FROM avatar_silver_conversation c
LEFT JOIN LATERAL (
    SELECT sum(t.total_tokens) AS total_tokens, sum(t.cost_usd) AS cost_usd
    FROM avatar_silver_turn t WHERE t.conversation_id = c.conversation_id
) s ON true
GROUP BY c.execution_mode, c.record_kind, c.environment
ORDER BY c.execution_mode, c.record_kind, c.environment;

COMMIT;
