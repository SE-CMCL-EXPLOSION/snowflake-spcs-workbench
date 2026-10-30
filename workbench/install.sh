#!/bin/bash
# =============================================================================
# SPCS Workbench installer. Creates (or upgrades) everything in one go:
#   roles + access levels, warehouse, database, compute pools, egress integrations,
#   app tables + audit log, the 5 container images, and the portal service.
#
#   ./install.sh                     # uses config.env
#   ./install.sh my-config.env       # another settings file
#   ./install.sh --skip-images       # re-apply SQL + portal without rebuilding images
#   ./install.sh --connection NAME   # use this Snowflake CLI connection (overrides config.env)
#
# Safe to re-run: existing objects are kept, data is never deleted.
# Needs: Snowflake CLI (snow), Docker (running), a connection whose user can use ACCOUNTADMIN.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"

CONFIG=config.env
SKIP_IMAGES=0
CONNECTION_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-images) SKIP_IMAGES=1 ;;
    --connection|-c) [[ -n "${2:-}" ]] || { echo "--connection needs a name" >&2; exit 1; }; CONNECTION_ARG=$2; shift ;;
    --connection=*) CONNECTION_ARG=${1#*=} ;;
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) CONFIG=$1 ;;
  esac
  shift
done

STEP=0
TOTAL=8
step() { STEP=$((STEP + 1)); printf '\n\033[1m[%d/%d] %s\033[0m\n' "$STEP" "$TOTAL" "$*"; }
info() { echo "      $*"; }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
LOG=$(mktemp -t workbench-install.XXXXXX)
info "Detailed log: $LOG"

# ---------------------------------------------------------------- settings
step "Reading settings from $CONFIG"
[[ -f "$CONFIG" ]] || fail "Settings file '$CONFIG' not found."
# shellcheck disable=SC1090
source "$CONFIG"
[[ -n "$CONNECTION_ARG" ]] && SNOW_CONNECTION=$CONNECTION_ARG
for v in SNOW_CONNECTION APP_DATABASE WAREHOUSE OBJECT_PREFIX OWNER_ROLE ACCESS_ROLE_PREFIX \
         SESSION_POOL_FAMILY SESSION_POOL_MAX_NODES IMAGE_TAG; do
  [[ -n "${!v:-}" ]] || fail "$v is empty in $CONFIG"
done
FIRST_ADMIN=${FIRST_ADMIN:-}; USER_ACCESS_ROLES=${USER_ACCESS_ROLES:-}; DATA_DATABASES=${DATA_DATABASES:-}
DEFAULT_DATABASE=${DEFAULT_DATABASE:-}; DEFAULT_SCHEMA=${DEFAULT_SCHEMA:-}; SESSION_EGRESS_HOSTS=${SESSION_EGRESS_HOSTS:-}

IDENT='^[A-Za-z_][A-Za-z0-9_$]*$'
upper() { echo "$1" | tr '[:lower:]' '[:upper:]'; }
for v in APP_DATABASE WAREHOUSE OBJECT_PREFIX OWNER_ROLE ACCESS_ROLE_PREFIX SESSION_POOL_FAMILY DEFAULT_DATABASE DEFAULT_SCHEMA; do
  val=${!v}
  [[ -z "$val" || "$val" =~ $IDENT ]] || fail "$v='$val' is not a valid Snowflake name (letters, digits, _ only)"
  printf -v "$v" '%s' "$(upper "$val")"
done
for x in $DATA_DATABASES $USER_ACCESS_ROLES; do
  [[ "$x" =~ $IDENT ]] || fail "'$x' (in DATA_DATABASES or USER_ACCESS_ROLES) is not a valid Snowflake name"
