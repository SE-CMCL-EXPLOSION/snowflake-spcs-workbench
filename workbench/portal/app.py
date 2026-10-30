"""SPCS Workbench portal.

Single public endpoint (executeAsCaller). Identifies the user from SPCS ingress headers,
manages one SPCS service per IDE session, and sends the owner to that session's own
endpoint (/s/<id>/ -> session ingress URL). Each session enforces its owner itself.

Access levels come from Snowflake roles (owned by the app role, granted from Admin > Users):
WORKBENCH_USER < WORKBENCH_AUDITOR (read-only admin) < WORKBENCH_ADMIN.
A second, private endpoint (:8081) serves the curated package repository to sessions.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import aiohttp
import snowflake.connector
from aiohttp import web

import pkgrepo

log = logging.getLogger("portal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DB, SCHEMA = os.environ.get("WB_DATABASE", "WORKBENCH_APP"), os.environ.get("WB_SCHEMA", "WORKBENCH")
FQ = f"{DB}.{SCHEMA}"
POOL = os.environ.get("WB_SESSION_POOL", "WORKBENCH_SESSION_POOL")
# Session egress: customer data hosts only (no package hosts; packages come from the curated repo)
SESSION_EAI = os.environ.get("WB_SESSION_EAI", "")
WAREHOUSE = os.environ.get("WB_WAREHOUSE", "WORKBENCH_WH")
IMAGE_REPO = os.environ.get("WB_IMAGE_REPO", "/workbench_app/spcs/images")
IDLE_MINUTES = int(os.environ.get("WB_IDLE_MINUTES", "240"))
DEV_USER = os.environ.get("WB_DEV_USER")  # local testing only; never set in SPCS
SVC_TOKEN = "/snowflake/session/token"
REPO_URL = os.environ.get("WB_REPO_URL", "http://portal:8081")  # private endpoint, same schema
REPO_STAGE = f"@{FQ}.PKG_REPO"   # durable copy of the curated repo (served from local disk)

IDES = {
    "rstudio": {"image": "wb-rstudio", "home": "/home/rstudio", "gid": 1000},
    "jupyter": {"image": "wb-jupyter", "home": "/home/jovyan", "gid": 100},
}
R_VERSIONS = ["4.5.1", "4.4.3", "4.3.3"]          # one wb-rstudio:r<ver> image each
DEFAULT_R = "4.4.3"
SESSION_TAG = os.environ.get("WB_SESSION_TAG", "v4")    # wb-rstudio:r<ver>-<tag>
JUPYTER_TAG = os.environ.get("WB_JUPYTER_TAG", "v4")
# Access-level roles <prefix>_USER / _AUDITOR / _ADMIN (install/config.env: ACCESS_ROLE_PREFIX)
ROLE_PREFIX = os.environ.get("WB_ROLE_PREFIX", "WORKBENCH").upper()
LEVEL_ROLES = {"admin": f"{ROLE_PREFIX}_ADMIN", "auditor": f"{ROLE_PREFIX}_AUDITOR", "user": f"{ROLE_PREFIX}_USER"}
LEVEL_ROLE_SET = set(LEVEL_ROLES.values())
# Defaults for sf_connect() in sessions (install/config.env: DEFAULT_DATABASE / DEFAULT_SCHEMA)
DEFAULT_DATABASE = os.environ.get("WB_DEFAULT_DATABASE", "")
DEFAULT_SCHEMA = os.environ.get("WB_DEFAULT_SCHEMA", "")
LEVEL_RANK = {"none": 0, "user": 1, "auditor": 2, "admin": 3}
# Roles that may reach session endpoints (ingress); ownership is enforced inside each session
ACCESS_ROLES = [r for r in os.environ.get("WB_ACCESS_ROLES", LEVEL_ROLES["user"]).split(",") if r]
ROLE_RE = re.compile(r"^[A-Za-z0-9_$]{1,255}$")
GRANTEE_RE = re.compile(r"^[A-Za-z0-9_$.@-]{1,255}$")
SIZES = {
    "small": {"cpu": 1, "mem": "4Gi"},
    "medium": {"cpu": 2, "mem": "8Gi"},
    "large": {"cpu": 4, "mem": "16Gi"},
}
ID_RE = re.compile(r"^[a-f0-9]{10}$")
DEFAULT_SETTINGS = {"allowed_session_roles": [], "idle_minutes": IDLE_MINUTES, "sizes": list(SIZES)}

_last_active: dict[str, float] = {}
_status_cache = {"at": 0.0, "rows": {}}
_settings_cache = {"at": 0.0, "val": dict(DEFAULT_SETTINGS)}


# ---------------------------------------------------------------- Snowflake
class SF:
    """Owner-role connection via the service's own OAuth token (portal-only DDL/DML)."""

    def __init__(self):
        self.conn = None

    def _connect(self):
        if os.path.exists(SVC_TOKEN):
            args = dict(host=os.environ["SNOWFLAKE_HOST"], account=os.environ["SNOWFLAKE_ACCOUNT"],
                        authenticator="oauth", token=Path(SVC_TOKEN).read_text().strip())
        else:  # local dev: use a named connection from connections.toml
            args = dict(connection_name=os.environ.get("WB_DEV_CONNECTION", "ci"))
        self.conn = snowflake.connector.connect(**args, warehouse=WAREHOUSE, database=DB, schema=SCHEMA,
                                                session_parameters={"QUERY_TAG": "spcs-workbench-portal"})

    def run(self, sql, params=None):
        for attempt in (1, 2):
            try:
                if self.conn is None or self.conn.is_closed():
                    self._connect()
                cur = self.conn.cursor(snowflake.connector.DictCursor)
                cur.execute(sql, params)
                return cur.fetchall()
            except snowflake.connector.errors.ProgrammingError as e:
                # token expiry -> reconnect once; real SQL errors bubble up
                if attempt == 1 and e.errno in (390114, 390303, 390318):
                    self.conn = None
                    continue
                raise


