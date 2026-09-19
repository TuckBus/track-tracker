"""The orchestrator: telemetry in, Findings out.

Flow for every snapshot:

    bytes -> adapter -> Observation[]      (format-agnostic from here down)
          -> Detector -> Signal[]          (arithmetic, no model)
          -> backend.triage -> Verdict      (the model, as a router)
          -> clamp -> act                   (invariants in code)
          -> INTERVENE? draft artifacts     (never sent, only drafted)

Official alerts are captured but never fed to detection. They are the
measuring stick for lead time, and using them as input would make the
headline number circular.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from .detect import Detector
from .llm import Backend, best_match, get_backend
from .schema import (
    Document,
    Finding,
    ItineraryItem,
    Observation,
    OfficialAlert,
    Signal,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
  signal_id TEXT PRIMARY KEY, kind TEXT, route_id TEXT, vehicle_id TEXT,
  source_type TEXT, detected_at INT, first_seen_at INT, act TEXT,
  severity INT, reason_code TEXT, confidence REAL, backend TEXT,
  latency_ms INT, item_id TEXT, lead_time_s INT, payload TEXT
);
CREATE TABLE IF NOT EXISTS itinerary (
  item_id TEXT PRIMARY KEY, kind TEXT, label TEXT, depart_at INT,
  route_hint TEXT, confidence REAL, payload TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, source_type TEXT, published_at INT,
  effect TEXT, cause TEXT, header TEXT, route_ids TEXT
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, created_at INT,
  channel TEXT, subject TEXT, body TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS idx_find_detected ON findings(detected_at DESC);
"""


class Store:
    def __init__(self, path: str = "dispatch.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def save_finding(self, f: Finding) -> None:
        s, v = f.signal, f.verdict
        self.conn.execute(
            "INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s.signal_id, s.kind, s.route_id, s.vehicle_id, s.source_type,
                s.detected_at, s.first_seen_at, v.act, v.severity, v.reason_code,
                v.confidence, v.backend, v.latency_ms,
                f.item.item_id if f.item else None, f.lead_time_s,
                json.dumps(f.to_dict(), default=str),
            ),
        )
        self.conn.commit()

    def save_itinerary(self, items: list[ItineraryItem]) -> None:
        for it in items:
            self.conn.execute(
                "INSERT OR REPLACE INTO itinerary VALUES (?,?,?,?,?,?,?)",
                (it.item_id, it.kind, it.label, it.depart_at, it.route_hint,
                 it.confidence, json.dumps(it.to_dict(), default=str)),
            )
        self.conn.commit()

    def save_alert(self, a: OfficialAlert) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?)",
            (a.alert_id, a.source_type, a.published_at, a.effect, a.cause,
             a.header, json.dumps(a.route_ids)),
        )
        self.conn.commit()

    def save_action(self, signal_id: str, channel: str, subject: str, body: str) -> None:
        self.conn.execute(
            "INSERT INTO actions (signal_id, created_at, channel, subject, body, status)"
            " VALUES (?,?,?,?,?,?)",
            (signal_id, int(time.time()), channel, subject, body, "DRAFTED"),
        )
        self.conn.commit()

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]


