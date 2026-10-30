-- SPCS Workbench: app schema, compute pools, stages and tables. Run by install.sh after prereqs.sql.
USE ROLE ACCOUNTADMIN;
CREATE SCHEMA IF NOT EXISTS <% APP_DATABASE %>.WORKBENCH;
GRANT OWNERSHIP ON SCHEMA <% APP_DATABASE %>.WORKBENCH TO ROLE <% OWNER_ROLE %> COPY CURRENT GRANTS;

-- Portal: tiny, always on. Sessions: autoscaling pool, one service per session.
CREATE COMPUTE POOL IF NOT EXISTS <% OBJECT_PREFIX %>_PORTAL_POOL
  MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_XS AUTO_SUSPEND_SECS = 3600;
CREATE COMPUTE POOL IF NOT EXISTS <% OBJECT_PREFIX %>_SESSION_POOL
  MIN_NODES = 1 MAX_NODES = <% SESSION_POOL_MAX_NODES %> INSTANCE_FAMILY = <% SESSION_POOL_FAMILY %> AUTO_SUSPEND_SECS = 900;
GRANT USAGE, MONITOR ON COMPUTE POOL <% OBJECT_PREFIX %>_PORTAL_POOL TO ROLE <% OWNER_ROLE %>;
GRANT USAGE, MONITOR ON COMPUTE POOL <% OBJECT_PREFIX %>_SESSION_POOL TO ROLE <% OWNER_ROLE %>;

USE ROLE <% OWNER_ROLE %>;
USE SCHEMA <% APP_DATABASE %>.WORKBENCH;

-- Per-user home directories (@USER_HOMES/<user>/ mounted at ~/work in every session)
CREATE STAGE IF NOT EXISTS USER_HOMES
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') DIRECTORY = (ENABLE = TRUE);

-- Session registry (portal is the only writer)
CREATE TABLE IF NOT EXISTS SESSIONS (
  ID            STRING PRIMARY KEY,
  OWNER         STRING NOT NULL,
  NAME          STRING,
  TYPE          STRING,          -- rstudio | jupyter
  SIZE          STRING,          -- small | medium | large
  FAST_DISK     BOOLEAN,
  SERVICE_NAME  STRING,
  STATE         STRING,          -- ACTIVE | DELETED (live status comes from SHOW SERVICES)
  SF_ROLE       STRING,          -- Snowflake role sf_connect() uses in this session
  R_VERSION     STRING,          -- RStudio only, e.g. 4.4.3
  CREATED_AT    TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  LAST_ACTIVE   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Admin-editable settings (JSON values). Unset keys fall back to app defaults.
--   allowed_session_roles: ["DATA_SCIENTIST", ...]  limit the per-session role dropdown
--   idle_minutes: 240      auto-suspend idle sessions
--   sizes: ["small","medium","large"]
CREATE TABLE IF NOT EXISTS SETTINGS (
  KEY STRING PRIMARY KEY, VALUE VARIANT, UPDATED_BY STRING,
  UPDATED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());

-- Every admin action (grants, forced suspend/delete, package decisions, settings)
CREATE TABLE IF NOT EXISTS ADMIN_AUDIT (
  TS TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(), ACTOR STRING, ACTION STRING, TARGET STRING, DETAIL VARIANT);

-- Curated packages: one row per package (ecosystem = cran | bioc | pypi)
--   STATUS: REQUESTED | APPROVED | DENIED ; MIRROR_*: result of the last sync into @PKG_REPO
CREATE TABLE IF NOT EXISTS PACKAGE_POLICY (
  ECOSYSTEM STRING, NAME STRING, STATUS STRING, VERSION_SPEC STRING, NOTE STRING,
  REQUESTED_BY STRING, REQUESTED_AT TIMESTAMP_LTZ, DECIDED_BY STRING, DECIDED_AT TIMESTAMP_LTZ,
  MIRROR_STATUS STRING, MIRROR_DETAIL VARIANT, MIRRORED_AT TIMESTAMP_LTZ,
  PRIMARY KEY (ECOSYSTEM, NAME));

-- Mirrored package files (mounted by the portal at /repo, served to sessions internally)
CREATE STAGE IF NOT EXISTS PKG_REPO
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') DIRECTORY = (ENABLE = TRUE);
