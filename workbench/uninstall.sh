#!/bin/bash
# =============================================================================
# SPCS Workbench uninstaller.
#   ./uninstall.sh                  # stop and remove the app; KEEP user files, audit log, images
#   ./uninstall.sh --purge          # also delete schema WORKBENCH (user files, audit log, settings)
#                                     and schema SPCS (images)
#   ./uninstall.sh my-config.env [--purge]
#   ./uninstall.sh --connection NAME  # use this Snowflake CLI connection (overrides config.env)
# The warehouse, APP_DATABASE itself and your data databases are never dropped.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
CONFIG=config.env
PURGE=0
CONNECTION_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --connection|-c) [[ -n "${2:-}" ]] || { echo "--connection needs a name" >&2; exit 1; }; CONNECTION_ARG=$2; shift ;;
    --connection=*) CONNECTION_ARG=${1#*=} ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) CONFIG=$1 ;;
  esac
  shift
done
[[ -f "$CONFIG" ]] || { echo "Settings file '$CONFIG' not found." >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG"
[[ -n "$CONNECTION_ARG" ]] && SNOW_CONNECTION=$CONNECTION_ARG
[[ -n "${SNOW_CONNECTION:-}" ]] || { echo "SNOW_CONNECTION is empty in $CONFIG (or pass --connection NAME)" >&2; exit 1; }
upper() { echo "$1" | tr '[:lower:]' '[:upper:]'; }
APP_DATABASE=$(upper "$APP_DATABASE"); OBJECT_PREFIX=$(upper "$OBJECT_PREFIX")
OWNER_ROLE=$(upper "$OWNER_ROLE"); ACCESS_ROLE_PREFIX=$(upper "$ACCESS_ROLE_PREFIX")
for v in "$APP_DATABASE" "$OBJECT_PREFIX" "$OWNER_ROLE" "$ACCESS_ROLE_PREFIX"; do
  [[ "$v" =~ ^[A-Z_][A-Z0-9_$]*$ ]] || { echo "Invalid name '$v' in $CONFIG" >&2; exit 1; }
done
SQL=(snow sql -c "$SNOW_CONNECTION" --role ACCOUNTADMIN)

echo "This removes the SPCS Workbench in database $APP_DATABASE:"
echo "  - the portal and every RStudio/Jupyter session service (running work is lost)"
echo "  - compute pools ${OBJECT_PREFIX}_PORTAL_POOL / ${OBJECT_PREFIX}_SESSION_POOL"
echo "  - integrations ${OBJECT_PREFIX}_PKG_EGRESS / ${OBJECT_PREFIX}_SESSION_EGRESS"
echo "  - roles $OWNER_ROLE, ${ACCESS_ROLE_PREFIX}_USER / _AUDITOR / _ADMIN"
if [[ $PURGE == 1 ]]; then
  echo "  - PURGE: schemas $APP_DATABASE.WORKBENCH (user files, audit log, settings) and $APP_DATABASE.SPCS (images)"
else
  echo "  Kept: $APP_DATABASE.WORKBENCH (user files in @USER_HOMES, audit log) and $APP_DATABASE.SPCS (images)."
fi
read -r -p "Type the database name ($APP_DATABASE) to continue: " answer
[[ "$(upper "$answer")" == "$APP_DATABASE" ]] || { echo "Cancelled."; exit 1; }

# Session services are named WB_<id>; drop them before the pool
SERVICES=$("${SQL[@]}" -q "SHOW SERVICES IN SCHEMA $APP_DATABASE.WORKBENCH" --format json 2>/dev/null \
  | python3 -c "import json,sys; print(' '.join(r['name'] for r in json.load(sys.stdin)))" 2>/dev/null || true)
STMTS="USE ROLE ACCOUNTADMIN;
"
for s in $SERVICES; do STMTS+="DROP SERVICE IF EXISTS $APP_DATABASE.WORKBENCH.$s;
"; done
[[ -n "$SERVICES" ]] && STMTS+="UPDATE $APP_DATABASE.WORKBENCH.SESSIONS SET STATE = 'DELETED' WHERE STATE = 'ACTIVE';
"
STMTS+="ALTER TASK IF EXISTS $APP_DATABASE.WORKBENCH.ARCHIVE_SESSION_QUERIES SUSPEND;
ALTER COMPUTE POOL IF EXISTS ${OBJECT_PREFIX}_SESSION_POOL STOP ALL;
ALTER COMPUTE POOL IF EXISTS ${OBJECT_PREFIX}_PORTAL_POOL STOP ALL;
DROP COMPUTE POOL IF EXISTS ${OBJECT_PREFIX}_SESSION_POOL;
DROP COMPUTE POOL IF EXISTS ${OBJECT_PREFIX}_PORTAL_POOL;
DROP INTEGRATION IF EXISTS ${OBJECT_PREFIX}_PKG_EGRESS;
DROP INTEGRATION IF EXISTS ${OBJECT_PREFIX}_SESSION_EGRESS;
DROP NETWORK RULE IF EXISTS $APP_DATABASE.SPCS.${OBJECT_PREFIX}_PKG_RULE;
DROP NETWORK RULE IF EXISTS $APP_DATABASE.SPCS.${OBJECT_PREFIX}_SESSION_RULE;
"
if [[ $PURGE == 1 ]]; then
  STMTS+="DROP SCHEMA IF EXISTS $APP_DATABASE.WORKBENCH;
DROP SCHEMA IF EXISTS $APP_DATABASE.SPCS;
"
fi
# Objects owned by dropped roles pass to ACCOUNTADMIN (the role running DROP ROLE)
STMTS+="DROP ROLE IF EXISTS ${ACCESS_ROLE_PREFIX}_ADMIN;
DROP ROLE IF EXISTS ${ACCESS_ROLE_PREFIX}_AUDITOR;
DROP ROLE IF EXISTS ${ACCESS_ROLE_PREFIX}_USER;
DROP ROLE IF EXISTS $OWNER_ROLE;
"
F=$(mktemp -t wbuninstall.XXXXXX)
printf '%s' "$STMTS" >"$F"
echo "Removing ..."
if "${SQL[@]}" -f "$F" >"$F.log" 2>&1; then
  echo "Done. The Workbench has been removed."
else
  tail -20 "$F.log" >&2; echo "Some steps failed (full output: $F.log). Re-running is safe." >&2; exit 1
fi
rm -f "$F" "$F.log"
