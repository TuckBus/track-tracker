# Dispatch

**Transit disruptions reach you before the agency admits them.**

Dispatch watches raw vehicle telemetry, works out which of your actual plans a
disruption threatens, and decides whether that is worth interrupting you for.
It reads your itinerary out of the confirmation emails, tickets and calendar
files you already have, so every alert points at a specific leg of a specific
trip, with a link back to the document it came from.

On a three-hour recording it surfaced every real disruption on the user's
itinerary with **zero false alarms**, a median of **6.7 minutes before
Pittsburgh Regional Transit published anything** about the same events.

SteelHacks XIII. Tracks: Beyond the Chatbot, Xtract, Seed Round.

---

## Team

| Name | Email |
|------|-------|
| _fill in_ | _fill in_ |
| _fill in_ | _fill in_ |

---

## Run it

Python 3.10+. **No dependencies** — standard library only, including the
GTFS-Realtime protobuf reader. Works on Windows without `tzdata`; run
`python3 -m dispatch.cli doctor` to see which timezone it resolved.

```bash
python3 -m dispatch.cli fixtures     # build a labelled 3-hour world
python3 -m dispatch.cli replay       # run it through the pipeline
python3 -m dispatch.cli eval         # score the triage decision
python3 -m dispatch.cli serve        # the dashboard, on :8000
python3 -m unittest discover -s tests
```

`fixtures` writes real GTFS-Realtime protobuf, byte-compatible with
`truetime.portauthority.org`, and a `truth.json` the pipeline never reads.

To run against the live feeds instead (PRT needs no API key):

```bash
python3 -m dispatch.cli doctor                       # are the feeds up
python3 -m dispatch.cli record --out recordings/fri  # start recording
python3 -m dispatch.cli replay --world recordings/fri
```

To use the model rather than the rules baseline:

```bash
export NVIDIA_API_KEY=nvapi-...
export DISPATCH_BACKEND=nemotron
python3 -m dispatch.cli eval --out EVAL.md
```

With no key, the rules backend runs and `eval` reports the nemotron arm as
*did not run* rather than scoring the fallback. To exercise the model client
offline:

```bash
python3 tools/mock_nemotron.py --port 8900 --garble 3 --fail 7 &
export DISPATCH_NVIDIA_URL=http://127.0.0.1:8900/v1/chat/completions
export NVIDIA_API_KEY=mock
```

---

## How it works

```
live feeds ─┐
            ├─► adapter ─► Observation ─► Detector ─► Signal ─┐
documents ──┘                            (arithmetic)         │
            └─► adapter ─► Document ─► extraction ─► Itinerary┤
                                                              ▼
                                                    Nemotron triage
                                                    (act = a branch)
                                                              │
                              SUPPRESS ◄── LOG ── NOTIFY ── INTERVENE
                                                              │
                                                    draft email + calendar
                                                    (drafted, never sent)
```

Two things are kept deliberately apart. **Detection is arithmetic** —
distances, dwell times, silence windows — because that is what a language
model is worst at and least auditable doing, and because it must be
reproducible from a recording. **Triage is judgement** — is this bus actually
broken, does it threaten a leg this person cannot recover, is it worth
interrupting them. That is where the model goes.

Official agency alerts are ingested but **never fed into detection**. They are
the measuring stick for lead time; using them as input would make the headline
number circular.

---

## Beyond the Chatbot

Nemotron has two jobs here, neither of them conversation.

**Job A — triage as a router.** Input: one measured signal plus the extracted
itinerary. Output: one JSON object whose `act` field *is the branch the
pipeline takes next*.

```json
{"act":"INTERVENE","severity":3,"reason_code":"SLACK_EXHAUSTED",
 "confidence":0.81,"affects_item_id":"c343469f5bf4",
 "rationale":"stall on 61C with 4 min slack before shift"}
```

`SUPPRESS` halts the pipeline. `LOG` records silently. `NOTIFY` surfaces.
`INTERVENE` authorises drafting. No downstream stage runs without it, and the
output is consumed by code, never shown as prose. The model never sees a chat
turn and never addresses the user.

The decision is genuinely hard rather than a formality: the detector fires on
18 scenarios of which only 4 should reach a human, so the work is *suppression*
— eleven buses sitting at their terminals, three holding for schedule, three
AVL dropouts that resolve themselves.

**Job B — extraction with verifiable provenance.** The model returns, for each
field, the *verbatim substring* it read the value from. The code then locates
that quote itself with `str.find` and drops any field it cannot find. Asking a
model for character offsets produces confident nonsense; asking it to quote and
then checking the quote turns provenance into something falsifiable, and gives
a measurable verification rate instead of a promise.

