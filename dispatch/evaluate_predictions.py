"""Scoring the arrival predictions against what actually happened.

An ETA nobody has measured is decoration. This replays a recording twice: once
to learn when each vehicle really reached each stop, and once to ask the
predictor, at every snapshot along the way, when it thought that would happen.
The difference between those two numbers is the error.

Two things the table is designed to expose rather than hide.

Coverage. The predictor refuses to guess when a vehicle is stationary, off
route, or too new to have a speed. A model that answers every question looks
better on a naive accuracy chart and is worse in practice, so refusals are
counted separately instead of being scored as though they were predictions.

The disruption split. Errors are reported separately for vehicles inside an
injected disruption window. Speed-based prediction is good on a moving bus and
useless on a broken one, and that gap is the argument for Dispatch treating
disruption detection as a separate system rather than something to be inferred
from a wandering ETA.
"""

from __future__ import annotations

import bisect
import json
import statistics
from dataclasses import dataclass, field

from .predict import Predictor, from_static_gtfs
from .replay import load_snapshots, load_truth

# Horizon buckets, in seconds. A three-minute ETA and a half-hour ETA are
# different products and averaging them together hides both.
BUCKETS = ((0, 300, "under 5 min"), (300, 900, "5 to 15 min"),
           (900, 10 ** 9, "over 15 min"))


@dataclass
class Row:
    label: str
    errors: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.errors)

    @property
    def median_abs(self) -> float:
        return statistics.median([abs(e) for e in self.errors]) if self.errors else 0.0

    @property
    def mean_signed(self) -> float:
        return statistics.fmean(self.errors) if self.errors else 0.0

    @property
    def p90_abs(self) -> float:
        if not self.errors:
            return 0.0
        vals = sorted(abs(e) for e in self.errors)
        return vals[min(len(vals) - 1, int(0.9 * len(vals)))]

    @property
    def within_120(self) -> float:
        if not self.errors:
            return 0.0
        return sum(1 for e in self.errors if abs(e) <= 120) / len(self.errors)


@dataclass
class PredictionReport:
    rows: dict[str, Row] = field(default_factory=dict)
    basis_counts: dict[str, int] = field(default_factory=dict)
    scored: int = 0
    unmatched: int = 0

    def row(self, label: str) -> Row:
        return self.rows.setdefault(label, Row(label))

    def to_dict(self) -> dict:
        return {
            "scored": self.scored,
            "unmatched": self.unmatched,
            "basis_counts": self.basis_counts,
            "rows": {k: {"n": r.n, "median_abs_s": round(r.median_abs),
                         "p90_abs_s": round(r.p90_abs),
                         "mean_signed_s": round(r.mean_signed),
                         "within_2min": round(r.within_120, 3)}
                     for k, r in self.rows.items()},
        }


def _disrupted_windows(truth: dict | None) -> dict[str, list[tuple[int, int]]]:
    """vehicle_id -> windows during which it was deliberately broken."""
    out: dict[str, list[tuple[int, int]]] = {}
    if not truth:
        return out
    for sc in truth.get("scenarios", []):
        if not sc.get("disruptive"):
            continue
        out.setdefault(sc["vehicle_id"], []).append(
            (sc["onset"], sc["onset"] + sc["duration"]))
    return out


