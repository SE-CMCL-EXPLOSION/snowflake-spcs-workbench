"""In-session reverse proxy (public endpoint 8080 -> IDE).

Each session is its own SPCS service with its own public endpoint (executeAsCaller).
SPCS ingress authenticates the user; we additionally require the user to be the
session owner (WB_OWNER). The caller token SPCS adds is combined with THIS service's
token and written to /run/sf/token, so R / Python connect as the signed-in user.
(SPCS OAuth tokens only work from the service they were issued to - they can't be
minted in the portal and handed over.)
"""
import asyncio
import os
import time

import aiohttp
from aiohttp import web

UPSTREAM = os.environ.get("WB_UPSTREAM", "http://127.0.0.1:8787")
OWNER = os.environ.get("WB_OWNER", "").upper()
SESSION_ID = os.environ.get("WB_SESSION_ID", "")
TOKEN_FILE = "/run/sf/token"
SVC_TOKEN = "/snowflake/session/token"
UID, GID = int(os.environ.get("WB_UID", "1000")), int(os.environ.get("WB_GID", "1000"))
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host",
       "proxy-connection", "te", "trailer", "content-length"}
_state = {"caller": None, "active": time.time(), "reported": 0.0}

DENIED = """<!doctype html><body style="font-family:system-ui;padding:3rem;color:#334">
<h2>Not your session</h2><p>This workbench session belongs to another user.</p></body>"""


def read_svc_token():
    with open(SVC_TOKEN) as f:
        return f.read().strip()


def capture(request):
    caller = request.headers.get("Sf-Context-Current-User-Token")
    if not caller or caller == _state["caller"]:
        return
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(read_svc_token() + "." + caller)
    os.chmod(tmp, 0o600)
    os.chown(tmp, UID, GID)
    os.replace(tmp, TOKEN_FILE)
    _state["caller"] = caller


def fwd_headers(request):
    h = {k: v for k, v in request.headers.items()
         if k.lower() not in HOP and not k.lower().startswith("sf-context")}
    h["X-Forwarded-Proto"] = "https"
    h["X-Forwarded-Host"] = request.host
    h["Host"] = request.host
    return h


def fix_location(value, request):
    # IDEs may build absolute redirects from their own address; point them back at the endpoint
    for local in (UPSTREAM, UPSTREAM.replace("127.0.0.1", "localhost"), "http://" + request.host):
        if value.startswith(local):
            return "https://" + request.host + value[len(local):]
    return value


async def health(request):
    try:
        async with request.app["client"].get(UPSTREAM + "/", allow_redirects=False,
                                              timeout=aiohttp.ClientTimeout(total=3)):
            return web.Response(text="ok")
    except Exception:
        return web.Response(status=503, text="starting")


async def pump(src, dst):
    async for msg in src:
        _state["active"] = time.time()
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type == aiohttp.WSMsgType.PING:
            await dst.ping(msg.data)
        elif msg.type == aiohttp.WSMsgType.PONG:
            await dst.pong(msg.data)
        else:
            break
    await dst.close()


async def handler(request):
    if request.path == "/healthz":  # readiness probe comes from inside SPCS, no user headers
        return await health(request)
    user = request.headers.get("Sf-Context-Current-User", "").upper()
    if not OWNER or user != OWNER:
        return web.Response(status=403, text=DENIED, content_type="text/html")
    capture(request)
    _state["active"] = time.time()
    url = UPSTREAM + request.path_qs
    session = request.app["client"]

    if request.headers.get("Upgrade", "").lower() == "websocket":
        protos = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        ws_client = web.WebSocketResponse(autoping=False, protocols=protos, max_msg_size=0)
        await ws_client.prepare(request)
        hdrs = {k: v for k, v in fwd_headers(request).items() if not k.lower().startswith("sec-websocket")}
        async with session.ws_connect(url.replace("http", "ws", 1), headers=hdrs, protocols=protos,
                                      autoping=False, max_msg_size=0) as ws_up:
            await asyncio.gather(pump(ws_client, ws_up), pump(ws_up, ws_client), return_exceptions=True)
        return ws_client

    body = await request.read()
    async with session.request(request.method, url, headers=fwd_headers(request), data=body,
                               allow_redirects=False) as up:
        resp = web.StreamResponse(status=up.status)
        for k, v in up.headers.items():
            if k.lower() not in HOP and k.lower() != "content-encoding":
                resp.headers.add(k, fix_location(v, request) if k.lower() == "location" else v)
        await resp.prepare(request)
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def report_activity(app):
    """Push LAST_ACTIVE to the portal's SESSIONS table (service token = owner role) for the idle reaper."""
    table = os.environ.get("WB_SESSIONS_TABLE")
    if not table or not SESSION_ID:
        return
    url = f"https://{os.environ['SNOWFLAKE_HOST']}/api/v2/statements"
    while True:
        await asyncio.sleep(300)
        if _state["active"] <= _state["reported"]:
            continue
        try:
            body = {"statement": f"UPDATE {table} SET LAST_ACTIVE = TO_TIMESTAMP_LTZ(?) WHERE ID = ?",
                    "bindings": {"1": {"type": "FIXED", "value": str(int(_state["active"]))},
                                 "2": {"type": "TEXT", "value": SESSION_ID}},
                    "warehouse": os.environ.get("WB_WAREHOUSE", "")}
            hdrs = {"Authorization": f"Bearer {read_svc_token()}",
                    "X-Snowflake-Authorization-Token-Type": "OAUTH", "Accept": "application/json"}
            async with app["client"].post(url, json=body, headers=hdrs) as r:
                if r.status < 300:
                    _state["reported"] = _state["active"]
                else:
                    print("activity report failed", r.status, (await r.text())[:200], flush=True)
        except Exception as e:
            print("activity report error", e, flush=True)


async def on_startup(app):
    app["client"] = aiohttp.ClientSession(auto_decompress=True, timeout=aiohttp.ClientTimeout(total=None))
    app["reporter"] = asyncio.create_task(report_activity(app))


async def init():
    app = web.Application(client_max_size=1024 ** 3)
    app.on_startup.append(on_startup)
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


if __name__ == "__main__":
    os.makedirs("/run/sf", exist_ok=True)
    os.chown("/run/sf", UID, GID)
    web.run_app(init(), host="0.0.0.0", port=8080)
