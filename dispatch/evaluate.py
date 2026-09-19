"""Scoring the triage decision against ground truth.

What is being measured. The detector is arithmetic and fires on anything that
looks unusual; that is deliberate, because a condition never measured cannot be
recovered downstream. The question this harness answers is whether the *triage*
stage turns those raw signals into the right decisions -- specifically whether
it suppresses the benign ones without dropping the real ones.

Arms, all sharing one recording and one extracted itinerary so the only thing
varying is the triage decision:

  detector   no triage at all. Every signal becomes a notification. This is
             what the proposal's original design would have shipped.
  rules      interpretable thresholds. A real baseline, not a mock, and the
             offline fallback when the venue wifi dies.
  nemotron   the model, via NVIDIA's OpenAI-compatible endpoint.

Plus one ablation: `rules` with the static-GTFS terminal join switched off,
which prices a piece of plumbing rather than asserting it matters.

Positive prediction = act in {NOTIFY, INTERVENE}. Those are the two branches
that reach the user. LOG and SUPPRESS do not.

One measurement rule worth stating: lead time is only reported for true
positives. A false positive can still "beat" an unrelated alert published later
on the same route, and counting that would let a system win the headline number
by crying wolf.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field

from .detect import Detector, load_terminal_stops
from .llm import RulesBackend, get_backend
from .pipeline import Pipeline
from .replay import default_docs, load_snapshots, load_truth
from .schema import ItineraryItem, Signal, Verdict
from .sources.base import load_documents

REACHES_USER = ("NOTIFY", "INTERVENE")

# How loosely a signal may sit around a scenario window and still be credited
# to it. Detection legitimately lags onset by the threshold (7 min for a
# stall, 10 for a vanish), and a stall can be detected shortly after the
# scenario's nominal end while the bus is still recovering.
ATTRIB_LEAD_S = 120
ATTRIB_TRAIL_S = 900


class AlwaysNotify:
    """Arm A: no triage. Every measured signal reaches the user.

    Extraction is delegated so that all arms see an identical itinerary --
    otherwise the comparison would be confounded by two changes at once.
    """

    name = "detector"

    def __init__(self) -> None:
        self._rules = RulesBackend()

    def triage(self, signal: Signal, items: list[ItineraryItem], now: int) -> Verdict:
        return Verdict(
            act="NOTIFY",
            severity=2,
            reason_code="NO_TRIAGE",
            confidence=1.0,
            rationale="every signal forwarded",
            backend=self.name,
            latency_ms=0,
        )

    def extract(self, doc):
        return self._rules.extract(doc)


class HoldExtraction:
    """Wraps a triage backend but keeps extraction on the rules path.

    The triage comparison and the extraction comparison are separate
    experiments. Mixing them would mean a win could come from either.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._rules = RulesBackend()
        self.name = getattr(inner, "name", "wrapped")

    def triage(self, signal, items, now):
        return self._inner.triage(signal, items, now)

    def extract(self, doc):
        return self._rules.extract(doc)


@dataclass
class ArmResult:
    arm: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    spurious: int = 0            # positive findings matching no scenario
    signals: int = 0
    notifications: int = 0
    hours: float = 0.0
    lead_times_s: list[int] = field(default_factory=list)
    fp_by_class: dict[str, int] = field(default_factory=dict)
    fn_by_class: dict[str, int] = field(default_factory=dict)
    schema_failures: int = 0
    fellback: int = 0             # verdicts served by the fallback, not the model
    latencies_ms: list[int] = field(default_factory=list)
    note: str = ""

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp + self.spurious
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def alerts_per_hour(self) -> float:
        return self.notifications / self.hours if self.hours else 0.0

    @property
    def median_lead_min(self) -> float | None:
        if not self.lead_times_s:
            return None
        return round(statistics.median(self.lead_times_s) / 60.0, 1)

    def to_dict(self) -> dict:
        return {
            "arm": self.arm,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "spurious": self.spurious,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "signals": self.signals,
            "notifications": self.notifications,
            "alerts_per_hour": round(self.alerts_per_hour, 2),
            "median_lead_min": self.median_lead_min,
            "fp_by_class": self.fp_by_class,
            "fn_by_class": self.fn_by_class,
            "schema_failures": self.schema_failures,
            "fellback": self.fellback,
            "median_latency_ms": (
                int(statistics.median(self.latencies_ms)) if self.latencies_ms else 0
            ),
            "note": self.note,
        }


