# Evaluation

Generated 2026-09-19 17:35:31 from `fixtures/world`.

## What was measured

A 3.0-hour recording containing 540 feed snapshots and 18 injected scenarios, of which 4 should reach the user and 14 should not.

A positive prediction is `act in {NOTIFY, INTERVENE}` -- the two branches that surface to a human. Every arm replays the same bytes and the same extracted itinerary, so the only variable is the triage decision.

| arm                        | TP | FP | FN | TN | prec | recall |  F1  | alerts/h | median lead |
|----------------------------|----|----|----|----|------|--------|------|----------|-------------|
| detector                   |  4 |  6 |  0 |  8 | 0.40 | 1.00   | 0.57 |     3.33 |     6.7 min |
| rules                      |  4 |  1 |  0 | 13 | 0.80 | 1.00   | 0.89 |     1.67 |     6.7 min |


## Where the errors are

- **detector**: false positives FEED_ARTIFACTx1, STALL_DISABLEDx1, TIMEPOINT_HOLDx3, VANISHED_REALx1
- **rules**: false positives TIMEPOINT_HOLDx1

## Honest limits of this number

- The scenarios are synthetic. Motion, GPS jitter and the 11-24 minute agency alert lag are assumptions, and the lead-time figure inherits them directly. Re-run against a real recording before quoting it as a product claim: `dispatch record` then `dispatch eval --world <dir>`.
- Ground truth is per scenario, not per notification. A system that fires five times about one real stall scores one true positive, which flatters it; `alerts/h` is included to keep that visible.
- 18 scenarios is a small sample. Treat single-point differences between arms as noise.