def run(root: str, gtfs: str | None = None, horizon_limit: int = 4,
        mode: str = "segment", horizon_s: int | None = None) -> PredictionReport:
    """Replay `root` and score every prediction that can be checked."""
    gtfs = gtfs or f"{root}/gtfs-static.zip"
    snaps = load_snapshots(root)
    truth = load_truth(root)
    broken = _disrupted_windows(truth)
    report = PredictionReport()
    if not snaps:
        return report

    pred = from_static_gtfs(gtfs, mode=mode, horizon_s=horizon_s)
    if not pred.geo:
        return report

    # Pass one: ground truth. Where was every vehicle, and when did it pass
    # each stop? "Passed" means its distance along the shape crossed the
    # stop's distance, which is the same yardstick the predictor uses, so the
    # comparison is not confounded by a different definition of arrival.
    truth_pred = from_static_gtfs(gtfs)
    actual: dict[tuple[str, int, str], int] = {}
    prev_along: dict[str, tuple[float, int]] = {}
    for snap in snaps:
        truth_pred.observe(snap.observations, snap.epoch)
        for key, tr in truth_pred.tracks.items():
            geo = truth_pred.geo[tr.route_id]
            was = prev_along.get(key)
            prev_along[key] = (tr.along, tr.trips)
            if was is None or was[1] != tr.trips:
                continue
            lo, hi = was[0], tr.along
            if hi <= lo:
                continue
            dists = [s[3] for s in geo.stops]
            for j in range(bisect.bisect_right(dists, lo),
                           bisect.bisect_right(dists, hi)):
                k = (tr.vehicle_id, tr.trips, geo.stops[j][0])
                actual.setdefault(k, snap.epoch)

    # Pass two: predictions, checked against those crossings.
    for snap in snaps:
        pred.observe(snap.observations, snap.epoch)
        for key, tr in pred.tracks.items():
            for p in pred.predict_vehicle(key, limit=horizon_limit,
                                          now=snap.epoch):
                report.basis_counts[p.basis] = (
                    report.basis_counts.get(p.basis, 0) + 1)
                if p.arrives_at is None:
                    continue
                truth_at = actual.get((tr.vehicle_id, tr.trips, p.stop_id))
                if truth_at is None or truth_at < snap.epoch:
                    report.unmatched += 1
                    continue
                err = float(p.arrives_at - truth_at)
                report.scored += 1
                report.row("all").errors.append(err)

                horizon = truth_at - snap.epoch
                for lo, hi, label in BUCKETS:
                    if lo <= horizon < hi:
                        report.row(label).errors.append(err)
                        break

                windows = broken.get(tr.vehicle_id, [])
                hit = any(a <= snap.epoch < b for a, b in windows)
                report.row("disrupted vehicle" if hit
                           else "normal operation").errors.append(err)
    return report


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

ORDER = ("all", "under 5 min", "5 to 15 min", "over 15 min",
         "normal operation", "disrupted vehicle")


def table(report: PredictionReport) -> str:
    head = (f"| {'slice':20s} |     n | median err | p90 err | within 2 min |\n"
            f"|{'-'*22}|-------|------------|---------|--------------|\n")
    body = ""
    for label in ORDER:
        r = report.rows.get(label)
        if not r or not r.n:
            continue
        body += (f"| {label:20s} | {r.n:5d} | {r.median_abs/60:7.1f} min "
                 f"| {r.p90_abs/60:4.1f} min | {r.within_120*100:10.0f}% |\n")
    return head + body


def coverage(report: PredictionReport) -> str:
    total = sum(report.basis_counts.values()) or 1
    lines = ""
    for basis, n in sorted(report.basis_counts.items(), key=lambda kv: -kv[1]):
        lines += f"| {basis:20s} | {n:6d} | {100*n/total:5.1f}% |\n"
    return (f"| {'basis':20s} |      n | share |\n"
            f"|{'-'*22}|--------|-------|\n" + lines)


def write_report(report: PredictionReport, out_path: str = "PREDICTIONS.md",
                 root: str = "", arms_md: str = "", sweep_md: str = "") -> str:
    d = report.to_dict()
    text = "\n".join([
        "# Arrival prediction accuracy",
        "",
        f"Scored {d['scored']:,} predictions against observed stop crossings"
        + (f" from `{root}`." if root else "."),
        "",
        "## Two predictors, compared",
        "",
        arms_md or "(run with --arms to score the constant-speed baseline)",
        "",
        "`speed` extrapolates a vehicle's current speed across the whole"
        " remaining trip. `segment` splits each route into 400m bins, learns"
        " each bin's typical speed from vehicles that have already driven it,"
        " and integrates. A route is not one speed, so the gap grows with"
        " distance.",
        "",
        "A note on how this comparison was earned. Scored the first time, the"
        " segment model showed no benefit at all. That turned out to be a flaw"
        " in the *fixture*, not the model: every simulated vehicle moved at one"
        " nominal speed along its whole route, so there was no spatial"
        " variation to learn and the evaluation could not have detected one."
        " Adding per-location speed profiles to the simulator made the"
        " phenomenon exist, and the model then showed a clear win. An"
        " evaluation that cannot fail is not measuring anything.",
        "",
        "## Horizon policy",
        "",
        sweep_md or "(run with --horizon-sweep to price the cutoff)",
        "",
        "Refusing past a horizon is a measured choice rather than a taste."
        " Twenty minutes takes p90 error down by roughly a third for about five"
        " points of extra refusals, so that is the default.",
        "",
        "## Error",
        "",
        table(report),
        "Signed mean for all slices: "
        f"{report.row('all').mean_signed:.0f}s "
        f"({'late' if report.row('all').mean_signed > 0 else 'early'} on average).",
        "",
        "## What the predictor refused to answer",
        "",
        coverage(report),
        "A refusal is not a failure. `stalled` means the vehicle is not moving,"
        " and dividing a distance by a speed of zero produces a number that is"
        " worse than no number. `insufficient_data` means fewer than three"
        " position samples, which is not enough to call a speed.",
        "",
        "## The finding that matters",
        "",
        "Compare the `normal operation` and `disrupted vehicle` rows. Speed-based"
        " prediction is usable on a moving bus and falls apart on a broken one,"
        " which is exactly the moment a rider needs to know something. That gap"
        " is why Dispatch detects disruptions with a separate deterministic"
        " detector instead of inferring them from a drifting ETA.",
        "",
        "## Limits",
        "",
        "- Ground truth here is a synthetic world, so the motion model is an"
        " assumption. Re-run against a real recording before quoting these"
        " numbers: `dispatch record --out recordings/x` then"
        " `dispatch predict-eval --world recordings/x`.",
        "- Arrival is defined as crossing the stop's distance along the route"
        " shape, the same yardstick the predictor uses. That is consistent but"
        " it is not the same as the doors opening.",
        "- Predictions are only scored when the vehicle was later observed"
        f" crossing that stop; {d['unmatched']:,} were left unmatched, mostly"
        " vehicles that vanished or ended their trip first.",
        "- That last point makes the disrupted row *optimistic*, and it is worth"
        " being clear about. A bus that breaks down and never moves again never"
        " crosses the stop, so its predictions are unmatched rather than scored."
        " The disrupted figures therefore come from the subset that eventually"
        " recovered. The real-world error for a vehicle that never arrives is"
        " unbounded.",
        "",
    ])
    with open(out_path, "w") as fh:
        fh.write(text)
    with open(out_path.rsplit(".", 1)[0] + ".json", "w") as fh:
        json.dump(d, fh, indent=2)
    return out_path


