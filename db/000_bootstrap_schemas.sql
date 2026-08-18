-- =============================================================================
-- IntelAvatar telemetry — schema bootstrap (DBA runs this, once)
--
-- Run as a role that owns the database (or as a superuser), against
-- wirelesscustomerengineering. Everything after this file can be run by the
-- application role itself.
--
-- WHY THIS FILE EXISTS
-- --------------------
-- The application role (wirelesscustomerengi_so) has CREATE in `public` but
-- not on the database, so it cannot create schemas. There are two ways to
-- unblock it; this file is the smaller one.
--
--   A. GRANT CREATE ON DATABASE ... then REVOKE afterwards.
--      Works, but grants the role the right to create ANY schema for as long
--      as it is held, needs a second DBA action to take back, and has to be
--      repeated for any future migration that adds a schema.
--
--   B. This file: the DBA creates exactly three schemas and hands ownership
--      to the application role.
--      The role never receives database-level CREATE. It owns these three
--      schemas, so it can create and alter every table, view and index inside
--      them — including all future migrations — with no further DBA
--      involvement. The privilege is narrower AND the ongoing process is
--      lighter, which is unusual enough to be worth stating plainly.
--
-- Option B is recommended. Nothing outside these three schemas is touched.
-- =============================================================================

-- Adjust if the application role is named differently.
\set app_role wirelesscustomerengi_so

CREATE SCHEMA IF NOT EXISTS bronze AUTHORIZATION :app_role;
CREATE SCHEMA IF NOT EXISTS silver AUTHORIZATION :app_role;
CREATE SCHEMA IF NOT EXISTS gold   AUTHORIZATION :app_role;

-- Ownership already implies USAGE and CREATE for the owner; these are explicit
-- so the intent survives a later ownership change.
GRANT USAGE, CREATE ON SCHEMA bronze, silver, gold TO :app_role;

-- ---------------------------------------------------------------------------
-- Verification — the application role should report true for all three.
-- ---------------------------------------------------------------------------
SELECT nspname                                              AS schema,
       pg_get_userbyid(nspowner)                            AS owner,
       has_schema_privilege(:'app_role', nspname, 'CREATE') AS can_create
FROM   pg_namespace
WHERE  nspname IN ('bronze', 'silver', 'gold')
ORDER  BY nspname;

-- Once this returns three rows with can_create = true, the application role
-- can run 001_initial_schema.sql unaided. Database-level CREATE is never
-- required, and nothing needs to be revoked later.
