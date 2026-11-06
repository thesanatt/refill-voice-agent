from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest, SmallWebRTCRequestHandler

from refill.db import init_db
from refill.voice import run_call

HOST = "127.0.0.1"
PORT = 7860
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


@app.post("/api/offer")
async def offer(request: Request, background_tasks: BackgroundTasks):
    async def start_call(connection):
        background_tasks.add_task(run_call, connection)

    return await webrtc_requests.handle_web_request(SmallWebRTCRequest.from_dict(await request.json()), start_call)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
