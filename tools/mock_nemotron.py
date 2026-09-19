"""An offline stand-in for NVIDIA's chat-completions endpoint.

Purpose: prove the HTTP client, the strict-JSON contract, the loose-JSON
parser, the schema validator and the clamp layer all work, without a network
or an API key. At the hackathon you delete one environment variable and the
same code talks to the real thing.

    python tools/mock_nemotron.py --port 8900
    export DISPATCH_NVIDIA_URL=http://127.0.0.1:8900/v1/chat/completions
    export NVIDIA_API_KEY=mock
    python -m dispatch.cli eval

This is NOT a model and must never be reported as one. It follows the prompt
contract by hand so the transport is exercised. `dispatch eval` detects the
endpoint override and stamps "endpoint 127.0.0.1:8900, not NVIDIA" on the row,
so a mock run cannot masquerade as a Nemotron result in EVAL.md.

Two switches make it useful for testing failure handling rather than only the
happy path:

    --garble N   every Nth reply comes back as prose wrapped in a markdown
                 fence, to prove the parser survives it
    --fail N     every Nth request returns HTTP 500, to prove the fallback
                 path engages and gets counted
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"n": 0, "garble": 0, "fail": 0}


def triage_reply(payload: dict) -> dict:
    """Apply the documented triage rules to the signal payload.

    Mirrors TRIAGE_SYSTEM in dispatch/llm.py. Kept deliberately simple: this
    exists to produce contract-shaped output, not to be clever.
    """
    sig = payload.get("signal", {})
    ev = sig.get("evidence", {}) or {}
    slack = payload.get("slack_minutes")
    matched = payload.get("matched_item_id")
    kind = sig.get("kind", "")

    itinerary = {i.get("item_id"): i for i in payload.get("itinerary", [])}
    leg = itinerary.get(matched) or {}
    headway = leg.get("headway_min")
    role = leg.get("leg_role", "primary")

    # A vehicle sitting at a stop with essentially no drift, away from either
    # end of the route, is holding for schedule rather than broken.
    # Note the feature used: current_status plus a stop id, NOT drift_m.
    # drift_m measures distance from an anchor set before the vehicle stopped,
    # so a bus decelerating into a stop reads 50m of "drift" and a threshold on
    # it misfires. jitter_m is the honest stationarity measure, but for this
    # decision the feed's own status field is the right evidence.
    holding = (
        kind == "STALL"
        and ev.get("status") == "STOPPED_AT"
        and bool(ev.get("stop_id"))
    )
    if holding:
        return {
            "act": "SUPPRESS",
            "severity": 0,
            "reason_code": "TIMEPOINT_HOLD",
            "confidence": 0.72,
            "affects_item_id": None,
            "rationale": "stopped at a mid-route stop with no drift",
        }

    if ev.get("scheduled_layover"):
        return {
            "act": "SUPPRESS", "severity": 0, "reason_code": "TERMINAL_LAYOVER",
            "confidence": 0.9, "affects_item_id": None,
            "rationale": "layover at terminal",
        }

    if not matched:
        return {
            "act": "LOG", "severity": 1, "reason_code": "NOT_ON_ITINERARY",
            "confidence": 0.8, "affects_item_id": None,
            "rationale": "no itinerary leg on this route",
        }

    unrecoverable = headway is not None and headway >= 45
    if role in ("backup", "return"):
        act, sev, code = "NOTIFY", 2, "ALTERNATIVE_EXISTS"
    elif slack is not None and (slack <= 15 or unrecoverable):
        act, sev, code = "INTERVENE", 3, "SLACK_EXHAUSTED"
    else:
        act, sev, code = "NOTIFY", 2, "CONNECTION_AT_RISK"

    return {
        "act": act, "severity": sev, "reason_code": code, "confidence": 0.78,
        "affects_item_id": matched,
        "rationale": f"{kind.lower()} threatens leg with {slack} min slack",
    }


def extract_reply(document_text: str) -> dict:
    """Return legs with verbatim quotes, as the extraction contract requires.

    Quotes are sliced straight out of the document so they verify. That is the
    point of the contract: the client locates each quote itself and drops any
    field it cannot find, so a fabricated span cannot become provenance.
    """
    legs = []
    for m in re.finditer(r"\b(?:Route|Rte|Bus)\s*#?\s*(\d{1,3}[A-Z]?)\b",
                         document_text, re.I):
        legs.append({
            "kind": "transit",
            "label": f"Route {m.group(1)}",
            "route_hint": m.group(1),
            "quotes": {"route_hint": m.group(1)},
        })
    for m in re.finditer(r"\b(?:Train|Trn)\s*#?\s*(\d{1,4})\b", document_text, re.I):
        legs.append({
            "kind": "rail",
            "label": f"Train {m.group(1)}",
            "route_hint": m.group(1),
            "quotes": {"route_hint": m.group(1)},
        })
    return {"legs": legs}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep the console readable
        pass

    def do_POST(self) -> None:
        STATE["n"] += 1
        n = STATE["n"]

        if STATE["fail"] and n % STATE["fail"] == 0:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}

        messages = body.get("messages", [])
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")

        if "extract" in system.lower() or "itinerary legs" in system.lower():
            content = json.dumps(extract_reply(user))
        else:
            try:
                payload = json.loads(user)
            except ValueError:
                payload = {}
            content = json.dumps(triage_reply(payload))

        if STATE["garble"] and n % STATE["garble"] == 0:
            # Exactly the failure the loose parser exists for.
            content = (
                "Sure! Here's my analysis:\n\n```json\n" + content + "\n```\n"
                "Let me know if you'd like more detail."
            )

        reply = json.dumps({
            "id": f"mock-{n}",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def serve(port: int, garble: int = 0, fail: int = 0) -> ThreadingHTTPServer:
    STATE.update(n=0, garble=garble, fail=fail)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--garble", type=int, default=0,
                    help="every Nth reply is fenced prose")
    ap.add_argument("--fail", type=int, default=0,
                    help="every Nth request returns HTTP 500")
    args = ap.parse_args()
    httpd = serve(args.port, args.garble, args.fail)
    print(f"mock nemotron on http://127.0.0.1:{args.port}/v1/chat/completions")
    print("  export DISPATCH_NVIDIA_URL="
          f"http://127.0.0.1:{args.port}/v1/chat/completions")
    print("  export NVIDIA_API_KEY=mock")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
