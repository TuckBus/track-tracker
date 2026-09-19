from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from .intelligence import NemotronClient
from .models import ActionPayload, TelemetryRecord
from .providers import AmtrakProvider, OpenSkyProvider, PRTProvider

logger = logging.getLogger(__name__)


class DispatchService:
    def __init__(self, *, interval_seconds: int = 60, nemotron: NemotronClient | None = None) -> None:
        self.interval_seconds = max(30, interval_seconds)
        self.providers = [PRTProvider(), AmtrakProvider(), OpenSkyProvider()]
        self.nemotron = nemotron or NemotronClient()
        self.telemetry: list[TelemetryRecord] = []
        self.alerts: deque[ActionPayload] = deque(maxlen=50)
        self.last_poll_at: datetime | None = None
        self.poll_errors: list[str] = []
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self.itinerary = [
            {"id": "pennsylvanian", "label": "Pennsylvanian 42", "mode": "rail", "status": "Monitoring"},
            {"id": "pit-flight", "label": "PIT departure", "mode": "air", "status": "Monitoring"},
        ]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())
        await self.poll_once()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.poll_once()

    async def poll_once(self) -> ActionPayload:
        records: list[TelemetryRecord] = []
        errors: list[str] = []
        for provider in self.providers:
            try:
                records.extend(await asyncio.to_thread(provider.fetch))
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"{provider.__class__.__name__}: {exc}")
                logger.warning("Telemetry provider failed: %s", errors[-1])
        self.telemetry = records
        self.poll_errors = errors
        self.last_poll_at = datetime.now(timezone.utc)
        action = self.nemotron.analyze(self.itinerary, records)
        if action.action != "none":
            self.alerts.appendleft(action)
            await self.publish({"type": "action", "action": action.to_dict()})
        await self.publish({"type": "state", "state": self.snapshot()})
        return action

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "telemetry": [item.to_dict() for item in self.telemetry],
            "itineraries": self.itinerary,
            "alerts": [item.to_dict() for item in self.alerts],
            "provider_errors": self.poll_errors,
        }

    async def publish(self, event: dict[str, Any]) -> None:
        for queue in tuple(self.subscribers):
            await queue.put(event)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.add(queue)
        try:
            yield {"type": "state", "state": self.snapshot()}
            while True:
                yield await queue.get()
        finally:
            self.subscribers.discard(queue)

    def add_action(self, action: ActionPayload) -> ActionPayload:
        self.alerts.appendleft(action)
        return action
