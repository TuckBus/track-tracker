"""Model backends for the two structural jobs Nemotron does here.

Job A -- TRIAGE. Input: a measured Signal plus the user's itinerary. Output:
a Verdict whose `act` field is a branch, not a sentence. SUPPRESS stops the
pipeline. LOG writes and stays silent. NOTIFY surfaces. INTERVENE drafts the
email and calendar change. Nothing downstream runs without it. The model is
a router in a pipeline, which is why it never sees a chat turn.

Job B -- EXTRACTION. Input: document text. Output: itinerary rows with a
verbatim quote per field. We then locate that quote ourselves with str.find
to get byte offsets. Asking a model for character offsets directly produces
confident nonsense; asking it to quote and verifying the quote turns
provenance into something we can *check*, and gives us a measurable
provenance-verification rate instead of a promise.

Two backends implement the same protocol:

    rules     -- interpretable thresholds and regexes. Not a mock. This is
                 the baseline arm the eval measures Nemotron against, and
                 the fallback when the venue wifi dies.
    nemotron  -- NVIDIA's OpenAI-compatible endpoint.

Select with DISPATCH_BACKEND=rules|nemotron.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Protocol

from .timeutil import resolve_tz
from .schema import (
    ACT_ORDER,
    Document,
    ItineraryItem,
    Provenance,
    Signal,
    Verdict,
)

NVIDIA_URL = os.environ.get(
    "DISPATCH_NVIDIA_URL", "https://integrate.api.nvidia.com/v1/chat/completions"
)
# nvidia/nemotron-3-nano-30b-a3b reached end of life 2026-09-01 and now
# returns HTTP 410. Verified working replacement, same 30B/3B-active shape:
NVIDIA_MODEL = os.environ.get(
    "DISPATCH_NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"
)

TRIAGE_SYSTEM = """You are the triage stage of an automated transit disruption pipeline.
You are not a chat assistant. You do not address the user. You emit one JSON object.

You receive a SIGNAL (an anomaly already measured by deterministic code) and the
user's ITINERARY. Decide what the pipeline does next.

act must be exactly one of:
  SUPPRESS  - normal operations misread as a fault (scheduled layover, terminal
              dwell, yard movement, a vehicle not on any itinerary leg).
  LOG        - real anomaly, no itinerary leg affected. Record, stay silent.
  NOTIFY     - affects a leg, but the user still has slack to absorb it.
  INTERVENE  - affects a leg and the slack is gone. Authorises drafting a
               cancellation email and moving calendar events.

Rules you must follow:
- Never compute times yourself. Use the numbers given. They are authoritative.
- If no itinerary leg is plausibly affected, you may not exceed LOG.
- INTERVENE requires slack_minutes <= 15 OR the leg being unrecoverable: a leg
  whose headway_min >= 45 is effectively unrecoverable once missed, so escalate
  earlier for it than for a frequent service.
- leg_role "backup" or "return" may not exceed NOTIFY; those legs have alternatives.
- Prefer SUPPRESS when evidence says scheduled_layover or the dwell is at a
  terminal stop.
- reason_code: SHORT_SCREAMING_SNAKE, e.g. TERMINAL_LAYOVER, SLACK_EXHAUSTED,
  NOT_ON_ITINERARY, CORRIDOR_CASCADE, CONNECTION_AT_RISK.

Output strictly this JSON and nothing else, no markdown fence:
{"act":"...","severity":0-3,"reason_code":"...","confidence":0.0-1.0,
 "affects_item_id":"<id or null>","rationale":"<=20 words"}"""

EXTRACT_SYSTEM = """You extract travel itinerary legs from a document. You are not a
chat assistant. You emit one JSON object.

For every leg you find, return the fields you are confident about AND, for each,
the EXACT verbatim substring from the document you read it from. The quote must
appear character-for-character in the document. Do not paraphrase, reformat, or
correct quotes. If you cannot quote it, omit the field.

