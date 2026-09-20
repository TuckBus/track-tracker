# Track Tracker

## Elevator pitch

Track Tracker is a parrot-bright, predictive dashboard for Pittsburgh Regional Transit buses. It turns live PRT vehicle data, stop information, and service alerts into a map, stop-level ETA checks, and early warnings that help riders leave with confidence instead of guessing whether their bus is really coming.

## About the project

### Inspiration

Public transit delays are frustrating because the information riders need is often fragmented: a posted alert may arrive after a delay has already started, while a bus moving unusually slowly can be an early signal that a trip is about to change. We wanted to build a focused Pittsburgh tool that watches the signals that matter to bus riders and makes them understandable at a glance.

The parrot mascot represents the product's personality: observant, vocal, and always keeping watch. Track Tracker is designed to feel more like a helpful travel companion than a cold operations console.

### What we learned

We learned how to work with GTFS-Realtime protobuf feeds, normalize live vehicle data for a browser dashboard, and combine measurable signals with posted service alerts without treating missing sensor data as proof of a delay. We also learned that an AI assistant is most useful when it is constrained by a clear policy: the model should explain evidence and identify possible delays, while deterministic fallback logic keeps the experience useful when AI is unavailable.

### How we built it

Track Tracker is a single Next.js application. The server polls PRT's GTFS-Realtime vehicle and service-alert feeds, parses the protobuf responses, and stores a normalized state. The React dashboard renders live bus positions and stops on a Leaflet map, provides searchable PRT stop and route checks, and surfaces posted alerts alongside early-warning predictions.

Nemotron analyzes compact route-level summaries, including slow buses, stationary buses, missing positions, and posted alerts. Its output is validated before it reaches the UI, and ETA results use a conservative one-minute safety buffer with a 180-minute cap. When the model is not configured or fails, the app falls back to deterministic behavior rather than hiding an error behind an optimistic status.

### Challenges we faced

The biggest challenge was distinguishing a real service problem from incomplete telemetry. A null speed is unknown data, not zero miles per hour, and a missing position does not mean a bus disappeared. We built explicit rules for those cases and required stronger repeated or multi-vehicle evidence before showing a network-level warning.

We also had to make the system resilient to provider and model failures. PRT requests can time out or return malformed data, and AI responses can fail schema validation. Isolating provider errors, validating model output, and keeping a deterministic fallback path let us preserve a useful dashboard while still making failures visible.

## Built With

Next.js, React, TypeScript, GTFS-Realtime, PRT GTFS-RT feeds, Leaflet, React Leaflet, NVIDIA Nemotron, Vercel, Upstash Redis, Vitest
