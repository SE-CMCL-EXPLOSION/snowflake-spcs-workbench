-- Session egress: hosts sessions may reach directly (e.g. an S3 bucket). Run by install.sh only
-- when SESSION_EAI is set (SESSION_EGRESS_HOSTS non-empty). Never add package hosts here, or
-- users can install anything and bypass the curated repository.
USE ROLE ACCOUNTADMIN;
CREATE OR REPLACE NETWORK RULE <% APP_DATABASE %>.SPCS.<% OBJECT_PREFIX %>_SESSION_RULE
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = (<% SESSION_HOSTS_SQL %>);
CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS <% SESSION_EAI %>
  ALLOWED_NETWORK_RULES = (<% APP_DATABASE %>.SPCS.<% OBJECT_PREFIX %>_SESSION_RULE) ENABLED = TRUE;
GRANT USAGE ON INTEGRATION <% SESSION_EAI %> TO ROLE <% OWNER_ROLE %>;
