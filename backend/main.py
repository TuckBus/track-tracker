from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .intelligence import NemotronClient
from .models import ActionPayload
from .providers import AmtrakProvider, OpenSkyProvider, PRTProvider
from .service import DispatchService

service = DispatchService(
    interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
    nemotron=NemotronClient(
        endpoint=os.getenv("NEMOTRON_ENDPOINT"),
        api_key=os.getenv("NEMOTRON_API_KEY"),
        model=os.getenv("NEMOTRON_MODEL", "nvidia/nemotron"),
    ),
)
service.providers = [
    PRTProvider(os.getenv("PRT_VEHICLE_POSITIONS_URL")),
    AmtrakProvider(os.getenv("AMTRAK_TELEMETRY_URL")),
    OpenSkyProvider(os.getenv("OPENSKY_STATES_URL")),
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    await service.start()
    yield
    await service.stop()


app = FastAPI(title="Project Dispatch", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/state")
async def state() -> dict:
    return service.snapshot()


@app.post("/api/poll")
async def poll() -> dict:
    action = await service.poll_once()
    return {"action": action.to_dict(), "state": service.snapshot()}


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        async for event in service.subscribe():
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class MitigationRequest(BaseModel):
    type: str


@app.post("/api/actions/{action_id}/execute")
async def execute_action(action_id: str, request: MitigationRequest) -> dict:
    if request.type not in {"draft_email", "reschedule_calendar"}:
        raise HTTPException(status_code=400, detail="Unsupported mitigation action")
    source = next((alert for alert in service.alerts if alert.id == action_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Action not found")
    executed = ActionPayload(status=source.status, action=request.type, message=f"{request.type.replace('_', ' ').capitalize()} prepared for your review.", source="dispatch")
    service.add_action(executed)
    await service.publish({"type": "action", "action": executed.to_dict()})
    return executed.to_dict()