sf = SF()


async def q(sql, params=None):
    return await asyncio.to_thread(sf.run, sql, params)


def _iso(v):
    return v.isoformat() if v else None


def _variant(v):
    return json.loads(v) if isinstance(v, str) else v


# ---------------------------------------------------------------- identity + access levels
def current_user(request) -> str:
    user = request.headers.get("Sf-Context-Current-User") or DEV_USER
    if not user:
        raise web.HTTPUnauthorized(text="No Snowflake user on request")
    return user.upper()


def caller_token(request):
    """<service token>.<caller token> -> lets portal code act as the signed-in user."""
    caller = request.headers.get("Sf-Context-Current-User-Token")
    if not caller or not os.path.exists(SVC_TOKEN):
        return None
    return Path(SVC_TOKEN).read_text().strip() + "." + caller


def path_safe(user: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", user)


def quote_ident(name: str) -> str:
    if not GRANTEE_RE.match(name):
        raise web.HTTPBadRequest(text="Invalid name")
    return '"' + name + '"'


def _caller_roles(token):
    conn = snowflake.connector.connect(host=os.environ["SNOWFLAKE_HOST"], account=os.environ["SNOWFLAKE_ACCOUNT"],
                                       authenticator="oauth", token=token)
    try:
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_AVAILABLE_ROLES(), CURRENT_ROLE()")
        roles, current = cur.fetchone()
        return sorted(r for r in json.loads(roles) if r != "PUBLIC"), current
    finally:
        conn.close()


_roles_cache: dict[str, tuple[float, list, str]] = {}


async def user_roles(request):
    """All roles the signed-in user holds (incl. inherited), looked up AS the user."""
    user = current_user(request)
    hit = _roles_cache.get(user)
    if hit and time.time() - hit[0] < 300:
        return hit[1], hit[2]
    tok = caller_token(request)
    if not tok:  # local dev
        return [LEVEL_ROLES[os.environ.get("WB_DEV_LEVEL", "admin")]], None
    roles, current = await asyncio.to_thread(_caller_roles, tok)
    _roles_cache[user] = (time.time(), roles, current)
    return roles, current


async def user_level(request) -> str:
    roles, _ = await user_roles(request)
    for lvl in ("admin", "auditor", "user"):
        if LEVEL_ROLES[lvl] in roles:
            return lvl
    return "none"


async def require(request, need: str) -> str:
    lvl = await user_level(request)
    if LEVEL_RANK[lvl] < LEVEL_RANK[need]:
        raise web.HTTPForbidden(text=f"Requires Workbench {need} access")
    return lvl


async def session_roles(request):
    """Roles offered in the per-session role dropdown (data roles only, filtered by settings)."""
    roles, current = await user_roles(request)
    roles = [r for r in roles if r not in LEVEL_ROLE_SET]
    allowed = (await get_settings())["allowed_session_roles"]
    if allowed:
        roles = [r for r in roles if r in allowed]
    return roles, (current if current in roles else (roles[0] if roles else None))


async def audit(request, action, target, detail=None):
    await q(f"INSERT INTO {FQ}.ADMIN_AUDIT (ACTOR, ACTION, TARGET, DETAIL) SELECT %s, %s, %s, PARSE_JSON(%s)",
            (current_user(request), action, target, json.dumps(detail or {})))


# ---------------------------------------------------------------- settings
async def get_settings(force=False):
    if force or time.time() - _settings_cache["at"] > 30:
        val = dict(DEFAULT_SETTINGS)
        for r in await q(f"SELECT KEY, VALUE FROM {FQ}.SETTINGS"):
            if r["KEY"] in val:
                val[r["KEY"]] = _variant(r["VALUE"])
        _settings_cache.update(at=time.time(), val=val)
    return _settings_cache["val"]


# ---------------------------------------------------------------- sessions
async def live_status(force=False):
    """name -> {status, dns_name} for all workbench services (cached ~5s)."""
    if force or time.time() - _status_cache["at"] > 5:
        rows = await q(f"SHOW SERVICES LIKE 'WB\\\\_%' IN SCHEMA {FQ}")
        _status_cache["rows"] = {r["name"]: {"status": r["status"], "dns": r["dns_name"]} for r in rows}
        _status_cache["at"] = time.time()
    return _status_cache["rows"]


async def get_session(sid, user=None):
    if not ID_RE.match(sid):
        raise web.HTTPNotFound()
    rows = await q(f"SELECT * FROM {FQ}.SESSIONS WHERE ID = %s AND STATE = 'ACTIVE'", (sid,))
    if not rows:
        raise web.HTTPNotFound()
    s = rows[0]
    if user is not None and s["OWNER"] != user:
        raise web.HTTPForbidden(text="Not your session")
    return s


def session_json(r, live):
    return {"id": r["ID"], "owner": r["OWNER"], "name": r["NAME"], "type": r["TYPE"], "size": r["SIZE"],
            "fast_disk": r["FAST_DISK"], "status": live.get(r["SERVICE_NAME"], {}).get("status", "UNKNOWN"),
            "role": r["SF_ROLE"], "r_version": r["R_VERSION"],
            "created_at": _iso(r["CREATED_AT"]), "last_active": _iso(r["LAST_ACTIVE"])}


def render_spec(sid, owner, ide, size, fast_disk, role, r_version):
    cfg, res = IDES[ide], SIZES[size]
    tag = f"r{r_version}-{SESSION_TAG}" if ide == "rstudio" else JUPYTER_TAG
    mounts = [{"name": "work", "mountPath": f"{cfg['home']}/work"}]
    volumes = [{"name": "work", "source": "stage", "uid": 1000, "gid": cfg["gid"],
                "stageConfig": {"name": f"@{FQ}.USER_HOMES/{path_safe(owner)}/"}}]
    if fast_disk:
        mounts.append({"name": "scratch", "mountPath": f"{cfg['home']}/scratch"})
        volumes.append({"name": "scratch", "source": "block", "size": "20Gi",
                        "blockConfig": {"snapshotOnDelete": False}})
    env = {"WB_OWNER": owner, "WB_SESSION_ID": sid, "WB_ROLE": role or "",
           "WB_SESSIONS_TABLE": f"{FQ}.SESSIONS", "WB_WAREHOUSE": WAREHOUSE,
           "WB_DEFAULT_DATABASE": DEFAULT_DATABASE, "WB_DEFAULT_SCHEMA": DEFAULT_SCHEMA,
           # Curated package repo (portal private endpoint); public package hosts are not reachable
           "PIP_INDEX_URL": f"{REPO_URL}/pypi/simple/", "PIP_TRUSTED_HOST": REPO_URL.split("//")[1].split(":")[0],
           "WB_PORTAL_PACKAGES": "Workbench portal > Packages"}
    if ide == "rstudio":
        env["WB_R_REPO"] = f"{REPO_URL}/r/{r_version}"
    spec = {"spec": {
        "containers": [{
            "name": "ide",
            "image": f"{IMAGE_REPO}/{cfg['image']}:{tag}",
            "env": env,
            "resources": {"requests": {"cpu": res["cpu"], "memory": res["mem"]},
                          "limits": {"cpu": res["cpu"], "memory": res["mem"]}},
            "readinessProbe": {"port": 8080, "path": "/healthz"},
            "volumeMounts": mounts,
        }],
        # Own public endpoint: SPCS caller tokens only work in the service that received them.
        # The session proxy still rejects anyone but WB_OWNER.
        "endpoints": [{"name": "ide", "port": 8080, "public": True}],
        "volumes": volumes,
    },
        "capabilities": {"securityContext": {"executeAsCaller": True}},
        "serviceRoles": [{"name": "app_user", "endpoints": ["ide"]}],
    }
    return json.dumps(spec)  # JSON is valid YAML; no user free-text goes in here


async def api_me(request):
    lvl = await require(request, "user")
    roles, current = await session_roles(request)
    st = await get_settings()
    return web.json_response({"user": current_user(request), "level": lvl, "roles": roles, "default_role": current,
                              "r_versions": R_VERSIONS, "default_r": DEFAULT_R,
                              "sizes": [s for s in SIZES if s in st["sizes"]], "level_roles": LEVEL_ROLES})


async def api_list(request):
    await require(request, "user")
    user = current_user(request)
    rows = await q(f"SELECT * FROM {FQ}.SESSIONS WHERE OWNER = %s AND STATE = 'ACTIVE' ORDER BY CREATED_AT DESC",
                   (user,))
    live = await live_status()
    return web.json_response([session_json(r, live) for r in rows])


async def api_create(request):
    await require(request, "user")
    user = current_user(request)
    body = await request.json()
    ide, size = body.get("type"), body.get("size", "small")
    if ide not in IDES or size not in SIZES or size not in (await get_settings())["sizes"]:
        raise web.HTTPBadRequest(text="bad type/size")
    name = str(body.get("name") or f"{ide} session")[:60]
    fast_disk = bool(body.get("fast_disk"))
    r_version = body.get("r_version") or DEFAULT_R
    if ide == "rstudio" and r_version not in R_VERSIONS:
        raise web.HTTPBadRequest(text="bad R version")
    if ide != "rstudio":
        r_version = None
    role = (body.get("role") or "").upper() or None
    if role:
        roles, _ = await session_roles(request)
        if not ROLE_RE.match(role) or role not in roles:
            raise web.HTTPForbidden(text=f"Role {role} is not available for sessions")
    sid = secrets.token_hex(5)
    svc = f"WB_{sid.upper()}"
    spec = render_spec(sid, user, ide, size, fast_disk, role, r_version)
    await q(f"INSERT INTO {FQ}.SESSIONS (ID, OWNER, NAME, TYPE, SIZE, FAST_DISK, SERVICE_NAME, STATE, SF_ROLE, R_VERSION) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s)",
            (sid, user, name, ide, size, fast_disk, svc, role, r_version))
    eai = f"EXTERNAL_ACCESS_INTEGRATIONS = ({SESSION_EAI}) " if SESSION_EAI else ""
    try:
        await q(f"CREATE SERVICE {FQ}.{svc} IN COMPUTE POOL {POOL} FROM SPECIFICATION $${spec}$$ "
                f"{eai}MIN_INSTANCES = 1 MAX_INSTANCES = 1 "
                f"COMMENT = 'spcs-workbench session for {path_safe(user)}'")
    except Exception as e:
        await q(f"UPDATE {FQ}.SESSIONS SET STATE = 'DELETED' WHERE ID = %s", (sid,))
        log.exception("create failed")
        raise web.HTTPInternalServerError(text=f"Could not create session: {e}")
    for r in ACCESS_ROLES:
        await q(f"GRANT SERVICE ROLE {FQ}.{svc}!app_user TO ROLE {r}")
    try:  # caller token default is short-lived; R/Python sessions are long
        await q(f"ALTER SERVICE {FQ}.{svc} SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = 3600")
    except Exception as e:
        log.warning("could not extend caller token validity: %s", e)
    _last_active[sid] = time.time()
    await live_status(force=True)
    log.info("created %s (%s/%s) for %s", svc, ide, size, user)
    return web.json_response({"id": sid})


async def _session_action(s, action):
    svc = f"{FQ}.{s['SERVICE_NAME']}"
    if action == "suspend":
        await q(f"ALTER SERVICE IF EXISTS {svc} SUSPEND")
    elif action == "resume":
        await q(f"ALTER SERVICE IF EXISTS {svc} RESUME")
        _last_active[s["ID"]] = time.time()
    elif action == "delete":
        await q(f"DROP SERVICE IF EXISTS {svc} FORCE")
        await q(f"UPDATE {FQ}.SESSIONS SET STATE = 'DELETED' WHERE ID = %s", (s["ID"],))
        _last_active.pop(s["ID"], None)
    else:
        raise web.HTTPNotFound()
    await live_status(force=True)


async def api_action(request):
    await require(request, "user")
    s = await get_session(request.match_info["sid"], current_user(request))
    await _session_action(s, request.match_info["action"])
    return web.json_response({"ok": True})


async def api_delete(request):
    await require(request, "user")
    s = await get_session(request.match_info["sid"], current_user(request))
    await _session_action(s, "delete")
    return web.json_response({"ok": True})


def _caller_session_queries(token, sid):
    conn = snowflake.connector.connect(host=os.environ["SNOWFLAKE_HOST"], account=os.environ["SNOWFLAKE_ACCOUNT"],
                                       authenticator="oauth", token=token, warehouse=WAREHOUSE)
    try:
        cur = conn.cursor(snowflake.connector.DictCursor)
        cur.execute(
            f"SELECT QUERY_ID, START_TIME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ROWS_PRODUCED, "
            f"TOTAL_ELAPSED_TIME, LEFT(QUERY_TEXT, 400) AS QUERY_TEXT "
            f"FROM TABLE({DB}.INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(USER_NAME => CURRENT_USER(), RESULT_LIMIT => 2000)) "
            f"WHERE TRY_PARSE_JSON(QUERY_TAG):session_id::STRING = %s AND QUERY_TYPE <> 'ALTER_SESSION' "
            f"ORDER BY START_TIME DESC LIMIT 100", (sid,))
        return cur.fetchall()
    finally:
        conn.close()


async def api_queries(request):
    """Recent queries tagged with this session, read AS the user (real time; last 7 days)."""
    await require(request, "user")
    s = await get_session(request.match_info["sid"], current_user(request))
    tok = caller_token(request)
    if not tok:
        raise web.HTTPServiceUnavailable(text="No caller token")
    rows = await asyncio.to_thread(_caller_session_queries, tok, s["ID"])
    # Merge the permanent archive (older than 7 days / survives deletion)
    seen = {r["QUERY_ID"] for r in rows}
    archived = await q(
        f"SELECT QUERY_ID, START_TIME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ROWS_PRODUCED, "
        f"TOTAL_ELAPSED_TIME, LEFT(QUERY_TEXT, 400) AS QUERY_TEXT FROM {FQ}.SESSION_QUERY_LOG "
        f"WHERE SESSION_ID = %s AND QUERY_TYPE <> 'ALTER_SESSION' ORDER BY START_TIME DESC LIMIT 100", (s["ID"],))
    rows += [r for r in archived if r["QUERY_ID"] not in seen]
    rows.sort(key=lambda r: r["START_TIME"], reverse=True)
    return web.json_response([{**r, "START_TIME": r["START_TIME"].isoformat()} for r in rows[:200]])


# ---------------------------------------------------------------- admin: users
async def _level_grants():
    """[{name, type, level}] for direct grants of the WORKBENCH_* roles (highest level wins)."""
    out = {}
    for lvl in ("user", "auditor", "admin"):
        for g in await q(f"SHOW GRANTS OF ROLE {LEVEL_ROLES[lvl]}"):
            if g["granted_to"] == "ROLE" and g["grantee_name"] in LEVEL_ROLE_SET:
                continue  # role hierarchy itself
            key = (g["granted_to"], g["grantee_name"])
            out[key] = {"name": g["grantee_name"], "type": g["granted_to"], "level": lvl,
                        "granted_by": g.get("granted_by"), "granted_on": _iso(g.get("created_on"))}
    return sorted(out.values(), key=lambda r: (-LEVEL_RANK[r["level"]], r["type"], r["name"]))


async def admin_users(request):
    await require(request, "auditor")
    return web.json_response(await _level_grants())


async def admin_set_user(request):
    await require(request, "admin")
    body = await request.json()
    name = str(body.get("name") or "").strip().upper()
    gtype = str(body.get("type") or "USER").upper()
    lvl = body.get("level")
    if gtype not in ("USER", "ROLE") or lvl not in LEVEL_RANK or not name:
        raise web.HTTPBadRequest(text="Need name, type USER|ROLE, level user|auditor|admin|none")
    if gtype == "ROLE" and (name in LEVEL_ROLE_SET or not ROLE_RE.match(name)):
        raise web.HTTPBadRequest(text="Invalid role")
    ident = quote_ident(name)
    grants = await _level_grants()
    current = next((g["level"] for g in grants if g["name"] == name and g["type"] == gtype), "none")
    admins = [g for g in grants if g["level"] == "admin"]
    if current == "admin" and lvl != "admin" and len(admins) <= 1:
        raise web.HTTPBadRequest(text="Can't remove the last Workbench admin")
    for role in LEVEL_ROLES.values():
        try:
            await q(f"REVOKE ROLE {role} FROM {gtype} {ident}")
        except Exception:
            pass
    if lvl != "none":
        try:
            await q(f"GRANT ROLE {LEVEL_ROLES[lvl]} TO {gtype} {ident}")
        except snowflake.connector.errors.ProgrammingError as e:
            raise web.HTTPBadRequest(text=f"Could not grant: {e.msg}")
    _roles_cache.clear()
    await audit(request, "set_level", f"{gtype} {name}", {"from": current, "to": lvl})
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- admin: sessions, audit, settings
async def admin_sessions(request):
    await require(request, "auditor")
    rows = await q(f"SELECT * FROM {FQ}.SESSIONS WHERE STATE = 'ACTIVE' ORDER BY OWNER, CREATED_AT DESC")
    live = await live_status()
    return web.json_response([session_json(r, live) for r in rows])


async def admin_session_action(request):
    await require(request, "admin")
    s = await get_session(request.match_info["sid"])
    action = request.match_info["action"]
    await _session_action(s, action)
    await audit(request, f"session_{action}", s["ID"], {"owner": s["OWNER"], "name": s["NAME"]})
    return web.json_response({"ok": True})


async def admin_audit(request):
    await require(request, "auditor")
    p = request.query
    days = min(max(int(p.get("days", "7") or 7), 1), 3650)
    where, args = ["START_TIME > DATEADD(day, -%s, CURRENT_TIMESTAMP())"], [days]
    for key, col in (("user", "SESSION_OWNER"), ("session", "SESSION_ID")):
        if p.get(key):
            where.append(f"{col} = %s")
            args.append(p[key].upper() if key == "user" else p[key])
    if p.get("q"):
        where.append("QUERY_TEXT ILIKE %s")
        args.append(f"%{p['q']}%")
    rows = await q(
        f"SELECT START_TIME, SESSION_OWNER, SESSION_NAME, SESSION_ID, IDE, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, "
        f"ROWS_PRODUCED, TOTAL_ELAPSED_TIME, LEFT(QUERY_TEXT, 400) AS QUERY_TEXT, QUERY_ID, OBJECTS_READ, OBJECTS_WRITTEN "
        f"FROM {FQ}.SESSION_QUERY_LOG WHERE {' AND '.join(where)} ORDER BY START_TIME DESC LIMIT 500", tuple(args))
    return web.json_response([{**r, "START_TIME": _iso(r["START_TIME"]), "OBJECTS_READ": _variant(r["OBJECTS_READ"]),
                               "OBJECTS_WRITTEN": _variant(r["OBJECTS_WRITTEN"])} for r in rows])


async def admin_audit_refresh(request):
    await require(request, "admin")
    await q(f"EXECUTE TASK {FQ}.ARCHIVE_SESSION_QUERIES")
    return web.json_response({"ok": True, "note": "Archive task started; Snowflake's ACCOUNT_USAGE lags ~45 min."})


async def admin_actions(request):
    await require(request, "auditor")
    rows = await q(f"SELECT TS, ACTOR, ACTION, TARGET, DETAIL FROM {FQ}.ADMIN_AUDIT ORDER BY TS DESC LIMIT 500")
    return web.json_response([{**r, "TS": _iso(r["TS"]), "DETAIL": _variant(r["DETAIL"])} for r in rows])


async def admin_get_settings(request):
    await require(request, "auditor")
    roles = [r["name"] for r in await q("SHOW ROLES") if r["name"] not in LEVEL_ROLE_SET]
    return web.json_response({"settings": await get_settings(force=True), "all_sizes": list(SIZES),
                              "known_roles": sorted(roles)})


async def admin_put_settings(request):
    await require(request, "admin")
    body = await request.json()
    clean = {}
    if "allowed_session_roles" in body:
        roles = [str(r).upper() for r in body["allowed_session_roles"] or []]
        if not all(ROLE_RE.match(r) for r in roles):
            raise web.HTTPBadRequest(text="Invalid role name")
        clean["allowed_session_roles"] = roles
    if "idle_minutes" in body:
        clean["idle_minutes"] = max(15, min(int(body["idle_minutes"]), 10080))
    if "sizes" in body:
        sizes = [s for s in body["sizes"] if s in SIZES]
        if not sizes:
            raise web.HTTPBadRequest(text="Keep at least one size")
        clean["sizes"] = sizes
    for k, v in clean.items():
        await q(f"MERGE INTO {FQ}.SETTINGS t USING (SELECT %s AS K, PARSE_JSON(%s) AS V, %s AS U) s ON t.KEY = s.K "
                f"WHEN MATCHED THEN UPDATE SET VALUE = s.V, UPDATED_BY = s.U, UPDATED_AT = CURRENT_TIMESTAMP() "
                f"WHEN NOT MATCHED THEN INSERT (KEY, VALUE, UPDATED_BY) VALUES (s.K, s.V, s.U)",
                (k, json.dumps(v), current_user(request)))
    await audit(request, "settings", ",".join(clean), clean)
    _roles_cache.clear()
    return web.json_response({"settings": await get_settings(force=True)})


# ---------------------------------------------------------------- packages
_sync = {"running": False, "queued": False, "last_at": None, "last_error": None, "task": None}


async def _policy():
    return await q(f"SELECT * FROM {FQ}.PACKAGE_POLICY")


def _version_spec(eco, raw):
    """Normalise a list/comma string of approved versions; "" means latest only."""
    toks = raw if isinstance(raw, list) else str(raw or "").split(",")
    toks = list(dict.fromkeys(re.sub(r"\s+", "", str(t)) for t in toks if str(t).strip()))
    if len(toks) > 20 or not all(pkgrepo.valid_version_token(eco, t) for t in toks):
        raise web.HTTPBadRequest(text="Versions must be exact versions (e.g. 1.15.0) or 'latest'"
                                      + ("; PyPI also accepts specifiers like >=2.0" if eco == "pypi" else ""))
    if eco == "bioc" and toks not in ([], ["latest"]):
        raise web.HTTPBadRequest(text="Bioconductor versions are fixed by each R image's Bioconductor release")
    return "" if toks in ([], ["latest"]) else ",".join(toks)


def _pkg_args(eco, name):
    if eco not in pkgrepo.ECOSYSTEMS or not pkgrepo.NAME_RE.match(name or ""):
        raise web.HTTPBadRequest(text="bad ecosystem/name")
    return eco, name


async def run_sync():
    """Rebuild the curated repo from PACKAGE_POLICY (coalesces concurrent requests)."""
    if _sync["running"]:
        _sync["queued"] = True
        return
    _sync["running"] = True
    try:
        while True:
            _sync["queued"] = False
            rows = await _policy()
            result = await asyncio.to_thread(pkgrepo.sync_all, rows)
            await asyncio.to_thread(pkgrepo.persist, sf.run, REPO_STAGE)
            spec_at_start = {(r["ECOSYSTEM"], r["NAME"]): r["VERSION_SPEC"] for r in rows}
            for (eco, name), (status, detail) in result.items():
                # Skip rows whose approved versions changed mid-sync; they stay QUEUED for the next pass
                await q(f"UPDATE {FQ}.PACKAGE_POLICY SET MIRROR_STATUS = %s, MIRROR_DETAIL = PARSE_JSON(%s), "
                        f"MIRRORED_AT = CURRENT_TIMESTAMP() WHERE ECOSYSTEM = %s AND NAME = %s "
                        f"AND STATUS = 'APPROVED' AND EQUAL_NULL(VERSION_SPEC, %s)",
                        (status, json.dumps(detail), eco, name, spec_at_start.get((eco, name))))
            if await q(f"SELECT 1 FROM {FQ}.PACKAGE_POLICY WHERE MIRROR_STATUS = 'QUEUED' LIMIT 1"):
                _sync["queued"] = True
            await q(f"UPDATE {FQ}.PACKAGE_POLICY SET MIRROR_STATUS = NULL, MIRROR_DETAIL = NULL "
                    f"WHERE STATUS <> 'APPROVED' AND MIRROR_STATUS IS NOT NULL")
            _sync.update(last_at=time.time(), last_error=None)
            if not _sync["queued"]:
                break
    except Exception as e:
        log.exception("package sync failed")
        _sync["last_error"] = str(e)
    finally:
        _sync["running"] = False


def kick_sync():
    _sync["task"] = asyncio.create_task(run_sync())


def _pkg_json(r):
    return {"ecosystem": r["ECOSYSTEM"], "name": r["NAME"], "status": r["STATUS"], "version_spec": r["VERSION_SPEC"],
            "versions": pkgrepo.parse_versions(r["VERSION_SPEC"]),
            "note": r["NOTE"], "requested_by": r["REQUESTED_BY"], "requested_at": _iso(r["REQUESTED_AT"]),
            "decided_by": r["DECIDED_BY"], "decided_at": _iso(r["DECIDED_AT"]),
            "mirror_status": r["MIRROR_STATUS"], "mirror_detail": _variant(r["MIRROR_DETAIL"]),
            "mirrored_at": _iso(r["MIRRORED_AT"])}


async def api_packages(request):
    lvl = await require(request, "user")
    rows = [_pkg_json(r) for r in await _policy()]
    rows.sort(key=lambda r: ({"REQUESTED": 0, "APPROVED": 1, "DENIED": 2}.get(r["status"], 3), r["name"].lower()))
    included = [{"ecosystem": e, "name": n} for e, names in pkgrepo.INCLUDED.items() for n in sorted(names)]
    return web.json_response({"level": lvl, "packages": rows, "included": included,
                              "r_targets": list(pkgrepo.R_TARGETS), "python": pkgrepo.PY_VERSION,
                              "sync": {k: _sync[k] for k in ("running", "queued", "last_at", "last_error")}})


def _status_map(rows):
    return {(r["ECOSYSTEM"], r["NAME"].lower()): r["STATUS"] for r in rows}


async def api_pkg_search(request):
    await require(request, "user")
    eco, qtext = request.query.get("eco", "cran"), (request.query.get("q") or "").strip()
    if eco not in pkgrepo.ECOSYSTEMS or not re.match(r"^[A-Za-z0-9._-]{2,60}$", qtext):
        raise web.HTTPBadRequest(text="Type at least 2 letters of a package name")
    try:
        found = await asyncio.to_thread(pkgrepo.search, eco, qtext)
    except Exception as e:
        raise web.HTTPBadGateway(text=f"Package index unavailable: {e}")
    st = _status_map(await _policy())
    inc = {n.lower() for n in pkgrepo.INCLUDED[eco]}
    for f in found:
        f["status"] = "INCLUDED" if f["name"].lower() in inc else st.get((eco, f["name"].lower()))
    return web.json_response(found)


async def api_pkg_info(request):
    await require(request, "user")
    eco, name = _pkg_args(request.query.get("eco"), request.query.get("name"))
    try:
        d = await asyncio.to_thread(pkgrepo.info, eco, name)
    except Exception as e:
        raise web.HTTPBadGateway(text=f"Could not load package info: {e}")
    rows = await _policy()
    st = _status_map(rows)
    d["status"] = "INCLUDED" if name.lower() in {n.lower() for n in pkgrepo.INCLUDED[eco]} else st.get((eco, name.lower()))
    deps_eco = ("pypi",) if eco == "pypi" else ("cran", "bioc")
    d["depends"] = [{"name": n, "status": next((st.get((e, n.lower())) for e in deps_eco if st.get((e, n.lower()))),
                                               "INCLUDED" if any(n in pkgrepo.INCLUDED[e] for e in deps_eco) else None)}
                    for n in d["depends"]]
    return web.json_response(d)


async def api_pkg_request(request):
    await require(request, "user")
    body = await request.json()
    eco, name = _pkg_args(body.get("ecosystem"), body.get("name"))
    note = str(body.get("note") or "")[:500]
    want = _version_spec(eco, body.get("version"))
    existing = [r for r in await _policy() if r["ECOSYSTEM"] == eco and r["NAME"].lower() == name.lower()]
    if existing and existing[0]["STATUS"] == "DENIED":
        return web.json_response({"status": "DENIED"})
    if existing and existing[0]["STATUS"] == "APPROVED":
        have = pkgrepo.parse_versions(existing[0]["VERSION_SPEC"])
        if not want or want in have:
            return web.json_response({"status": "APPROVED"})
        # Approved package, new version wanted: record the request; admin adds it to the approved list
        note = f"version {want} requested by {current_user(request)}" + (f": {note}" if note else "")
        await q(f"UPDATE {FQ}.PACKAGE_POLICY SET REQUESTED_BY = %s, REQUESTED_AT = CURRENT_TIMESTAMP(), "
                f"NOTE = %s WHERE ECOSYSTEM = %s AND NAME = %s", (current_user(request), note, eco, existing[0]["NAME"]))
        return web.json_response({"status": "APPROVED", "version_requested": want})
    await q(f"MERGE INTO {FQ}.PACKAGE_POLICY t USING (SELECT %s E, %s N, %s U, %s NT, %s V) s "
            f"ON t.ECOSYSTEM = s.E AND t.NAME = s.N "
            f"WHEN MATCHED THEN UPDATE SET REQUESTED_BY = s.U, REQUESTED_AT = CURRENT_TIMESTAMP(), NOTE = s.NT, "
            f"VERSION_SPEC = s.V "
            f"WHEN NOT MATCHED THEN INSERT (ECOSYSTEM, NAME, STATUS, NOTE, VERSION_SPEC, REQUESTED_BY, REQUESTED_AT) "
            f"VALUES (s.E, s.N, 'REQUESTED', s.NT, s.V, s.U, CURRENT_TIMESTAMP())",
            (eco, name, current_user(request), note, want or None))
    return web.json_response({"status": "REQUESTED"})


async def api_pkg_versions(request):
    await require(request, "user")
    eco, name = _pkg_args(request.query.get("eco"), request.query.get("name"))
    try:
        rows = await asyncio.to_thread(pkgrepo.versions, eco, name)
    except Exception as e:
        raise web.HTTPBadGateway(text=f"Could not load versions: {e}")
    return web.json_response({"versions": rows, "pinnable": eco != "bioc"})


async def api_pkg_decide(request):
    await require(request, "admin")
    body = await request.json()
    eco, name = _pkg_args(body.get("ecosystem"), body.get("name"))
    status = str(body.get("status") or "").upper()
    if status not in ("APPROVED", "DENIED", "REMOVE"):
        raise web.HTTPBadRequest(text="status must be APPROVED, DENIED or REMOVE")
    spec = _version_spec(eco, body.get("version_spec"))
    note = str(body.get("note") or "")[:500]
    if status == "REMOVE":
        await q(f"DELETE FROM {FQ}.PACKAGE_POLICY WHERE ECOSYSTEM = %s AND NAME = %s", (eco, name))
    else:
        await q(f"MERGE INTO {FQ}.PACKAGE_POLICY t USING (SELECT %s E, %s N, %s S, %s V, %s NT, %s U) s "
                f"ON t.ECOSYSTEM = s.E AND t.NAME = s.N "
                f"WHEN MATCHED THEN UPDATE SET STATUS = s.S, VERSION_SPEC = s.V, NOTE = COALESCE(NULLIF(s.NT, ''), t.NOTE), "
                f"DECIDED_BY = s.U, DECIDED_AT = CURRENT_TIMESTAMP(), MIRROR_STATUS = IFF(s.S = 'APPROVED', 'QUEUED', NULL) "
                f"WHEN NOT MATCHED THEN INSERT (ECOSYSTEM, NAME, STATUS, VERSION_SPEC, NOTE, DECIDED_BY, DECIDED_AT, MIRROR_STATUS) "
                f"VALUES (s.E, s.N, s.S, s.V, s.NT, s.U, CURRENT_TIMESTAMP(), IFF(s.S = 'APPROVED', 'QUEUED', NULL))",
                (eco, name, status, spec or None, note, current_user(request)))
    await audit(request, f"package_{status.lower()}", f"{eco}:{name}", {"version_spec": spec, "note": note})
    kick_sync()
    return web.json_response({"ok": True})


async def api_pkg_sync(request):
    await require(request, "admin")
    kick_sync()
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- open
STARTING_PAGE = """<!doctype html><meta http-equiv="refresh" content="5">
<body style="font-family:system-ui;padding:3rem;color:#334"><h2>Session is starting&hellip;</h2>
<p>{msg}</p><p>This page refreshes automatically. <a href="/">Back to Workbench</a></p></body>"""
NO_ACCESS_PAGE = """<!doctype html><body style="font-family:system-ui;padding:3rem;color:#334">
<h2>No Workbench access</h2><p>Ask a Workbench admin to add you (Admin &gt; Users).</p></body>"""


async def ingress_url(service_name):
    rows = await q(f"SHOW ENDPOINTS IN SERVICE {FQ}.{service_name}")
    url = rows[0]["ingress_url"] if rows else ""
    return url if url and "provisioning" not in url.lower() else None


async def open_session(request):
    """Send the owner to the session's own endpoint (it collects its own caller token there)."""
    await require(request, "user")
    s = await get_session(request.match_info["sid"], current_user(request))
    live = (await live_status()).get(s["SERVICE_NAME"])
    st = live["status"] if live else "UNKNOWN"
    if st == "SUSPENDED":
        return web.Response(text=STARTING_PAGE.format(msg="This session is suspended. Resume it from the portal."),
                            content_type="text/html")
    if st != "RUNNING":
        return web.Response(text=STARTING_PAGE.format(msg=f"Status: {st}"), content_type="text/html")
    url = await ingress_url(s["SERVICE_NAME"])
    if not url:
        return web.Response(text=STARTING_PAGE.format(msg="Setting up the session's secure URL (about 1-2 minutes on first launch)."),
                            content_type="text/html")
    _last_active[s["ID"]] = time.time()
    raise web.HTTPFound(f"https://{url}/")


# ---------------------------------------------------------------- idle reaper
async def reaper(app):
    while True:
        await asyncio.sleep(300)
        try:
            idle = int((await get_settings())["idle_minutes"])
            for sid, ts in list(_last_active.items()):  # portal-side opens; sessions report their own activity
                await q(f"UPDATE {FQ}.SESSIONS SET LAST_ACTIVE = GREATEST(LAST_ACTIVE, TO_TIMESTAMP_LTZ(%s)) WHERE ID = %s", (int(ts), sid))
            live = await live_status(force=True)
            rows = await q(f"SELECT ID, SERVICE_NAME, LAST_ACTIVE FROM {FQ}.SESSIONS WHERE STATE = 'ACTIVE' "
                           f"AND LAST_ACTIVE < DATEADD(minute, -%s, CURRENT_TIMESTAMP())", (idle,))
            for r in rows:
                if live.get(r["SERVICE_NAME"], {}).get("status") == "RUNNING":
                    log.info("idle > %s min, suspending %s", idle, r["SERVICE_NAME"])
                    await q(f"ALTER SERVICE IF EXISTS {FQ}.{r['SERVICE_NAME']} SUSPEND")
                    _last_active.pop(r["ID"], None)
            # Approvals made outside the UI (or a sync interrupted by a restart) get picked up here
            if not _sync["running"] and await q(f"SELECT 1 FROM {FQ}.PACKAGE_POLICY WHERE MIRROR_STATUS = 'QUEUED' LIMIT 1"):
                kick_sync()
        except Exception:
            log.exception("reaper pass failed")


# ---------------------------------------------------------------- pages
STATIC = Path(__file__).parent / "static"


def page(name, need):
    async def handler(request):
        try:
            await require(request, need)
        except web.HTTPForbidden:
            if need == "user":
                return web.Response(text=NO_ACCESS_PAGE, content_type="text/html", status=403)
            raise web.HTTPFound("/")
        return web.FileResponse(STATIC / name)
    return handler


async def healthz(request):
    return web.Response(text="ok")


# ---------------------------------------------------------------- private repo endpoint (:8081)
async def repo_file(request):
    """Read-only curated repo for sessions (private endpoint; never public)."""
    root = pkgrepo.ROOT.resolve()
    p = (root / request.match_info["path"]).resolve()
    if p != root and root not in p.parents:
        raise web.HTTPNotFound()
    if p.is_dir():
        p = p / "index.html"
    if not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p)


