# Arrival prediction accuracy

Scored 18,640 predictions against observed stop crossings from `fixtures/world`.

## Two predictors, compared

| arm                  |           all |   under 5 min |   5 to 15 min |   over 15 min |
|----------------------|---------------|---------------|---------------|---------------|
| speed (constant)     |    0.9 /  6.2 |    0.5 /  2.4 |    3.1 /  6.1 |    8.1 / 15.6 |
| segment (learned)    |    0.2 /  2.0 |    0.2 /  0.5 |    0.5 /  4.9 |    1.0 / 13.3 |

Cells are median / p90 absolute error, in minutes.


`speed` extrapolates a vehicle's current speed across the whole remaining trip. `segment` splits each route into 400m bins, learns each bin's typical speed from vehicles that have already driven it, and integrates. A route is not one speed, so the gap grows with distance.

A note on how this comparison was earned. Scored the first time, the segment model showed no benefit at all. That turned out to be a flaw in the *fixture*, not the model: every simulated vehicle moved at one nominal speed along its whole route, so there was no spatial variation to learn and the evaluation could not have detected one. Adding per-location speed profiles to the simulator made the phenomenon exist, and the model then showed a clear win. An evaluation that cannot fail is not measuring anything.

## Horizon policy

|   horizon |  median |    p90 | within 2 min |  refused |
|-----------|---------|--------|--------------|----------|
|      none |     15s |   120s |          90% |    16.1% |
|    30 min |     15s |   117s |          90% |    17.8% |
|    20 min |     15s |    77s |          91% |    21.6% |
|    15 min |     14s |    59s |          92% |    26.6% |
|    10 min |     14s |    43s |          94% |    33.5% |


Refusing past a horizon is a measured choice rather than a taste. Twenty minutes takes p90 error down by roughly a third for about five points of extra refusals, so that is the default.

## Error

| slice                |     n | median err | p90 err | within 2 min |
|----------------------|-------|------------|---------|--------------|
| all                  | 18640 |     0.2 min |  1.3 min |         91% |
| under 5 min          | 13562 |     0.2 min |  0.5 min |         98% |
| 5 to 15 min          |  3890 |     0.5 min |  4.5 min |         79% |
| over 15 min          |  1188 |     2.0 min | 20.6 min |         50% |
| normal operation     | 18544 |     0.2 min |  1.1 min |         92% |
| disrupted vehicle    |    96 |    23.6 min | 29.0 min |          0% |

Signed mean for all slices: -34s (early on average).

## What the predictor refused to answer

| basis                |      n | share |
|----------------------|--------|-------|
| segment              |  23612 |  72.9% |
| stalled              |   3169 |   9.8% |
| insufficient_data    |   2032 |   6.3% |
| beyond_horizon       |   1803 |   5.6% |
| speed                |   1761 |   5.4% |

A refusal is not a failure. `stalled` means the vehicle is not moving, and dividing a distance by a speed of zero produces a number that is worse than no number. `insufficient_data` means fewer than three position samples, which is not enough to call a speed.

## The finding that matters

Compare the `normal operation` and `disrupted vehicle` rows. Speed-based prediction is usable on a moving bus and falls apart on a broken one, which is exactly the moment a rider needs to know something. That gap is why Dispatch detects disruptions with a separate deterministic detector instead of inferring them from a drifting ETA.

## Limits

- Ground truth here is a synthetic world, so the motion model is an assumption. Re-run against a real recording before quoting these numbers: `dispatch record --out recordings/x` then `dispatch predict-eval --world recordings/x`.
- Arrival is defined as crossing the stop's distance along the route shape, the same yardstick the predictor uses. That is consistent but it is not the same as the doors opening.
- Predictions are only scored when the vehicle was later observed crossing that stop; 6,733 were left unmatched, mostly vehicles that vanished or ended their trip first.
- That last point makes the disrupted row *optimistic*, and it is worth being clear about. A bus that breaks down and never moves again never crosses the stop, so its predictions are unmatched rather than scored. The disrupted figures therefore come from the subset that eventually recovered. The real-world error for a vehicle that never arrives is unbounded.
