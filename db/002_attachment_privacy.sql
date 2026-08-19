-- Upgrade an already-initialised IntelAvatar telemetry database before its
-- first load. Raw attachment names remain in SMB; PostgreSQL receives only the
-- pseudonymous declared_name emitted by sync_share plus safe transfer metrics.

BEGIN;

ALTER TABLE avatar_silver_attachment_event
    ADD COLUMN log_family text NOT NULL DEFAULT '',
    ADD COLUMN byte_size bigint NULL,
    ADD COLUMN latency_ms integer NULL,
    ADD COLUMN attempt_count integer NULL,
    ADD CONSTRAINT avatar_silver_attachment_metrics_ck CHECK (
        (byte_size IS NULL OR byte_size >= 0)
        AND (latency_ms IS NULL OR latency_ms >= 0)
        AND (attempt_count IS NULL OR attempt_count >= 0));

COMMENT ON COLUMN avatar_silver_attachment_event.declared_name IS
'Pseudonymous SHA-256 key with safe suffix; raw attachment name remains in SMB';

COMMIT;
