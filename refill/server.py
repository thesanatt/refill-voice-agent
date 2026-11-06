import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest, SmallWebRTCRequestHandler

from refill.core import receive_text, text_thread
from refill.db import init_db
from refill.voice import DEMO_CALLER_PHONE, run_call

HOST = "127.0.0.1"
PORT = 7860
MAX_TEXT_LENGTH = 500
PAGE = Path(__file__).parent / "web" / "index.html"

webrtc_requests = SmallWebRTCRequestHandler()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield
    await webrtc_requests.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def page():
    return FileResponse(PAGE)


@app.get("/api/sms")
async def sms_thread(after: int = 0):
    return await asyncio.to_thread(text_thread, DEMO_CALLER_PHONE, after)


@app.post("/api/sms")
async def sms_reply(request: Request):
    body = await request.json()
    text = str(body.get("text", "")).strip()[:MAX_TEXT_LENGTH]
    if text:
        await receive_text(DEMO_CALLER_PHONE, text)
    return {"ok": True}


@app.post("/api/offer")
async def offer(request: Request, background_tasks: BackgroundTasks):
    async def start_call(connection):
        background_tasks.add_task(run_call, connection)

    return await webrtc_requests.handle_web_request(SmallWebRTCRequest.from_dict(await request.json()), start_call)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