@dataclass
class Pipeline:
    backend: Backend = field(default_factory=get_backend)
    detector: Detector = field(default_factory=Detector)
    store: Store | None = None
    itinerary: list[ItineraryItem] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    official: list[OfficialAlert] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    _alert_index: dict[str, OfficialAlert] = field(default_factory=dict, repr=False)

    # -- ingest -----------------------------------------------------------

    def load_documents(self, docs: list[Document]) -> list[ItineraryItem]:
        """Xtract lane. Extraction runs once per document, not per snapshot."""
        items: list[ItineraryItem] = []
        for doc in docs:
            items.extend(self.backend.extract(doc))
        # Keep the most confident reading of each leg.
        by_key: dict[tuple, ItineraryItem] = {}
        for it in items:
            k = (it.kind, it.route_hint, it.depart_at)
            if k not in by_key or it.confidence > by_key[k].confidence:
                by_key[k] = it
        self.itinerary = sorted(
            by_key.values(), key=lambda i: (i.depart_at or 1 << 62)
        )
        if self.store:
            self.store.save_itinerary(self.itinerary)
        return self.itinerary

    def observe(
        self,
        observations: list[Observation],
        alerts: list[OfficialAlert],
        now: int,
    ) -> list[Finding]:
        """One snapshot through the whole chain."""
        # A GTFS-RT alerts feed republishes every currently-active alert on
        # every poll, so a 3-hour replay sees the same alert ~500 times. Keep
        # the earliest sighting of each id: that timestamp is when the agency
        # actually went public, which is the only thing lead time can be
        # measured against.
        for a in alerts:
            known = self._alert_index.get(a.alert_id)
            if known is None:
                self._alert_index[a.alert_id] = a
                self.official.append(a)
                if self.store:
                    self.store.save_alert(a)
            elif a.published_at < known.published_at:
                known.published_at = a.published_at
                if self.store:
                    self.store.save_alert(known)

        out: list[Finding] = []
        for sig in self.detector.ingest(observations, now):
            verdict = self.backend.triage(sig, self.itinerary, now)
            item = (
                next(
                    (i for i in self.itinerary if i.item_id == verdict.affects_item_id),
                    None,
                )
                or best_match(sig, self.itinerary)
            )
            finding = Finding(signal=sig, verdict=verdict, item=item)
            finding.lead_time_s, finding.official_alert_id = self._lead_time(sig)
            if verdict.act == "INTERVENE":
                self._draft(finding)
            self.findings.append(finding)
            out.append(finding)
            if self.store:
                self.store.save_finding(finding)
        return out

    # -- lead time --------------------------------------------------------

    def _lead_time(self, sig: Signal) -> tuple[int | None, str | None]:
        """Seconds between our detection and the agency's own alert.

        Positive means we were first. Only counts alerts on the same route
        published after we fired; anything else is not the same event.
        """
        best, best_gap = None, None
        for a in self.official:
            if sig.route_id and sig.route_id not in a.route_ids:
                continue
            if a.published_at < sig.detected_at:
                continue
            gap = a.published_at - sig.detected_at
            if best_gap is None or gap < best_gap:
                best, best_gap = a, gap
        return (best_gap, best.alert_id if best else None)

    def reconcile_alerts(self) -> None:
        """Re-run lead-time matching after a replay finishes, so signals that
        fired before their alert arrived get credited."""
        for f in self.findings:
            f.lead_time_s, f.official_alert_id = self._lead_time(f.signal)
            if self.store:
                self.store.save_finding(f)

    # -- intervention -----------------------------------------------------

    def _draft(self, f: Finding) -> None:
        """INTERVENE drafts. It never sends.

        Nothing leaves the machine without a human pressing a button. An
        autonomous agent that emails your colleagues on a model's judgement
        is a liability, not a feature, and the draft-plus-approve step costs
        the user two seconds.
        """
        item = f.item
        label = item.label if item else f.signal.route_id
        when = (
            time.strftime("%I:%M %p", time.localtime(item.depart_at)).lstrip("0")
            if item and item.depart_at
            else "your next leg"
        )
        mins = (f.signal.detected_at - f.signal.first_seen_at) // 60
        subject = f"Running late: {label}"
        body = (
            f"Heads up - I'm likely to miss {label} at {when}.\n\n"
            f"Dispatch detected {f.signal.kind.lower().replace('_', ' ')} on "
            f"{f.signal.route_id} ({mins} min and counting) "
            f"{mins} minutes before the agency published anything.\n\n"
            f"I'll follow up with a new time shortly."
        )
        draft = {
            "signal_id": f.signal.signal_id,
            "channel": "email",
            "subject": subject,
            "body": body,
            "status": "DRAFTED - awaiting approval",
        }
        self.drafts.append(draft)
        if self.store:
            self.store.save_action(f.signal.signal_id, "email", subject, body)

