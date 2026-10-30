-- Create (or upgrade) the Workbench portal service. Run by install.sh (values from config.env).
-- Endpoints: portal (public UI/API) and repo (private: curated packages for sessions).
USE ROLE <% OWNER_ROLE %>;
CREATE SERVICE IF NOT EXISTS <% APP_DATABASE %>.WORKBENCH.PORTAL
  IN COMPUTE POOL <% OBJECT_PREFIX %>_PORTAL_POOL
  FROM SPECIFICATION $$
spec:
  containers:
  - name: portal
    image: <% IMAGE_REPO_PATH %>/wb-portal:<% IMAGE_TAG %>
    env:
      WB_IDLE_MINUTES: "240"
      WB_DATABASE: "<% APP_DATABASE %>"
      WB_WAREHOUSE: "<% WAREHOUSE %>"
      WB_SESSION_POOL: "<% OBJECT_PREFIX %>_SESSION_POOL"
      WB_SESSION_EAI: "<% SESSION_EAI %>"
      WB_IMAGE_REPO: "<% IMAGE_REPO_PATH %>"
      WB_ROLE_PREFIX: "<% ACCESS_ROLE_PREFIX %>"
      WB_SESSION_TAG: "<% IMAGE_TAG %>"
      WB_JUPYTER_TAG: "<% IMAGE_TAG %>"
      WB_DEFAULT_DATABASE: "<% DEFAULT_DATABASE %>"
      WB_DEFAULT_SCHEMA: "<% DEFAULT_SCHEMA %>"
    resources:
      requests: {cpu: 0.5, memory: 1Gi}
      limits: {cpu: 1, memory: 3Gi}
    readinessProbe:
      port: 8080
      path: /healthz
  endpoints:
  - name: portal
    port: 8080
    public: true
  - name: repo
    port: 8081
    public: false
capabilities:
  securityContext:
    executeAsCaller: true
serviceRoles:
- name: app_user
  endpoints:
  - portal
$$
  EXTERNAL_ACCESS_INTEGRATIONS = (<% OBJECT_PREFIX %>_PKG_EGRESS)
  MIN_INSTANCES = 1 MAX_INSTANCES = 1;

-- Re-apply spec so a new image tag rolls out on re-deploy
ALTER SERVICE <% APP_DATABASE %>.WORKBENCH.PORTAL FROM SPECIFICATION $$
spec:
  containers:
  - name: portal
    image: <% IMAGE_REPO_PATH %>/wb-portal:<% IMAGE_TAG %>
    env:
      WB_IDLE_MINUTES: "240"
      WB_DATABASE: "<% APP_DATABASE %>"
      WB_WAREHOUSE: "<% WAREHOUSE %>"
      WB_SESSION_POOL: "<% OBJECT_PREFIX %>_SESSION_POOL"
      WB_SESSION_EAI: "<% SESSION_EAI %>"
      WB_IMAGE_REPO: "<% IMAGE_REPO_PATH %>"
      WB_ROLE_PREFIX: "<% ACCESS_ROLE_PREFIX %>"
      WB_SESSION_TAG: "<% IMAGE_TAG %>"
      WB_JUPYTER_TAG: "<% IMAGE_TAG %>"
      WB_DEFAULT_DATABASE: "<% DEFAULT_DATABASE %>"
      WB_DEFAULT_SCHEMA: "<% DEFAULT_SCHEMA %>"
    resources:
      requests: {cpu: 0.5, memory: 1Gi}
      limits: {cpu: 1, memory: 3Gi}
    readinessProbe:
      port: 8080
      path: /healthz
  endpoints:
  - name: portal
    port: 8080
    public: true
  - name: repo
    port: 8081
    public: false
capabilities:
  securityContext:
    executeAsCaller: true
serviceRoles:
- name: app_user
  endpoints:
  - portal
$$;
ALTER SERVICE <% APP_DATABASE %>.WORKBENCH.PORTAL SET EXTERNAL_ACCESS_INTEGRATIONS = (<% OBJECT_PREFIX %>_PKG_EGRESS);

-- Access: <prefix>_USER (and AUDITOR/ADMIN, which inherit it) can reach the portal
GRANT USAGE ON SCHEMA <% APP_DATABASE %>.WORKBENCH TO ROLE <% ACCESS_ROLE_PREFIX %>_USER;
GRANT SERVICE ROLE <% APP_DATABASE %>.WORKBENCH.PORTAL!app_user TO ROLE <% ACCESS_ROLE_PREFIX %>_USER;

USE ROLE ACCOUNTADMIN;
ALTER SERVICE <% APP_DATABASE %>.WORKBENCH.PORTAL SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = 3600;
SHOW ENDPOINTS IN SERVICE <% APP_DATABASE %>.WORKBENCH.PORTAL;
