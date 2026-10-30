"""Snowflake helpers for Workbench Jupyter sessions (caller's rights).

    from sfwb import sf_connect, sf_session
    conn = sf_connect()      # snowflake.connector connection
    session = sf_session()   # Snowpark session
"""
import json
import os
import platform

TOKEN_FILE = "/run/sf/token"
# Defaults come from the portal (install/config.env); empty values are left to the user's defaults
DEFAULTS = {k: v for k, v in dict(warehouse=os.environ.get("WB_WAREHOUSE", ""),
                                  database=os.environ.get("WB_DEFAULT_DATABASE", ""),
                                  schema=os.environ.get("WB_DEFAULT_SCHEMA", "")).items() if v}
# Audit trail: every query is tagged with the workbench session (see WORKBENCH.SESSION_QUERY_HISTORY)
QUERY_TAG = json.dumps({"app": "spcs-workbench", "ide": "jupyter",
                        "session_id": os.environ.get("WB_SESSION_ID", ""),
                        "owner": os.environ.get("WB_OWNER", ""),
                        "python_version": platform.python_version()})


def _params(**kw):
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError("No Snowflake token yet - reload the session tab in the browser.")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    p = {"host": os.environ["SNOWFLAKE_HOST"], "account": os.environ["SNOWFLAKE_ACCOUNT"],
         "authenticator": "oauth", "token": token, **DEFAULTS,
         "session_parameters": {"QUERY_TAG": QUERY_TAG}}
    if os.environ.get("WB_ROLE"):  # role picked at session launch
        p["role"] = os.environ["WB_ROLE"]
    return {**p, **kw}


def sf_connect(**kw):
    import snowflake.connector
    return snowflake.connector.connect(**_params(**kw))


def sf_session(**kw):
    from snowflake.snowpark import Session
    return Session.builder.configs(_params(**kw)).create()
