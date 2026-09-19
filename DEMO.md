# Demo

Two minutes, in this order. Every step has been run; nothing here is aspirational.

## Before you stand up

```bash
python3 -m dispatch.cli verify
```

Rebuilds the world from a fixed seed, runs the tests, and re-derives every
number in the README and EVAL.md. If it ends with `all checks passed`, the
demo will work. Then:

```bash
python3 -m dispatch.cli serve
```

Open `http://localhost:8000`. Leave it on **Now** with the replay finished.

Have a terminal open on a second window. If the browser dies, the terminal
demo below stands on its own.

---

## The two minutes

**1. The answer, not a dashboard.** (15s)

Point at the top of Now. It says something like *"You're going to miss Route
61C."* Say: most transit tools show you where the buses are and leave the
thinking to you. This one has already read your calendar and your confirmation
emails, so it can answer the only question you have.

**2. Detection plus prediction together.** (25s)

Click the red `intervene` event. Read the evidence: dwell time, drift, jitter,
the stop it died at. Then scroll to **Next vehicle on this route** — that is
the arrival predictor telling you what is behind the broken bus. Say: detection
knows which bus died, prediction knows what is following, and neither is useful
on its own.

**3. Provenance you can check.** (25s)

Still in that panel, scroll to **Where this came from** and click the `email`
card. The actual confirmation email opens with the characters that produced the
claim highlighted. Say: every field in this app can be walked back to the bytes
it came from, and there is a test that fails if a highlighted span stops
matching the value it supports.

**4. Hand them the laptop.** (20s)

Go to **Trips**, scroll to the dropzone, and let a judge drop in one of their
own confirmation emails. It parses with the same adapters and the extracted
legs appear with working provenance. This is the moment that separates a demo
from a fixture.

**5. The evidence.** (25s)

Go to **Evidence**. Three arms on one recording: no triage gets F1 0.57, the
rules baseline 0.89. The gap is what the triage decision is worth. Then say the
number that matters: median **6.7 minutes** ahead of Pittsburgh Regional
Transit's own alerts, measured against their published alert feed.

**6. Close on a failure.** (10s)

"Our arrival predictor is accurate to about 15 seconds on a moving bus and
about 24 minutes on a broken one. That hundredfold gap is why disruption
detection is a separate system instead of something we infer from a drifting
ETA. And the first time we scored the model it showed no improvement at all,
which turned out to be a flaw in our own test fixture rather than the model."

Judges remember the team that volunteered its own failure.

---

## Questions you will get

**"Is this real data?"** The feeds are real and need no API key — PRT publishes
GTFS-Realtime openly. `dispatch doctor` proves reachability live.
`serve --live` maps the whole system. The *eval* runs on a synthetic world
because scoring needs ground truth, and EVAL.md says so in a section called
"honest limits of this number."

**"Why not just use ETAs to spot problems?"** PREDICTIONS.md, the disrupted
row. Prediction degrades exactly when you need it.

**"Did Nemotron actually run?"** If `EVAL.md` mentions the fallback, say so
plainly: the model arm fell back to the rules baseline, and we know because we
built a counter that tells us when our own evidence is fake. That counter
caught a real failure during the build — a model that hit end-of-life
mid-project and returned HTTP 410.

**"What about flights?"** Searchable, from your documents, not live-tracked.
Aviation was deliberately excluded: OpenSky meters requests in credits and is
non-commercial only, so a product built on it would need a licensed feed. The
UI labels every result's source rather than implying coverage it does not have.

**"Is the 6.7 minutes a product claim?"** No, and we will not pretend it is.
The synthetic world assumes an 11–24 minute agency alert lag. `dispatch record`
exists to capture a real week and the eval re-runs unchanged against it.

---

## If something breaks

**Browser blank or map empty.** Run the terminal demo instead:

```bash
python3 -m dispatch.cli replay
```

One screen: the seven itinerary legs and which document each came from, the
agency alerts, every finding with its triage decision and minutes of lead, and
the drafted messages. It cannot fail to render and it shows all three tracks.

**No basemap on the Map screen.** Expected on a blocked network. It falls back
to drawing from coordinates and says so. Positions are unaffected.

**Nemotron row empty.** `export NVIDIA_API_KEY=...` and re-run
`dispatch eval --out EVAL.md`. Without a key the arm reports "did not run"
rather than quietly scoring the fallback.

**Venue wifi gone entirely.** Everything except `serve --live` and the model
backend works offline by design. The replay, both evals, the tests and the
whole UI need no network.