def _attribute(finding, scenarios: list[dict]) -> dict | None:
    """Credit a finding to the scenario that caused it, or None."""
    sig = finding.signal
    best = None
    for sc in scenarios:
        if sc["vehicle_id"] != sig.vehicle_id:
            continue
        lo = sc["onset"] - ATTRIB_LEAD_S
        hi = sc["onset"] + sc["duration"] + ATTRIB_TRAIL_S
        if lo <= sig.first_seen_at <= hi or lo <= sig.detected_at <= hi:
            best = sc
            break
    return best


def score(pipe: Pipeline, truth: dict, arm: str, note: str = "") -> ArmResult:
    scenarios = truth["scenarios"]
    res = ArmResult(arm=arm, note=note)
    res.hours = truth["ticks"] * truth["tick_seconds"] / 3600.0
    res.signals = len(pipe.findings)

    # scenario_id -> did any finding for it reach the user
    reached: dict[str, bool] = {}
    for f in pipe.findings:
        if f.verdict.latency_ms:
            res.latencies_ms.append(int(f.verdict.latency_ms))
        if f.verdict.reason_code in ("UNPARSEABLE_ACT", "MODEL_ERROR"):
            res.schema_failures += 1
        if "fallback" in (f.verdict.backend or ""):
            res.fellback += 1

        positive = f.verdict.act in REACHES_USER
        if positive:
            res.notifications += 1

        sc = _attribute(f, scenarios)
        if sc is None:
            if positive:
                res.spurious += 1
            continue
        reached[sc["scenario_id"]] = reached.get(sc["scenario_id"], False) or positive

        # Lead time only counts when the finding is a genuine hit.
        if positive and sc["should_notify"] and f.lead_time_s is not None:
            res.lead_times_s.append(f.lead_time_s)

    for sc in scenarios:
        actual = bool(sc["should_notify"])
        predicted = reached.get(sc["scenario_id"], False)
        if actual and predicted:
            res.tp += 1
        elif actual and not predicted:
            res.fn += 1
            res.fn_by_class[sc["kind"]] = res.fn_by_class.get(sc["kind"], 0) + 1
        elif not actual and predicted:
            res.fp += 1
            res.fp_by_class[sc["kind"]] = res.fp_by_class.get(sc["kind"], 0) + 1
        else:
            res.tn += 1
    return res