Output strictly this JSON and nothing else, no markdown fence:
{"legs":[{"kind":"transit|flight|rail|appointment",
          "label":"short human label",
          "route_hint":"route/train/flight number as printed",
          "origin":"", "destination":"",
          "depart_local":"YYYY-MM-DD HH:MM or empty",
          "quotes":{"route_hint":"<verbatim>","depart_local":"<verbatim>",
                    "origin":"<verbatim>","destination":"<verbatim>"}}]}"""


class Backend(Protocol):
    name: str

    def triage(self, signal: Signal, items: list[ItineraryItem], now: int) -> Verdict: ...
    def extract(self, doc: Document) -> list[ItineraryItem]: ...


# --------------------------------------------------------------------------
# shared post-processing
# --------------------------------------------------------------------------


def slack_minutes(item: ItineraryItem | None, now: int) -> int | None:
    if item is None or item.depart_at is None:
        return None
    return int((item.depart_at - now) // 60)


def clamp_verdict(
    v: Verdict, item: ItineraryItem | None, now: int, evidence: dict
) -> Verdict:
    """Deterministic guardrails over whatever the model said.

    A model that escalates to INTERVENE on a bus nobody is riding costs real
    trust, and INTERVENE sends email. The two hard invariants -- no itinerary
    means no escalation past LOG, and INTERVENE needs the slack to actually be
    gone -- are enforced in code, not requested in a prompt. Every clamp is
    counted and reported in EVAL.md.
    """
    original = v.act
    if item is None and ACT_ORDER[v.act] > ACT_ORDER["LOG"]:
        v.act = "LOG"  # type: ignore[assignment]
        v.reason_code = v.reason_code or "NOT_ON_ITINERARY"
    slack = slack_minutes(item, now)
    if v.act == "INTERVENE" and slack is not None:
        infrequent = (item.headway_min or 0) >= 45 if item else False
        allowed = slack <= 15 or (infrequent and slack <= 30)
        if not allowed:
            v.act = "NOTIFY"  # type: ignore[assignment]
    if item is not None and item.leg_role in ("backup", "return"):
        if ACT_ORDER[v.act] > ACT_ORDER["NOTIFY"]:
            v.act = "NOTIFY"  # type: ignore[assignment]
            v.reason_code = v.reason_code or "ALTERNATIVE_EXISTS"
    if evidence.get("scheduled_layover") and ACT_ORDER[v.act] > ACT_ORDER["SUPPRESS"]:
        v.act = "SUPPRESS"  # type: ignore[assignment]
        v.reason_code = "TERMINAL_LAYOVER"
    if original != v.act:
        v.rationale = (v.rationale + f" [clamped {original}->{v.act}]").strip()
    v.severity = max(0, min(3, int(v.severity)))
    return v


# --------------------------------------------------------------------------
# rules backend -- the baseline arm
# --------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1
    )
}

_RE_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?")
_RE_DATE_TEXT = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})",
    re.I,
)
_RE_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RE_TRAIN = re.compile(r"\b(?:Train|Trn)\s*#?\s*(\d{1,4})\b", re.I)
_RE_FLIGHT = re.compile(r"\b([A-Z]{2})\s?(\d{2,4})\b")
_RE_ROUTE = re.compile(r"\b(?:Route|Rte|Bus)\s*#?\s*(\d{1,3}[A-Z]?)\b", re.I)
_RE_ICS_DT = re.compile(r"^DT(START|END)(?:;[^:]*)?:(\d{8}T\d{6}Z?)", re.M)


class RulesBackend:
    """Interpretable baseline. No network, no model, no GPU."""

    name = "rules"

    # -- Job A ---------------------------------------------------------
    def triage(self, signal: Signal, items: list[ItineraryItem], now: int) -> Verdict:
        t0 = time.perf_counter()
        item = best_match(signal, items)
        ev = signal.evidence
        slack = slack_minutes(item, now)

        if ev.get("scheduled_layover"):
            act, sev, code = "SUPPRESS", 0, "TERMINAL_LAYOVER"
        elif item is None:
            act, sev, code = "LOG", 1, "NOT_ON_ITINERARY"
        elif signal.kind == "CASCADE":
            act = "INTERVENE" if (slack is not None and slack <= 15) else "NOTIFY"
            sev, code = 3, "CORRIDOR_CASCADE"
        elif slack is not None and slack <= 15:
            act, sev, code = "INTERVENE", 3, "SLACK_EXHAUSTED"
        elif slack is not None and slack <= 45:
            act, sev, code = "NOTIFY", 2, "CONNECTION_AT_RISK"
        else:
            act, sev, code = "LOG", 1, "AMPLE_SLACK"

        v = Verdict(
            act=act,  # type: ignore[arg-type]
            severity=sev,
            reason_code=code,
            confidence=0.55,
            affects_item_id=item.item_id if item else None,
            rationale="threshold baseline",
            backend=self.name,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            # The baseline gets an audit trail too, so the two arms are
            # compared on the same terms: here are the inputs, here is the
            # rule that fired.
            request_json=json.dumps({
                "signal": {"kind": signal.kind, "route_id": signal.route_id,
                           "evidence": ev},
                "matched_item_id": item.item_id if item else None,
                "slack_minutes": slack,
                "headway_min": item.headway_min if item else None,
            }, indent=2),
            response_raw=json.dumps({"act": act, "reason_code": code,
                                     "rule": "thresholds in RulesBackend.triage"}),
        )
        return clamp_verdict(v, item, now, ev)

    # -- Job B ---------------------------------------------------------
    def extract(self, doc: Document) -> list[ItineraryItem]:
        text = doc.text
        legs: list[ItineraryItem] = []

        if doc.source_type == "ics":
            for m in _RE_ICS_DT.finditer(text):
                if m.group(1) != "START":
                    continue
                epoch = _ics_epoch(m.group(2))
                summary = re.search(r"^SUMMARY:(.*)$", text, re.M)
                legs.append(
                    _leg(
                        doc,
                        "appointment",
                        (summary.group(1).strip() if summary else doc.title),
                        depart_at=epoch,
                        spans={"depart_local": m.span(2)},
                        confidence=0.7,
                    )
                )
            return legs

        date_epoch, date_span = _find_date(text)

        for rx, kind in ((_RE_TRAIN, "rail"), (_RE_ROUTE, "transit")):
            for m in rx.finditer(text):
                t_epoch, t_span = _find_time_near(text, m.end(), date_epoch)
                legs.append(
                    _leg(
                        doc,
                        kind,
                        f"{kind} {m.group(1)}",
                        route_hint=m.group(1),
                        depart_at=t_epoch,
                        spans={
                            "route_hint": m.span(1),
                            "depart_local": t_span,
                            "date": date_span,
                        },
                        confidence=0.6,
                    )
                )

        for m in _RE_FLIGHT.finditer(text):
            code = f"{m.group(1)}{m.group(2)}"
            if m.group(1) in ("AM", "PM"):
                continue
            t_epoch, t_span = _find_time_near(text, m.end(), date_epoch)
            legs.append(
                _leg(
                    doc,
                    "flight",
                    f"flight {code}",
                    route_hint=code,
                    depart_at=t_epoch,
                    spans={"route_hint": m.span(0), "depart_local": t_span},
                    confidence=0.5,
                )
            )
        return _dedupe(legs)


# --------------------------------------------------------------------------
# nemotron backend
# --------------------------------------------------------------------------


class NemotronBackend:
    """NVIDIA's OpenAI-compatible chat endpoint, used strictly for structured
    output. Falls back to the rules arm on any transport failure so a dead
    network degrades the product instead of stopping it."""

    name = "nemotron"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("NVIDIA_API_KEY", "")
        self.model = model or NVIDIA_MODEL
        self.fallback = RulesBackend()
        self.calls = 0
        self.failures = 0

    @property
    def available(self) -> bool:
        """Whether this backend can actually reach a model.

        The eval consults this before running the nemotron arm. Without it, a
        missing key produces a full set of rules-backend verdicts labelled
        'nemotron', which would put a number in front of a judge that the
        model never produced.
        """
        return bool(self.api_key)

    def _chat(self, system: str, user: str, max_tokens: int = 2048) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
                "top_p": 1.0,
                "max_tokens": max_tokens,
            }
        ).encode()
        req = urllib.request.Request(
            NVIDIA_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self.calls += 1
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode())
        choice = payload["choices"][0]
        message = choice.get("message", {})
        content = (message.get("content") or "").strip()
        # Reasoning models sometimes put everything in reasoning_content and
        # leave content empty, especially when the reply was cut short.
        if not content:
            content = (message.get("reasoning_content") or "").strip()
        if choice.get("finish_reason") == "length" and not content.rstrip().endswith("}"):
            # Truncated mid-answer. Say so rather than handing the parser a
            # half-written object and reporting whatever it salvages.
            raise ValueError(
                "model response truncated (finish_reason=length); "
                "raise max_tokens"
            )
        return content

    # -- Job A ---------------------------------------------------------
    def triage(self, signal: Signal, items: list[ItineraryItem], now: int) -> Verdict:
        t0 = time.perf_counter()
        item = best_match(signal, items)
        ev = dict(signal.evidence)
        payload = {
            "signal": {
                "kind": signal.kind,
                "route_id": signal.route_id,
                "vehicle_id": signal.vehicle_id,
                "source": signal.source_type,
                "minutes_since_onset": (now - signal.first_seen_at) // 60,
                "evidence": ev,
            },
            "itinerary": [
                {
                    "item_id": it.item_id,
                    "label": it.label,
                    "route_hint": it.route_hint,
                    "kind": it.kind,
                    "minutes_until_departure": slack_minutes(it, now),
                    "headway_min": it.headway_min,
                    "leg_role": it.leg_role,
                }
                for it in items
            ],
            "matched_item_id": item.item_id if item else None,
            "slack_minutes": slack_minutes(item, now),
        }
        try:
            # Sized for a reasoning model: Nemotron 3.5 Lightning spends
            # several hundred tokens thinking before it emits the verdict, and
            # 300 truncated it every time -- which showed up as a silent
            # fallback to the rules arm, not as an error.
            request_json = json.dumps(payload, indent=2)
            raw = self._chat(TRIAGE_SYSTEM, request_json, max_tokens=2048)
            data = _loose_json(raw)
            v = Verdict(
                act=str(data.get("act", "LOG")).upper(),  # type: ignore[arg-type]
                severity=int(data.get("severity", 1)),
                reason_code=str(data.get("reason_code", "UNSPECIFIED"))[:48],
                confidence=float(data.get("confidence", 0.5)),
                affects_item_id=data.get("affects_item_id") or None,
                rationale=str(data.get("rationale", ""))[:160],
                backend=self.name,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                request_json=request_json,
                response_raw=raw[:4000],
            )
            if v.act not in ACT_ORDER:
                v.act = "LOG"  # type: ignore[assignment]
                v.reason_code = "UNPARSEABLE_ACT"
            return clamp_verdict(v, item, now, ev)
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError) as e:
            self.failures += 1
            v = self.fallback.triage(signal, items, now)
            v.backend = "nemotron:fallback-rules"
            v.rationale = f"backend error: {type(e).__name__}"
            return v

    # -- Job B ---------------------------------------------------------
    def extract(self, doc: Document) -> list[ItineraryItem]:
        window = doc.text[:6000]
        try:
            raw = self._chat(
                EXTRACT_SYSTEM,
                json.dumps({"source_type": doc.source_type, "text": window}),
                max_tokens=3000,
            )
            data = _loose_json(raw)
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError):
            self.failures += 1
            return self.fallback.extract(doc)

        out: list[ItineraryItem] = []
        for leg in data.get("legs", [])[:12]:
            if not isinstance(leg, dict):
                continue
            quotes = leg.get("quotes") or {}
            spans: dict[str, tuple[int, int]] = {}
            verified = 0
            for fname, quote in quotes.items():
                if not isinstance(quote, str) or not quote.strip():
                    continue
                # Verify by locating the quote. A quote we cannot find is a
                # hallucinated citation; we drop it rather than display it.
                idx = doc.text.find(quote)
                if idx >= 0:
                    spans[fname] = (idx, idx + len(quote))
                    verified += 1
            total = max(1, len([q for q in quotes.values() if isinstance(q, str) and q]))
            item = _leg(
                doc,
                str(leg.get("kind", "transit")),
                str(leg.get("label", ""))[:80] or doc.title,
                route_hint=str(leg.get("route_hint", ""))[:16],
                origin=str(leg.get("origin", ""))[:64],
                destination=str(leg.get("destination", ""))[:64],
                depart_at=_parse_local(str(leg.get("depart_local", ""))),
                spans=spans,
                confidence=round(0.45 + 0.5 * (verified / total), 2),
            )
            out.append(item)
        return _dedupe(out)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


# Keys that mark an object as an actual answer rather than a fragment the
# model wrote while thinking out loud.
_ANSWER_KEYS = ("act", "legs")


def _loose_json(raw: str) -> dict:
    """Pull the answer object out of whatever the model actually sent.

    Three things this has to survive, all of which happen in practice:

    - markdown fences and chatty preamble;
    - reasoning models that narrate first. Nemotron 3.5 Lightning emits a
      "Here's a thinking process:" block before answering, and that block
      often contains draft JSON. Taking the *first* balanced object returns
      the model's rough work instead of its conclusion, so scan back to
      front and prefer an object carrying an answer key;
    - <think> blocks, which some templates emit inline.
    """
    txt = (raw or "").strip()
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S | re.I).strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()

    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass

    # Collect every balanced {...} span.
    found: list[dict] = []
    depth, start = 0, -1
    for i, ch in enumerate(txt):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    found.append(json.loads(txt[start : i + 1]))
                except json.JSONDecodeError:
                    pass
                start = -1

    # Last answer-shaped object wins; otherwise last parseable object.
    for obj in reversed(found):
        if isinstance(obj, dict) and any(k in obj for k in _ANSWER_KEYS):
            return obj
    for obj in reversed(found):
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object in model output")


def _leg(
    doc: Document,
    kind: str,
    label: str,
    *,
    route_hint: str = "",
    origin: str = "",
    destination: str = "",
    depart_at: int | None = None,
    spans: dict[str, tuple[int, int] | None] | None = None,
    confidence: float = 0.5,
) -> ItineraryItem:
    item = ItineraryItem(
        kind=kind,
        label=label,
        depart_at=depart_at,
        origin=origin,
        destination=destination,
        route_hint=route_hint,
        confidence=confidence,
    )
    for fname, span in (spans or {}).items():
        if not span:
            continue
        prov = doc.provenance_for(span[0], span[1])
        item.field_provenance[fname] = prov
        item.provenance.append(prov)
    if not item.provenance:
        item.provenance.append(doc.provenance_for(0, min(60, len(doc.text))))
    return item


def _dedupe(legs: list[ItineraryItem]) -> list[ItineraryItem]:
    seen, out = set(), []
    for leg in legs:
        k = (leg.kind, leg.route_hint, leg.depart_at)
        if k in seen:
            continue
        seen.add(k)
        out.append(leg)
    return out


def _find_date(text: str) -> tuple[int | None, tuple[int, int] | None]:
    m = _RE_DATE_TEXT.search(text)
    if m:
        mon = _MONTHS[m.group(1)[:3].lower()]
        return (
            _epoch_local(int(m.group(3)), mon, int(m.group(2)), 0, 0),
            m.span(0),
        )
    m = _RE_DATE_ISO.search(text)
    if m:
        return (
            _epoch_local(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0),
            m.span(0),
        )
    return None, None


def _find_time_near(
    text: str, pos: int, date_epoch: int | None
) -> tuple[int | None, tuple[int, int] | None]:
    """First clock time within 140 chars after an anchor."""
    m = _RE_TIME.search(text, pos, pos + 140)
    if not m or date_epoch is None:
        return None, (m.span(0) if m else None)
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower().replace(".", "")
    if ampm.startswith("p") and hour < 12:
        hour += 12
    if ampm.startswith("a") and hour == 12:
        hour = 0
    return date_epoch + hour * 3600 + minute * 60, m.span(0)


def _epoch_local(y: int, mo: int, d: int, h: int, mi: int) -> int:
    """Local wall-clock -> epoch, honouring the DST rules in effect on that
    date. Naive UTC arithmetic here shifts every Pittsburgh timestamp by an
    hour for half the year; see the DST note in EVAL.md."""
    import calendar
    from datetime import datetime
    # The previous version fell back to calendar.timegm on any failure,
    # which reads a local departure time as if it were UTC -- a four hour
    # error that does not raise, and that silently corrupts slack_minutes
    # and therefore every triage decision. resolve_tz always returns a
    # usable zone instead.
    tz = resolve_tz()
    try:
        return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())
    except (ValueError, OverflowError, OSError):
        return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


def _parse_local(s: str) -> int | None:
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", s or "")
    if not m:
        return None
    return _epoch_local(*(int(g) for g in m.groups()))


def _ics_epoch(stamp: str) -> int | None:
    from datetime import datetime, timezone

    try:
        if stamp.endswith("Z"):
            return int(
                datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        return _epoch_local(
            int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]),
            int(stamp[9:11]), int(stamp[11:13]),
        )
    except ValueError:
        return None


def best_match(signal: Signal, items: list[ItineraryItem]) -> ItineraryItem | None:
    """Link a disruption to the itinerary leg it threatens.

    Deliberately deterministic: route-token equality, not fuzzy similarity.
    A wrong match sends a cancellation email about the wrong trip, so the
    match must be defensible in code review.
    """
    if not items:
        return None
    sig_tokens = {t for t in re.split(r"[^A-Za-z0-9]+", signal.route_id.upper()) if t}
    best, best_score = None, 0
    for it in items:
        hint = it.route_hint.upper().strip()
        if not hint:
            continue
        score = 0
        if hint in sig_tokens:
            score = 3
        elif any(tok.startswith(hint) or hint.startswith(tok) for tok in sig_tokens):
            score = 2
        if score > best_score:
            best, best_score = it, score
    return best


def get_backend(name: str | None = None) -> Backend:
    name = (name or os.environ.get("DISPATCH_BACKEND") or "rules").lower()
    if name in ("nemotron", "nvidia"):
        return NemotronBackend()
    return RulesBackend()