def make_repo_app():
    app = web.Application()
    app.router.add_get("/{path:.*}", repo_file)
    return app


async def on_startup(app):
    app["client"] = aiohttp.ClientSession(auto_decompress=True, timeout=aiohttp.ClientTimeout(total=None))
    app["reaper"] = asyncio.create_task(reaper(app))
    if os.path.exists(SVC_TOKEN):
        app["repo_boot"] = asyncio.create_task(restore_repo())


async def restore_repo():
    """Local repo is ephemeral: restore from @PKG_REPO, then reconcile with PACKAGE_POLICY."""
    try:
        await asyncio.to_thread(pkgrepo.restore, sf.run, REPO_STAGE)
    except Exception:
        log.exception("repo restore failed; a full sync will re-download")
    await run_sync()


async def on_cleanup(app):
    app["reaper"].cancel()
    await app["client"].close()


def make_app():
    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", page("index.html", "user"))
    app.router.add_get("/packages", page("packages.html", "user"))
    app.router.add_get("/admin", page("admin.html", "auditor"))
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/sessions", api_list)
    app.router.add_post("/api/sessions", api_create)
    app.router.add_get("/api/sessions/{sid}/queries", api_queries)
    app.router.add_post("/api/sessions/{sid}/{action}", api_action)
    app.router.add_delete("/api/sessions/{sid}", api_delete)
    app.router.add_get("/api/admin/users", admin_users)
    app.router.add_post("/api/admin/users", admin_set_user)
    app.router.add_get("/api/admin/sessions", admin_sessions)
    app.router.add_post("/api/admin/sessions/{sid}/{action}", admin_session_action)
    app.router.add_get("/api/admin/audit", admin_audit)
    app.router.add_post("/api/admin/audit/refresh", admin_audit_refresh)
    app.router.add_get("/api/admin/actions", admin_actions)
    app.router.add_get("/api/admin/settings", admin_get_settings)
    app.router.add_put("/api/admin/settings", admin_put_settings)
    app.router.add_get("/api/packages", api_packages)
    app.router.add_get("/api/packages/search", api_pkg_search)
    app.router.add_get("/api/packages/info", api_pkg_info)
    app.router.add_get("/api/packages/versions", api_pkg_versions)
    app.router.add_post("/api/packages/request", api_pkg_request)
    app.router.add_post("/api/packages/decide", api_pkg_decide)
    app.router.add_post("/api/packages/sync", api_pkg_sync)
    app.router.add_get("/s/{sid}", open_session)
    app.router.add_get("/s/{sid}/{tail:.*}", open_session)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def main():
    runners = []
    for app, port in ((make_app(), 8080), (make_repo_app(), 8081)):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("portal on :8080, package repo on :8081")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
