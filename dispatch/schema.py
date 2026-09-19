"""The shapes everything in Dispatch passes around.

Two ingest lanes, one schema each:

  Telemetry lane  -> Observation   (where is a vehicle, right now)
  Document lane   -> Document      (an itinerary the user actually has)

Every source adapter, whatever its wire format, produces one of those two.
Detectors read Observations and emit Signals. Extraction reads Documents and
emits ItineraryItems. Both carry Provenance, so any claim on screen can be
traced to the byte range it came from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Where a piece of information came from, precisely enough to show it.

    `locator` is deliberately loose: a page number for a PDF, a header name
    for an email, a cell for a CSV, a feed entity id for protobuf. Each
    adapter fills it with whatever identifies the spot in *its* format.
    """

    source_id: str
    source_type: str
    locator: str
    retrieved_at: int
    char_start: int | None = None
    char_end: int | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# telemetry lane
# --------------------------------------------------------------------------


@dataclass
class Observation:
    """One vehicle, one moment. Normalised across GTFS-RT and Amtrak JSON."""

    source_type: str  # "prt-bus" | "prt-rail" | "amtrak"
    vehicle_id: str
    route_id: str
    trip_id: str
    observed_at: int  # unix seconds, from the feed not the clock
    lat: float | None = None
    lon: float | None = None
    speed: float | None = None
    status: str = ""
    stop_id: str = ""
    stop_sequence: int | None = None
    delay_s: int | None = None  # positive = late, negative = early
    scheduled_layover: bool = False  # set by the terminal-stop join
    raw_ref: str = ""  # feed entity id, for provenance

    @property
    def key(self) -> str:
        return f"{self.source_type}:{self.vehicle_id}"


@dataclass
class OfficialAlert:
    """An agency-published alert. Never an input to detection -- this is the
    ground truth we measure our own lead time against."""

    source_type: str
    alert_id: str
    # When *we first saw it in the feed*. Not Alert.active_period.start:
    # agencies backdate that to when the disruption began, so using it as a
    # publication time silently destroys the lead-time metric. See EVAL.md,
    # "a measurement bug we found".
    published_at: int
    effect: str
    cause: str
    header: str
    description: str
    route_ids: list[str] = field(default_factory=list)
    url: str = ""
    # Retained for display. Agency's claim about when the disruption started.
    active_start: int | None = None


# --------------------------------------------------------------------------
# signals (deterministic detector output)
# --------------------------------------------------------------------------

SignalKind = Literal["STALL", "SCHEDULE_SLIP", "VANISHED", "CASCADE", "CANCELED"]


@dataclass
class Signal:
    """A measured anomaly. No model involvement: this is arithmetic on
    timestamps and coordinates, and it is reproducible from the recording."""

    kind: SignalKind
    route_id: str
    vehicle_id: str
    source_type: str
    detected_at: int  # when Dispatch could first have known
    first_seen_at: int  # when the underlying condition started
    evidence: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)

    @property
    def signal_id(self) -> str:
        basis = f"{self.kind}|{self.source_type}|{self.vehicle_id}|{self.first_seen_at}"
        return hashlib.sha1(basis.encode()).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signal_id"] = self.signal_id
        return d


# --------------------------------------------------------------------------
# document lane
# --------------------------------------------------------------------------


@dataclass
class Document:
    """Raw text plus enough metadata to point back into the original.

    `text` is the flattened, searchable form. `locator_map` lets an adapter
    translate a character offset back into something a human can find -- a
    PDF page, an email header, a CSV row.
    """

    source_id: str
    source_type: str  # "email" | "ics" | "pdf" | "csv" | "html" | "text"
    title: str
    text: str
    retrieved_at: int
    locator_map: list[tuple[int, str]] = field(default_factory=list)

    def locator_for(self, offset: int) -> str:
        """Nearest preceding landmark for a character offset."""
        best = "body"
        for start, name in self.locator_map:
            if start <= offset:
                best = name
            else:
                break
        return best

    def provenance_for(self, start: int, end: int) -> Provenance:
        pad = 45
        lo = max(0, start - pad)
        hi = min(len(self.text), end + pad)
        return Provenance(
            source_id=self.source_id,
            source_type=self.source_type,
            locator=self.locator_for(start),
            retrieved_at=self.retrieved_at,
            char_start=start,
            char_end=end,
            snippet=self.text[lo:hi].replace("\n", " ").strip(),
        )


@dataclass
class ItineraryItem:
    """One leg of the user's actual plans, extracted from a document.

    Every field that matters is paired with the provenance of the span it was
    read from, which is what lets the dashboard highlight the source.
    """

    kind: str  # "transit" | "flight" | "rail" | "appointment"
    label: str
    depart_at: int | None = None  # unix seconds
    arrive_at: int | None = None
    origin: str = ""
    destination: str = ""
    route_hint: str = ""  # route/train/flight number as printed
    confidence: float = 0.0
    # Recoverability context. A missed bus that runs every 8 minutes is an
    # inconvenience; a missed train that runs once an hour is a ruined day.
    # The rules baseline has no notion of headway, which is precisely where
    # we expect a reasoning model to earn its place -- see EVAL.md.
    headway_min: int | None = None
    leg_role: str = "primary"  # "primary" | "backup" | "return"
    provenance: list[Provenance] = field(default_factory=list)
    field_provenance: dict[str, Provenance] = field(default_factory=dict)

    @property
    def item_id(self) -> str:
        basis = f"{self.kind}|{self.label}|{self.depart_at}|{self.route_hint}"
        return hashlib.sha1(basis.encode()).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["item_id"] = self.item_id
        d["field_provenance"] = {
            k: v.to_dict() for k, v in self.field_provenance.items()
        }
        return d


# --------------------------------------------------------------------------
# triage (the model's structural output)
# --------------------------------------------------------------------------

Act = Literal["SUPPRESS", "LOG", "NOTIFY", "INTERVENE"]
ACT_ORDER: dict[str, int] = {"SUPPRESS": 0, "LOG": 1, "NOTIFY": 2, "INTERVENE": 3}


@dataclass
class Verdict:
    """The triage decision. `act` is not prose -- it is the branch the
    pipeline takes next, and nothing downstream runs without it."""

    act: Act
    severity: int  # 0-3
    reason_code: str
    confidence: float
    affects_item_id: str | None = None
    rationale: str = ""
    backend: str = ""
    latency_ms: int = 0
    # The audit trail. Exactly what the triage stage was shown and exactly
    # what it sent back, kept so a reviewer can check the decision rather
    # than take our word for it. Empty for arms that make no call.
    request_json: str = ""
    response_raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    """A Signal joined to its Verdict and the itinerary leg it threatens.
    This is the unit the dashboard renders and the eval scores."""

    signal: Signal
    verdict: Verdict
    item: ItineraryItem | None = None
    lead_time_s: int | None = None  # vs the official alert, once one lands
    official_alert_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal.to_dict(),
            "verdict": self.verdict.to_dict(),
            "item": self.item.to_dict() if self.item else None,
            "lead_time_s": self.lead_time_s,
            "official_alert_id": self.official_alert_id,
        }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)