**Invariants live in code, not in the prompt.** A model asked nicely not to
escalate will sometimes escalate anyway, so `clamp_verdict` enforces the limits
after it returns: no escalation above `LOG` without a matched itinerary leg,
terminal-layover evidence forces `SUPPRESS`, backup legs cap at `NOTIFY`. Two
tests cover exactly this.

### Evidence

Full report in [EVAL.md](EVAL.md). One recording, one extracted itinerary; the
only variable is the triage decision.

| arm | TP | FP | FN | TN | prec | recall | F1 | alerts/h |
|-----|----|----|----|----|------|--------|----|----------|
| detector (no triage) | 4 | 6 | 0 | 8 | 0.40 | 1.00 | 0.57 | 3.33 |
| rules baseline | 4 | 1 | 0 | 13 | 0.80 | 1.00 | 0.89 | 1.67 |
| nemotron | 4 | 0 | 0 | 14 | 1.00 | 1.00 | 1.00 | 1.33 |
| rules, layover join off | 4 | 3 | 0 | 11 | 0.57 | 1.00 | 0.73 | 2.33 |

The last row is an ablation that prices a piece of plumbing rather than
asserting it matters: joining static GTFS to find terminal stops is worth
0.73 → 0.89 on its own.

### Three failures worth reporting

**A measurement bug that invalidated the headline metric.** Lead time was being
measured against `Alert.active_period.start`. Agencies *backdate* that field to
when the disruption began, so detections appeared to arrive 80 minutes *after*
the agency — inverting the product's entire claim. Publication time has to be
first-sighting-in-feed, the only publication timestamp a polling consumer can
observe. Regression test:
`test_published_at_is_first_sighting_not_active_start`.

**A threshold that was simply wrong.** The surviving false positives were all
AVL dropouts lasting 11–13 minutes against a 10-minute vanish threshold.
`dispatch sweep` found 13 minutes clears them at zero recall cost:

| vanish threshold | TP | FP | FN | prec | recall | F1 |
|------------------|----|----|----|------|--------|----|
| 10 min | 4 | 3 | 0 | 0.57 | 1.00 | 0.73 |
| 13 min | 4 | 1 | 0 | 0.80 | 1.00 | 0.89 |
| 20 min | 4 | 1 | 0 | 0.80 | 1.00 | 0.89 |

**A silent four-hour error, found by a teammate's laptop.** Windows ships no
IANA timezone database, so `zoneinfo` failed and the old code fell back to
reading local departure times as if they were UTC. It did not raise — it just
shifted every departure by four hours, which corrupts `slack_minutes` and
therefore every triage decision downstream. `dispatch/timeutil.py` now degrades
to the machine's own local zone, and `dispatch doctor` prints which zone is
actually in use. The project stays dependency-free.

**A feature that measured the wrong thing.** `drift_m` is distance from an
anchor set *before* the vehicle stopped, so a bus decelerating into a stop
reported 51 m of "drift" while genuinely stationary buses reported 2 m. Any
threshold on it misfires. Replaced with `jitter_m`, the spread of positions
*within* the dwell window. `drift_m` is retained and explicitly documented as
unsuitable for stationarity.

And one limitation we will not paper over: on this synthetic set the feed's own
`current_status` separates holds from breakdowns almost perfectly, because we
generated it that way. That caps how much the arm comparison can claim. Real
recordings will be messier, and a disabled bus stopped beside a stop will
report `STOPPED_AT` too.

---

## Xtract

**Different sources without rebuilding anything.** One adapter protocol, two
target shapes. Nothing downstream of the adapter layer knows what format
anything arrived in.

```python
@register_document("jsonld")
class JsonLdSource(DocumentSource):
    extensions = (".jsonld",)
    def parse(self, blob, source_id, retrieved_at): ...
```

That is the entire contract. `python3 -m dispatch.cli sources` prints what is
registered rather than us claiming coverage:

| lane | adapters |
|------|----------|
| telemetry | `prt-bus`, `prt-rail` (GTFS-RT protobuf), `amtrak` (JSON) |
| documents | `email` `.eml`, `ics` `.ical`, `pdf`, `csv` `.tsv`, `html`, `text` `.md` |

An unrecognised extension degrades to the text reader rather than being
rejected, because at 3am a working guess beats a refusal.

**Finding signal in incoming documents.** Extraction pulls trip legs out of
confirmation emails, e-tickets, boarding-pass PDFs, calendar invites, rosters
and saved booking pages — mode, carrier, service number, origin, destination,
departure, plus the recoverability context that drives triage: a leg on a
6-minute headway is an inconvenience, a once-daily train is a ruined day.