def run_arm(
    root: str,
    arm: str,
    *,
    layover_aware: bool = True,
    doc_paths: list[str] | None = None,
    note: str = "",
    stall_seconds: int | None = None,
    vanish_seconds: int | None = None,
) -> tuple[ArmResult, Pipeline]:
    truth = load_truth(root)
    if truth is None:
        raise SystemExit(
            f"no truth.json in {root}. Generate a labelled world first:\n"
            f"  python -m dispatch.cli fixtures"
        )

    if arm == "detector":
        backend = AlwaysNotify()
    elif arm == "rules":
        backend = HoldExtraction(RulesBackend())
    else:
        inner = get_backend(arm)
        # Refuse rather than silently score the fallback path as if it were
        # the model. An empty NVIDIA_API_KEY used to produce a complete set of
        # rules verdicts labelled "nemotron" and an identical F1, which is the
        # kind of number that survives into a demo and should not.
        if not getattr(inner, "available", True):
            raise RuntimeError(
                "NVIDIA_API_KEY is not set, so the nemotron arm cannot run. "
                "Export a key, or point DISPATCH_NVIDIA_URL at "
                "tools/mock_nemotron.py to exercise the client offline."
            )
        backend = HoldExtraction(inner)

    gtfs_zip = os.path.join(root, "gtfs-static.zip")
    terminals = load_terminal_stops(gtfs_zip) if os.path.exists(gtfs_zip) else set()
    det_kwargs: dict = {
        "terminal_stops": terminals,
        "layover_aware": layover_aware,
    }
    if stall_seconds is not None:
        det_kwargs["stall_seconds"] = stall_seconds
    if vanish_seconds is not None:
        det_kwargs["vanish_seconds"] = vanish_seconds

    pipe = Pipeline(backend=backend, detector=Detector(**det_kwargs))

    # An endpoint override is usually the offline mock. Say so in the report
    # rather than letting a "nemotron" row imply NVIDIA served it.
    from .llm import NVIDIA_URL
    if arm not in ("detector", "rules") and "integrate.api.nvidia.com" not in NVIDIA_URL:
        host = NVIDIA_URL.split("/")[2] if "//" in NVIDIA_URL else NVIDIA_URL
        res_note = f"endpoint {host}, not NVIDIA"
        note = f"{note}; {res_note}" if note else res_note
    paths = doc_paths if doc_paths is not None else default_docs(root)
    if paths:
        pipe.load_documents(load_documents(paths))

    for snap in load_snapshots(root):
        pipe.observe(snap.observations, snap.alerts, snap.epoch)
    pipe.reconcile_alerts()

    result = score(pipe, truth, arm, note=note)
    # Make a non-NVIDIA endpoint visible in the arm name, not only in a
    # footnote. A row labelled plainly "nemotron" in a table a judge is
    # reading should mean NVIDIA served it.
    if "not NVIDIA" in note:
        result.arm = f"{arm} (mock endpoint)"
    return result, pipe


def compare(root: str, arms: list[str], *, ablate: bool = True) -> list[ArmResult]:
    results: list[ArmResult] = []
    for arm in arms:
        try:
            res, _ = run_arm(root, arm)
        except Exception as exc:  # a missing API key should not kill the run
            res = ArmResult(arm=arm, note=f"did not run: {type(exc).__name__}: {exc}")
        results.append(res)

    if ablate:
        try:
            res, _ = run_arm(
                root, "rules", layover_aware=False,
                note="terminal-stop join disabled",
            )
            res.arm = "rules (no layover join)"
            results.append(res)
        except Exception as exc:
            results.append(
                ArmResult(arm="rules (no layover join)", note=f"did not run: {exc}")
            )
    return results


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def table(results: list[ArmResult]) -> str:
    head = (
        f"| {'arm':26s} | TP | FP | FN | TN | prec | recall |  F1  "
        f"| alerts/h | median lead |\n"
        f"|{'-' * 28}|----|----|----|----|------|--------|------"
        f"|----------|-------------|\n"
    )
    rows = ""
    for r in results:
        if r.note.startswith("did not run"):
            rows += f"| {r.arm:26s} | {'-':^2} | {'-':^2} | {'-':^2} | {'-':^2} " \
                    f"| {'-':^4} | {'-':^6} | {'-':^4} | {'-':^8} | {'-':^11} |\n"
            continue
        lead = f"{r.median_lead_min} min" if r.median_lead_min is not None else "n/a"
        rows += (
            f"| {r.arm:26s} | {r.tp:2d} | {r.fp + r.spurious:2d} | {r.fn:2d} "
            f"| {r.tn:2d} | {r.precision:.2f} | {r.recall:.2f}   "
            f"| {r.f1:.2f} | {r.alerts_per_hour:8.2f} | {lead:>11s} |\n"
        )
    return head + rows


