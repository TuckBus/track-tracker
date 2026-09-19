from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.request import Request, urlopen

from .models import ActionPayload, TelemetryRecord

logger = logging.getLogger(__name__)
VALID_STATUSES = {"disrupted", "on_time", "anomalous"}
VALID_ACTIONS = {"trigger_ui_alert", "draft_email", "reschedule_calendar", "none"}


def build_prompt(itinerary: list[dict[str, Any]], telemetry: list[TelemetryRecord]) -> str:
    return (
        "You are a silent transit anomaly compiler. Return ONLY one JSON object with "
        'status ("disrupted"|"on_time"|"anomalous"), action '
        '("trigger_ui_alert"|"draft_email"|"reschedule_calendar"|"none"), and message. '
        "Do not provide markdown or conversational text.\n"
        f"ITINERARY:\n{json.dumps(itinerary, separators=(',', ':'))}\n"
        f"TELEMETRY:\n{json.dumps([item.to_dict() for item in telemetry], separators=(',', ':'))}"
    )


def parse_action(raw: str | dict[str, Any]) -> ActionPayload:
    candidate: Any = raw
    if isinstance(raw, str):
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                raise ValueError("Nemotron response did not contain a JSON object") from None
            candidate = json.loads(match.group(0))
    if not isinstance(candidate, dict):
        raise ValueError("Nemotron response must be a JSON object")
    status = candidate.get("status")
    action = candidate.get("action")
    message = candidate.get("message")
    if status not in VALID_STATUSES or action not in VALID_ACTIONS or not isinstance(message, str) or not message.strip():
        raise ValueError("Nemotron response does not match the action schema")
    return ActionPayload(status=status, action=action, message=message.strip())


def fallback_action(telemetry: list[TelemetryRecord]) -> ActionPayload:
    stalled = [item for item in telemetry if item.source == "amtrak" and item.speed_mph is not None and item.speed_mph < 1]
    if stalled:
        return ActionPayload(
            status="anomalous",
            action="trigger_ui_alert",
            message="Pennsylvanian telemetry reports a stationary train; review your itinerary.",
            source="dispatch-fallback",
        )
    return ActionPayload(status="on_time", action="none", message="No actionable disruption detected.", source="dispatch-fallback")


class NemotronClient:
    def __init__(self, endpoint: str | None = None, api_key: str | None = None, model: str = "nvidia/nemotron") -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    def analyze(self, itinerary: list[dict[str, Any]], telemetry: list[TelemetryRecord]) -> ActionPayload:
        if not self.endpoint:
            return fallback_action(telemetry)
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "system", "content": build_prompt(itinerary, telemetry)}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            request = Request(self.endpoint, data=body, headers=headers, method="POST")
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
            content = result["choices"][0]["message"]["content"]
            return parse_action(content)
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error("Nemotron request failed; using deterministic fallback: %s", exc)
            return fallback_action(telemetry)