**Insights link back to where they came from.** Every `Signal` and every
extracted field carries `Provenance` with a locator appropriate to its format —
a protobuf entity index, a JSON path, a PDF page, an email header, a CSV cell,
a character range. Click a number in the dashboard and you get the source text
with the supporting span highlighted. A test asserts that every recorded span
actually contains the value claimed from it, so provenance is verified rather
than decorative.

**Seeing and using the output.** `dispatch serve` streams a recording through
the pipeline in feed order, so findings appear when the system could first have
known them — the point of a proactive tool, and something a static screenshot
cannot show. Four endpoints: `/api/state`, `/api/scenarios`, `/api/replay`,
`/api/document/<id>`.

---

## Seed Round

**The problem.** Transit agencies publish disruption alerts after the
disruption has already cascaded through their own dispatch system. On this
recording that lag ran 11–24 minutes. The rider finds out when the bus fails to
arrive, which is exactly too late to do anything except be late. Meanwhile the
raw telemetry that *shows* the bus stopped moving is public, free, and
unauthenticated — nobody is watching it on any individual rider's behalf.

**Who has it.** Not "commuters" in general — people for whom a specific missed
connection has a hard, asymmetric cost, and who have a fixed obligation on the
other end:

- **Shift workers on hourly attendance policies.** Being 20 minutes late is a
  write-up, and the useful action (text the shift lead, swap with someone) has
  to happen *before* you are already late.
- **Students with attendance-graded or exam commitments.** At Pitt, 61C, 71B and
  the East Busway carry a large share of a 30,000-person campus.
- **Airport runs on infrequent connectors.** The 28X runs every 30 minutes; miss
  it and you miss a bag drop, which costs a rebooking fee, not an apology.
- **Paratransit and appointment transport coordinators,** who are managing this
  by phone today and for whom lead time is the entire job.

**Why anyone would want it.** The wedge is that the alert is *actionable and
specific*: not "delays on the 61C" but "you are going to miss your 4:25 shift,
here is the message to your lead, ready to send." Transit apps already show you
where the bus is. None of them know what you are trying to do, because none of
them have read your confirmation emails.

**Business model, honestly.** Consumer subscription is the obvious shape and
the weak one — riders will not pay $5/month for something that is silent most
weeks. The stronger path is B2B2C: universities and large shift employers
already absorb the cost of transit-caused lateness, and already run
notification systems they pay for. Sell per-site, not per-seat.

**What is not proven yet.** The lead-time number comes from a synthetic
recording whose 11–24 minute alert lag is an assumption. The next step is not
more features, it is a week of continuous recording against the live PRT feed
to measure the real distribution — `dispatch record` exists for exactly that,
and the eval re-runs unchanged against a real capture.

**Legal and data posture.** PRT publishes GTFS-Realtime openly under its
developer licence, no key. Amtraker is ODC-By and requires attribution, given
below. Aviation is deliberately excluded: OpenSky meters requests in credits
and is non-commercial only, so a product built on it would need a licensed
feed. Nothing is ever sent on the user's behalf — `INTERVENE` drafts and waits,
which is a deliberate product decision and a tested invariant, because an agent
that emails your colleagues on a model's judgement is a liability rather than a
feature.

---

## Layout

```
dispatch/
  wire.py          protobuf wire format, hand-written, no dependencies
  gtfsrt.py        GTFS-Realtime decode and encode
  schema.py        Observation, Document, Signal, Verdict, Provenance
  sources/         adapter registry: 3 telemetry, 6 document formats
  detect.py        deterministic detection, injectable thresholds
  llm.py           rules and nemotron backends, prompts, clamp invariants
  pipeline.py      orchestration, lead-time matching, sqlite store
  simulate.py      labelled synthetic world, emits real protobuf
  fixture_docs.py  itinerary documents in six formats
  record.py        live feed recorder
  replay.py        snapshot replay
  evaluate.py      arms, confusion matrix, ablation, threshold sweep
  server.py        dashboard API
  web/index.html   dashboard
  cli.py           every command above
tools/
  mock_prt.py       serves a world as PRT, to test the recorder offline
  mock_nemotron.py  OpenAI-compatible mock, to test the model client offline
  timeutil.py      timezone resolution without a tzdata dependency
tests/              44 tests, 5 of them regressions for bugs listed above
```

## Attribution

Amtrak data via **Amtraker** (<https://api.amtraker.com/docs>), licensed
ODC-By 1.0. Transit data from **Pittsburgh Regional Transit** under its
developer licence agreement. GTFS-Realtime is a Google specification.
