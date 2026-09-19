from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

Status = Literal["disrupted", "on_time", "anomalous"]
Action = Literal["trigger_ui_alert", "draft_email", "reschedule_calendar", "none"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class TelemetryRecord:
    source: str
    vehicle_id: str
    route: str | None
    latitude: float | None
    longitude: float | None
    speed_mph: float | None
    altitude_ft: float | None
    observed_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["observed_at"] = self.observed_at.isoformat()
        return value


@dataclass(slots=True)
class ActionPayload:
    status: Status
    action: Action
    message: str
    source: str = "nemotron"
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_at"] = self.created_at.isoformat()
        return value