done
DATA_DATABASES=$(upper "$DATA_DATABASES"); USER_ACCESS_ROLES=$(upper "$USER_ACCESS_ROLES")
[[ "$SESSION_POOL_MAX_NODES" =~ ^[1-9][0-9]*$ ]] || fail "SESSION_POOL_MAX_NODES must be a number"
[[ "$IMAGE_TAG" =~ ^[A-Za-z0-9._-]+$ ]] || fail "IMAGE_TAG may only contain letters, digits, . _ -"
for h in $SESSION_EGRESS_HOSTS; do
  [[ "$h" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || fail "SESSION_EGRESS_HOSTS entry '$h' is not a host name"
done
[[ -n "$DATA_DATABASES" ]] || info "Note: DATA_DATABASES is empty, so sessions can't query any data yet."

# ---------------------------------------------------------------- tools
step "Checking tools and the Snowflake connection"
command -v python3 >/dev/null || fail "python3 not found (macOS: run 'xcode-select --install'; Linux: install python3)."
command -v snow >/dev/null || fail "Snowflake CLI not found. Install it: https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation"
if [[ $SKIP_IMAGES == 0 ]]; then
  command -v docker >/dev/null || fail "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
  docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start Docker Desktop and re-run."
fi
AS_ADMIN=(--role ACCOUNTADMIN)   # every step runs as ACCOUNTADMIN; SQL switches roles where needed
sf_value() {  # sf_value "SELECT ... AS V"  -> first row's V
  snow sql -c "$SNOW_CONNECTION" "${AS_ADMIN[@]}" -q "$1" --format json 2>>"$LOG" \
    | python3 -c "import json,sys; r=json.load(sys.stdin); print(r[0]['V'] if r else '')" 2>>"$LOG"
}
ME=$(sf_value "SELECT CURRENT_USER() AS V")
[[ -n "$ME" ]] || fail "Could not connect with connection '$SNOW_CONNECTION' as ACCOUNTADMIN. Test it with:
    snow connection test -c $SNOW_CONNECTION
(the user needs the ACCOUNTADMIN role; see $LOG)"
ACCOUNT=$(sf_value "SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS V")
FIRST_ADMIN=${FIRST_ADMIN:-$ME}
[[ "$FIRST_ADMIN" =~ ^[A-Za-z0-9_.@$-]+$ ]] || fail "FIRST_ADMIN '$FIRST_ADMIN' is not a valid user name"
# Plain names are case-insensitive (unquoted); names with . @ - need quoting and exact case
if [[ "$FIRST_ADMIN" =~ $IDENT ]]; then FIRST_ADMIN_IDENT=$(upper "$FIRST_ADMIN"); else FIRST_ADMIN_IDENT="\"$FIRST_ADMIN\""; fi
snow sql -c "$SNOW_CONNECTION" "${AS_ADMIN[@]}" -q "DESCRIBE USER $FIRST_ADMIN_IDENT" >>"$LOG" 2>&1 \
  || fail "FIRST_ADMIN: Snowflake user $FIRST_ADMIN_IDENT not found (check SHOW USERS)"
info "Account: $ACCOUNT    connected as: $ME    first admin: $FIRST_ADMIN"

IMAGE_REPO_PATH="/$(echo "$APP_DATABASE" | tr '[:upper:]' '[:lower:]')/spcs/images"
SESSION_EAI=""
SESSION_HOSTS_SQL=""
if [[ -n "$SESSION_EGRESS_HOSTS" ]]; then
  SESSION_EAI="${OBJECT_PREFIX}_SESSION_EGRESS"
  for h in $SESSION_EGRESS_HOSTS; do
    [[ "$h" == *:* ]] || h="$h:443"
    SESSION_HOSTS_SQL+="${SESSION_HOSTS_SQL:+, }'$h'"
  done
fi
VARS=()
for v in APP_DATABASE WAREHOUSE OBJECT_PREFIX OWNER_ROLE ACCESS_ROLE_PREFIX SESSION_POOL_FAMILY \
         SESSION_POOL_MAX_NODES IMAGE_TAG FIRST_ADMIN_IDENT DEFAULT_DATABASE DEFAULT_SCHEMA \
         IMAGE_REPO_PATH SESSION_EAI SESSION_HOSTS_SQL; do
  VARS+=(-D "$v=${!v}")
done

run_sql_file() {  # file description
  info "$2"
  if ! snow sql -c "$SNOW_CONNECTION" "${AS_ADMIN[@]}" -f "$1" "${VARS[@]}" >>"$LOG" 2>&1; then
    tail -20 "$LOG" >&2
    fail "Step failed while running $1 (full output in $LOG)"
  fi
}
run_sql() {  # sql description
  local f; f=$(mktemp -t wbsql.XXXXXX)
  printf '%s\n' "$1" >"$f"
  run_sql_file "$f" "$2"
  rm -f "$f"
}

# ---------------------------------------------------------------- Snowflake objects
step "Creating roles, warehouse, database and integrations"
run_sql_file prereqs.sql "Owner role $OWNER_ROLE, access roles ${ACCESS_ROLE_PREFIX}_USER/_AUDITOR/_ADMIN, $WAREHOUSE, $APP_DATABASE"
if [[ -n "$SESSION_EAI" ]]; then
  run_sql_file session_egress.sql "Session egress $SESSION_EAI: $SESSION_EGRESS_HOSTS"
fi
GRANTS=""
for db in $DATA_DATABASES; do
  GRANTS+="GRANT ALL CALLER PRIVILEGES ON DATABASE $db TO ROLE $OWNER_ROLE;
"
  for kind in SCHEMAS TABLES VIEWS 'MATERIALIZED VIEWS' 'DYNAMIC TABLES' 'EXTERNAL TABLES' STAGES 'FILE FORMATS' FUNCTIONS; do
    GRANTS+="GRANT ALL INHERITED CALLER PRIVILEGES ON ALL $kind IN DATABASE $db TO ROLE $OWNER_ROLE;
"
  done
done
for r in $USER_ACCESS_ROLES; do
  GRANTS+="GRANT ROLE ${ACCESS_ROLE_PREFIX}_USER TO ROLE $r;
"
done
[[ -n "$GRANTS" ]] && run_sql "USE ROLE ACCOUNTADMIN;
$GRANTS" "Data access for: ${DATA_DATABASES:-(none)}; Workbench users via roles: ${USER_ACCESS_ROLES:-(none)}"

step "Creating the app schema, compute pools, tables and audit log"
run_sql_file setup.sql "Schema $APP_DATABASE.WORKBENCH, pools ${OBJECT_PREFIX}_PORTAL_POOL / ${OBJECT_PREFIX}_SESSION_POOL"
run_sql_file audit.sql "Audit views SESSION_QUERY_HISTORY / SESSION_OBJECT_ACCESS"
run_sql_file audit_retention.sql "Permanent query log SESSION_QUERY_LOG + hourly task"

# ---------------------------------------------------------------- images
step "Building and uploading container images (first time: 20-40 minutes)"
if [[ $SKIP_IMAGES == 1 ]]; then
  info "Skipped (--skip-images): using existing images tagged $IMAGE_TAG"
else
  REGISTRY=$(snow spcs image-registry url -c "$SNOW_CONNECTION" "${AS_ADMIN[@]}" 2>>"$LOG") || fail "Could not get the image registry URL (see $LOG)"
  REPO="$REGISTRY$IMAGE_REPO_PATH"
  registry_login() { snow spcs image-registry login -c "$SNOW_CONNECTION" "${AS_ADMIN[@]}" >>"$LOG" 2>&1; }
  registry_login || fail "Docker could not log in to the Snowflake image registry (see $LOG)"
  build_push() {  # image-ref context dockerfile [build args...]
    local ref=$1 ctx=$2 df=$3; shift 3
    local t0=$SECONDS
    info "building ${ref##*/} ..."
    docker build --platform linux/amd64 "$@" -t "$ref" -f "$df" "$ctx" >>"$LOG" 2>&1 \
      || { tail -30 "$LOG" >&2; fail "Building ${ref##*/} failed (full output in $LOG)"; }
    # The registry often times out on large layers, especially into a new repository. Finished
    # layers are kept, so each retry resumes where the last one stopped.
    for i in $(seq 1 30); do
      if docker push "$ref" >>"$LOG" 2>&1; then
        info "uploaded ${ref##*/} ($(( (SECONDS - t0) / 60 )) min)"; return 0
      fi
      info "upload interrupted (normal for big images), resuming ($i/30)"
      [[ $i == 3 ]] && info "tip: on a slow network, set Docker to upload one layer at a time (see README: Upload keeps failing)"
      sleep 5; registry_login
    done
    fail "Uploading ${ref##*/} failed (see $LOG)"
  }
  build_push "$REPO/wb-portal:$IMAGE_TAG" portal portal/Dockerfile
  build_push "$REPO/wb-jupyter:$IMAGE_TAG" images images/jupyter/Dockerfile
  for v in 4.4.3 4.3.3 4.5.1; do   # keep in sync with R_VERSIONS in portal/app.py
    build_push "$REPO/wb-rstudio:r$v-$IMAGE_TAG" images images/rstudio/Dockerfile --build-arg "R_VERSION=$v"
  done
fi

# ---------------------------------------------------------------- portal
step "Starting the portal service"
run_sql_file portal.sql "Service $APP_DATABASE.WORKBENCH.PORTAL ($IMAGE_TAG)"

step "Waiting for the portal to be ready"
URL=""
for i in $(seq 1 60); do
  STATUS=$(sf_value "SELECT PARSE_JSON(SYSTEM\$GET_SERVICE_STATUS('$APP_DATABASE.WORKBENCH.PORTAL'))[0]:status::STRING AS V")
  if [[ "$STATUS" == READY ]]; then
    URL=$(snow sql -c "$SNOW_CONNECTION" "${AS_ADMIN[@]}" -q "SHOW ENDPOINTS IN SERVICE $APP_DATABASE.WORKBENCH.PORTAL" --format json 2>>"$LOG" \
      | python3 -c "import json,sys; r=[e for e in json.load(sys.stdin) if e['name']=='portal']; u=r[0]['ingress_url'] if r else ''; print(u if '.' in u and ' ' not in u else '')" 2>>"$LOG")
    [[ -n "$URL" ]] && break
    info "running; waiting for the public URL ($((i * 15))s)"
  else
    info "status: ${STATUS:-starting} ($((i * 15))s)"
    [[ "$STATUS" == FAILED ]] && fail "Portal failed to start. Logs:
    SELECT SYSTEM\$GET_SERVICE_LOGS('$APP_DATABASE.WORKBENCH.PORTAL', 0, 'portal', 100);"
  fi
  sleep 15
done
[[ -n "$URL" ]] || fail "Portal didn't become ready within 15 minutes. Check:
    SELECT SYSTEM\$GET_SERVICE_STATUS('$APP_DATABASE.WORKBENCH.PORTAL');"

step "Done"
cat <<EOF

  Workbench portal:   https://$URL
  First admin:        $FIRST_ADMIN  (sign in with your normal Snowflake login)

  Next:
   - Open the portal, go to Admin > Users, and give people access (User / Auditor / Admin).
   - Packages: approve the R / Python packages users may install.
   - To upgrade later: change IMAGE_TAG in $CONFIG and run ./install.sh again.

EOF
rm -f "$LOG"