# --------------------------------------------------------------------------
# arms
# --------------------------------------------------------------------------

def horizon_sweep(root: str, gtfs: str | None = None,
                  options=(None, 1800, 1200, 900, 600)) -> str:
    """Price the refuse-past-here policy instead of picking a number by taste."""
    lines = (f"| {'horizon':>9s} | {'median':>7s} | {'p90':>6s} |"
             f" {'within 2 min':>12s} | {'refused':>8s} |\n"
             f"|-----------|---------|--------|--------------|----------|\n")
    for h in options:
        r = run(root, gtfs=gtfs, mode="segment", horizon_s=h)
        a = r.rows.get("all")
        if not a or not a.n:
            continue
        total = sum(r.basis_counts.values()) or 1
        answered = (r.basis_counts.get("segment", 0)
                    + r.basis_counts.get("speed", 0))
        lines += (f"| {('none' if h is None else str(h//60)+' min'):>9s} "
                  f"| {a.median_abs:6.0f}s | {a.p90_abs:5.0f}s "
                  f"| {a.within_120*100:11.0f}% "
                  f"| {100*(total-answered)/total:7.1f}% |\n")
    return lines


def compare(root: str, gtfs: str | None = None) -> list[tuple[str, PredictionReport]]:
    """Score the predictors against each other on one recording.

    Same structure as the triage arms in evaluate.py, for the same reason: a
    number with nothing to compare it to is not evidence. `speed` is the naive
    constant-speed extrapolation; `segment` learns each stretch of the route
    from the vehicles that have already driven it.
    """
    arms = [
        ("speed (constant)", dict(mode="speed", horizon_s=None)),
        ("segment (learned)", dict(mode="segment", horizon_s=None)),
    ]
    out = []
    for label, kw in arms:
        out.append((label, run(root, gtfs=gtfs, **kw)))
    return out


def arms_table(arms: list[tuple[str, PredictionReport]],
               slices: tuple[str, ...] = ("all", "under 5 min", "5 to 15 min",
                                          "over 15 min")) -> str:
    head = f"| {'arm':20s} |" + "".join(f" {sl:>13s} |" for sl in slices) + "\n"
    head += f"|{'-'*22}|" + "".join("-" * 15 + "|" for _ in slices) + "\n"
    body = ""
    for label, rep in arms:
        cells = ""
        for sl in slices:
            r = rep.rows.get(sl)
            cells += (f" {r.median_abs/60:6.1f} / {r.p90_abs/60:4.1f} |"
                      if r and r.n else f" {'-':>13s} |")
        body += f"| {label:20s} |{cells}\n"
    return head + body + "\nCells are median / p90 absolute error, in minutes.\n"
