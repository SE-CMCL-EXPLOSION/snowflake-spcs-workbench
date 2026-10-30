-- SPCS Workbench prerequisites (account-level objects). Run by install.sh as ACCOUNTADMIN;
-- values in <% ... %> come from config.env.
USE ROLE ACCOUNTADMIN;

CREATE WAREHOUSE IF NOT EXISTS <% WAREHOUSE %> WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;
CREATE DATABASE IF NOT EXISTS <% APP_DATABASE %>;

-- Owner role for the portal + all session services
CREATE ROLE IF NOT EXISTS <% OWNER_ROLE %> COMMENT = 'SPCS Workbench: owns the portal and session services';
GRANT ROLE <% OWNER_ROLE %> TO ROLE SYSADMIN;
GRANT USAGE ON DATABASE <% APP_DATABASE %> TO ROLE <% OWNER_ROLE %>;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE <% OWNER_ROLE %>;
GRANT USAGE ON WAREHOUSE <% WAREHOUSE %> TO ROLE <% OWNER_ROLE %>;

-- Image repository (install.sh pushes the portal, RStudio and Jupyter images here)
CREATE SCHEMA IF NOT EXISTS <% APP_DATABASE %>.SPCS;
GRANT USAGE ON SCHEMA <% APP_DATABASE %>.SPCS TO ROLE <% OWNER_ROLE %>;
CREATE IMAGE REPOSITORY IF NOT EXISTS <% APP_DATABASE %>.SPCS.IMAGES;
GRANT READ, WRITE ON IMAGE REPOSITORY <% APP_DATABASE %>.SPCS.IMAGES TO ROLE <% OWNER_ROLE %>;

-- Caller grants: the ceiling of what sessions may do as a user (the user's own role still applies).
-- Per data database grants are added by install.sh from DATA_DATABASES.
GRANT ALL CALLER PRIVILEGES ON WAREHOUSE <% WAREHOUSE %> TO ROLE <% OWNER_ROLE %>;

-- Workbench access levels. Owned by the app role so Workbench admins can grant/revoke them
-- from the portal (Admin > Users) without ACCOUNTADMIN. ADMIN > AUDITOR > USER.
--   _USER     use the portal, own sessions, request packages
--   _AUDITOR  + read-only view of all sessions, audit log, users, package queue
--   _ADMIN    + manage users, any session, packages, settings
CREATE ROLE IF NOT EXISTS <% ACCESS_ROLE_PREFIX %>_USER    COMMENT = 'SPCS Workbench: use the app';
CREATE ROLE IF NOT EXISTS <% ACCESS_ROLE_PREFIX %>_AUDITOR COMMENT = 'SPCS Workbench: read-only administration';
CREATE ROLE IF NOT EXISTS <% ACCESS_ROLE_PREFIX %>_ADMIN   COMMENT = 'SPCS Workbench: full administration';
GRANT ROLE <% ACCESS_ROLE_PREFIX %>_USER TO ROLE <% ACCESS_ROLE_PREFIX %>_AUDITOR;
GRANT ROLE <% ACCESS_ROLE_PREFIX %>_AUDITOR TO ROLE <% ACCESS_ROLE_PREFIX %>_ADMIN;
GRANT OWNERSHIP ON ROLE <% ACCESS_ROLE_PREFIX %>_USER TO ROLE <% OWNER_ROLE %> COPY CURRENT GRANTS;
GRANT OWNERSHIP ON ROLE <% ACCESS_ROLE_PREFIX %>_AUDITOR TO ROLE <% OWNER_ROLE %> COPY CURRENT GRANTS;
GRANT OWNERSHIP ON ROLE <% ACCESS_ROLE_PREFIX %>_ADMIN TO ROLE <% OWNER_ROLE %> COPY CURRENT GRANTS;
GRANT USAGE ON DATABASE <% APP_DATABASE %> TO ROLE <% ACCESS_ROLE_PREFIX %>_USER;
GRANT USAGE ON WAREHOUSE <% WAREHOUSE %> TO ROLE <% ACCESS_ROLE_PREFIX %>_USER;
-- First admin (add more in the portal: Admin > Users)
GRANT ROLE <% ACCESS_ROLE_PREFIX %>_ADMIN TO USER <% FIRST_ADMIN_IDENT %>;

-- Curated packages: the PORTAL (not sessions) reaches package sources, mirrors approved
-- packages, and serves them to sessions internally.
CREATE OR REPLACE NETWORK RULE <% APP_DATABASE %>.SPCS.<% OBJECT_PREFIX %>_PKG_RULE
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('p3m.dev:443', 'rspm-sync.rstudio.com:443', 'pypi.org:443', 'files.pythonhosted.org:443');  -- rspm-sync: Posit binary CDN
CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS <% OBJECT_PREFIX %>_PKG_EGRESS
  ALLOWED_NETWORK_RULES = (<% APP_DATABASE %>.SPCS.<% OBJECT_PREFIX %>_PKG_RULE) ENABLED = TRUE;
GRANT USAGE ON INTEGRATION <% OBJECT_PREFIX %>_PKG_EGRESS TO ROLE <% OWNER_ROLE %>;
-- Session egress (customer data hosts only; never package hosts) is created by install.sh
-- from SESSION_EGRESS_HOSTS, see session_egress.sql.