def report(root: str, results: list[ArmResult], truth: dict) -> str:
    n_pos = sum(1 for s in truth["scenarios"] if s["should_notify"])
    lines = [
        "# Evaluation",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} from `{root}`.",
        "",
        "## What was measured",
        "",
        f"A {results[0].hours:.1f}-hour recording containing "
        f"{truth['ticks']} feed snapshots and {len(truth['scenarios'])} injected "
        f"scenarios, of which {n_pos} should reach the user and "
        f"{len(truth['scenarios']) - n_pos} should not.",
        "",
        "A positive prediction is `act in {NOTIFY, INTERVENE}` -- the two "
        "branches that surface to a human. Every arm replays the same bytes "
        "and the same extracted itinerary, so the only variable is the triage "
        "decision.",
        "",
        table(results),
        "",
        "## Where the errors are",
        "",
    ]
    for r in results:
        if r.note.startswith("did not run"):
            lines.append(f"- **{r.arm}**: {r.note}")
            continue
        bits = []
        if r.fp_by_class:
            bits.append("false positives " + ", ".join(
                f"{k}x{v}" for k, v in sorted(r.fp_by_class.items())))
        if r.fn_by_class:
            bits.append("missed " + ", ".join(
                f"{k}x{v}" for k, v in sorted(r.fn_by_class.items())))
        if r.spurious:
            bits.append(f"{r.spurious} unattributable")
        if r.schema_failures:
            bits.append(f"{r.schema_failures} malformed model responses")
        if r.fellback:
            bits.append(
                f"**{r.fellback}/{r.signals} verdicts came from the fallback, "
                f"not the model** -- treat this row as degraded")
        lines.append(f"- **{r.arm}**: " + ("; ".join(bits) if bits else "clean"))
    lines += [
        "",
        "## Honest limits of this number",
        "",
        "- The scenarios are synthetic. Motion, GPS jitter and the 11-24 minute "
        "agency alert lag are assumptions, and the lead-time figure inherits "
        "them directly. Re-run against a real recording before quoting it as "
        "a product claim: `dispatch record` then `dispatch eval --world <dir>`.",
        "- Ground truth is per scenario, not per notification. A system that "
        "fires five times about one real stall scores one true positive, which "
        "flatters it; `alerts/h` is included to keep that visible.",
        "- 18 scenarios is a small sample. Treat single-point differences "
        "between arms as noise.",
        "",
    ]
    return "\n".join(lines)


def write_report(root: str, results: list[ArmResult], truth: dict,
                 out_path: str = "EVAL.md") -> str:
    text = report(root, results, truth)
    with open(out_path, "w") as fh:
        fh.write(text)
    with open(os.path.splitext(out_path)[0] + ".json", "w") as fh:
        json.dump([r.to_dict() for r in results], fh, indent=2)
    return out_path


def sweep(
    root: str,
    arm: str = "rules",
    vanish_options: tuple[int, ...] = (600, 780, 900, 1080, 1200),
) -> list[tuple[int, ArmResult]]:
    """Vary the vanish threshold and watch precision trade against recall.

    Motivated by an actual result: the surviving false positives at the default
    600s were all feed artifacts lasting 11-13 minutes, i.e. just over the
    threshold. If they clear at a higher threshold without costing recall, the
    default was simply wrong, and the eval is how you find that out rather
    than guessing.
    """
    out = []
    for seconds in vanish_options:
        res, _ = run_arm(root, arm, vanish_seconds=seconds,
                         note=f"vanish_seconds={seconds}")
        res.arm = f"{arm} vanish={seconds}s"
        out.append((seconds, res))
    return out


def sweep_table(rows: list[tuple[int, ArmResult]]) -> str:
    head = ("| vanish threshold | TP | FP | FN | prec | recall |  F1  |\n"
            "|------------------|----|----|----|------|--------|------|\n")
    body = ""
    for seconds, r in rows:
        body += (f"| {seconds // 60:>13d} min | {r.tp:2d} | {r.fp + r.spurious:2d} "
                 f"| {r.fn:2d} | {r.precision:.2f} | {r.recall:.2f}   "
                 f"| {r.f1:.2f} |\n")
    return head + body
