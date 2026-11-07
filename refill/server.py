import asyncio
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from markdown import markdown
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest, SmallWebRTCRequestHandler

from refill.core import receive_text, text_thread
from refill.db import init_db
from refill.voice import DEMO_CALLER_PHONE, run_call

HOST = os.environ.get("REFILL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7860"))
ACCESS_CODE = os.environ.get("DEMO_ACCESS_CODE", "")
MAX_ACTIVE_CALLS = int(os.environ.get("MAX_ACTIVE_CALLS", "3"))
ICE_SERVERS = json.loads(os.environ.get("TURN_ICE_SERVERS") or "[]")
MAX_TEXT_LENGTH = 500
DEMO_PHONE = re.compile(r"\(\d{3}\) 555-01\d{2}")
WEB = Path(__file__).parent / "web"
GUIDE = Path(__file__).parent.parent / "docs" / "TESTING_GUIDE.md"

webrtc_requests = SmallWebRTCRequestHandler(ice_servers=[IceServer(**server) for server in ICE_SERVERS])
active_calls = set()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield
    await webrtc_requests.close()


app = FastAPI(lifespan=lifespan)


def check_access(request):
    given = request.headers.get("x-access-code") or request.query_params.get("code") or ""
    if ACCESS_CODE and not secrets.compare_digest(given, ACCESS_CODE):
        raise HTTPException(status_code=403, detail="This test line needs the link with its access code.")


def caller_phone(request):
    """Each browser calls from its own made-up number, so testers never share a text thread."""
    phone = request.headers.get("x-caller-phone", "")
    return phone if DEMO_PHONE.fullmatch(phone) else DEMO_CALLER_PHONE


@app.exception_handler(HTTPException)
async def plain_error(_request, error):
    return PlainTextResponse(str(error.detail), status_code=error.status_code)


@app.get("/")
async def page(request: Request):
    check_access(request)
    return FileResponse(WEB / "index.html")


@app.get("/guide")
async def guide(request: Request):
    check_access(request)
    body = markdown(GUIDE.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
    template = (WEB / "guide.html").read_text(encoding="utf-8")
    return HTMLResponse(template.replace("<!-- guide -->", body))


@app.get("/api/ice")
async def ice_servers(request: Request):
    check_access(request)
    return {"iceServers": ICE_SERVERS}


@app.get("/api/sms")
async def sms_thread(request: Request, after: int = 0):
    check_access(request)
    return await asyncio.to_thread(text_thread, caller_phone(request), after)


@app.post("/api/sms")
async def sms_reply(request: Request):
    check_access(request)
    body = await request.json()
    text = str(body.get("text", "")).strip()[:MAX_TEXT_LENGTH]
    if text:
        await receive_text(caller_phone(request), text)
    return {"ok": True}


@app.post("/api/offer")
async def offer(request: Request, background_tasks: BackgroundTasks):
    check_access(request)
    phone = caller_phone(request)
    body = await request.json()
    if not body.get("pc_id") and len(active_calls) >= MAX_ACTIVE_CALLS:
        return JSONResponse({"error": "All lines are busy. Try again in a minute."}, status_code=503)

    async def counted_call(connection):
        active_calls.add(connection.pc_id)
        try:
            await run_call(connection, phone)
        finally:
            active_calls.discard(connection.pc_id)

    async def start_call(connection):
        background_tasks.add_task(counted_call, connection)

    return await webrtc_requests.handle_web_request(SmallWebRTCRequest.from_dict(body), start_call)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
