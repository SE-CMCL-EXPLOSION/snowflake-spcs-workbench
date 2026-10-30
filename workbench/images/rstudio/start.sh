#!/bin/bash
# Session env -> R, fix mount ownership, start token proxy, then rocker's init
{
  echo "SNOWFLAKE_HOST=${SNOWFLAKE_HOST}"
  echo "SNOWFLAKE_ACCOUNT=${SNOWFLAKE_ACCOUNT}"
  echo "WB_OWNER=${WB_OWNER}"
  echo "WB_ROLE=${WB_ROLE}"
  echo "WB_SESSION_ID=${WB_SESSION_ID}"
  echo "WB_R_REPO=${WB_R_REPO}"
  echo "PIP_INDEX_URL=${PIP_INDEX_URL}"
  echo "PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}"
} >> /usr/local/lib/R/etc/Renviron.site
mkdir -p /home/rstudio/work
[ -d /home/rstudio/scratch ] && chown 1000:1000 /home/rstudio/scratch
python3 /opt/wb/proxy.py &
exec /init
