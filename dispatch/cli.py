"""Dispatch command line.

    python3 -m dispatch.cli sources            what can be ingested
    python3 -m dispatch.cli fixtures           build a labelled world
    python3 -m dispatch.cli replay             run a world through the pipeline
    python3 -m dispatch.cli eval               score the triage arms
    python3 -m dispatch.cli predict-eval       score arrival predictions
    python3 -m dispatch.cli sweep              tune a detector threshold
    python3 -m dispatch.cli record --out DIR   poll the live feeds
    python3 -m dispatch.cli gtfs               fetch PRT static GTFS (map geometry)
    python3 -m dispatch.cli verify             rebuild + test + re-derive all numbers
    python3 -m dispatch.cli doctor             feeds and model endpoint reachable
    python3 -m dispatch.cli serve              the dashboard
    python3 -m dispatch.cli demo               all of the above, one command
"""

from __future__ import annotations

import argparse
import os
import sys
import time

DEFAULT_WORLD = "fixtures/world"
DEFAULT_DOCS = "fixtures/docs"


def cmd_sources(args: argparse.Namespace) -> int:
    from .sources.base import DOCUMENTS, TELEMETRY

    print("telemetry adapters  (feed bytes -> Observation)")
    for name, src in sorted(TELEMETRY.items()):
        print(f"  {name:12s} {getattr(src, 'media', '?')}")
    print("\ndocument adapters   (file -> Document with landmark offsets)")
    for name, src in sorted(DOCUMENTS.items()):
        print(f"  {name:12s} {' '.join(getattr(src, 'extensions', ()))}")
    print(
        "\nAdding a source is one class with one parse() method and a\n"
        "decorator. Nothing downstream of the adapter layer changes:\n"
        "detection, triage, matching, storage and the dashboard all see\n"
        "Observation or Document. See dispatch/sources/base.py."
    )
    return 0


def cmd_fixtures(args: argparse.Namespace) -> int:
    from .fixture_docs import write_all
    from .simulate import World

    start = args.start or int(time.time()) - int(args.hours * 3600)
    world = World(start_epoch=start, hours=args.hours, seed=args.seed)
    truth = world.write(args.world)
    docs = write_all(args.docs, start)
    pos = sum(1 for s in truth["scenarios"] if s["should_notify"])
    print(f"wrote {truth['ticks']} protobuf snapshots -> {args.world}/")
    print(f"  {len(truth['scenarios'])} scenarios, {pos} should reach the user")
    print(f"  static GTFS   {args.world}/gtfs-static.zip")
    print(f"  ground truth  {args.world}/truth.json")
    print(f"wrote {len(docs)} itinerary documents -> {args.docs}/")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from . import replay

    docs = [] if args.no_docs else replay.default_docs(args.world)
    pipe = replay.run(args.world, doc_paths=docs, store_path=args.db,
                      quiet=not args.verbose)

    print(f"itinerary legs extracted: {len(pipe.itinerary)}")
    for it in pipe.itinerary[:10]:
        src = it.provenance[0].source_id if it.provenance else "?"
        print(f"  {it.kind:11s} {it.route_hint or '-':8s} {it.label[:32]:32s} <- {src}")
    print(f"\nofficial alerts seen: {len(pipe.official)}")
    print(f"findings: {len(pipe.findings)}")
    for f in sorted(pipe.findings, key=lambda x: x.signal.detected_at):
        # Lead time is only meaningful for findings that actually surfaced. A
        # suppressed finding can still "beat" an unrelated later alert on the
        # same route, and printing that would flatter the demo.
        if f.verdict.act in ("NOTIFY", "INTERVENE"):
            lead = (f"{f.lead_time_s // 60} min ahead of agency"
                    if f.lead_time_s is not None else "no agency alert")
        else:
            lead = ""
        print(f"  {f.signal.kind:9s} {f.signal.route_id:5s} "
              f"{f.signal.vehicle_id:11s} {f.verdict.act:10s} "
              f"{f.verdict.reason_code:20s} {lead}")
    if pipe.drafts:
        print(f"\ndrafted, never sent: {len(pipe.drafts)}")
        for d in pipe.drafts:
            print(f"  [{d['status']}] {d['subject']}")
    if args.db:
        print(f"\nwrote {args.db}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evaluate
    from .replay import load_truth

    truth = load_truth(args.world)
    if truth is None:
        print(f"no truth.json in {args.world}; run: python3 -m dispatch.cli fixtures",
              file=sys.stderr)
        return 2
    arms = args.arms or ["detector", "rules", "nemotron"]
    results = evaluate.compare(args.world, arms, ablate=not args.no_ablation)
    print(evaluate.table(results))
    for r in results:
        if r.note:
            print(f"  {r.arm}: {r.note}")
    if args.out:
        evaluate.write_report(args.world, results, truth, args.out)
        print(f"\nwrote {args.out} and {os.path.splitext(args.out)[0]}.json")
    return 0


def cmd_predict_eval(args: argparse.Namespace) -> int:
    from . import evaluate_predictions as ep

    if args.arms:
        arms = ep.compare(args.world, gtfs=args.gtfs)
        print(ep.arms_table(arms))
    if args.horizon_sweep:
        print("horizon policy, measured:")
        print(ep.horizon_sweep(args.world, gtfs=args.gtfs))
    rep = ep.run(args.world, gtfs=args.gtfs, mode=args.mode,
                 horizon_s=None if args.horizon == 0 else args.horizon)
    if not rep.scored:
        print("nothing to score: no route geometry, or no snapshots.",
              file=sys.stderr)
        print("try: python3 -m dispatch.cli fixtures", file=sys.stderr)
        return 2
    print(ep.table(rep))
    print(ep.coverage(rep))
    if args.out:
        ep.write_report(rep, args.out, root=args.world)
        print(f"wrote {args.out}")
    normal = rep.rows.get("normal operation")
    bad = rep.rows.get("disrupted vehicle")
    if normal and bad and normal.n and bad.n:
        print(f"\nmedian error on a moving bus: {normal.median_abs:.0f}s")
        print(f"median error on a disrupted one: {bad.median_abs:.0f}s "
              f"({bad.median_abs / max(normal.median_abs, 1):.0f}x worse)")
        print("That gap is why disruption detection is a separate system and\n"
              "not something inferred from a drifting ETA.")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    from . import evaluate

    rows = evaluate.sweep(args.world, args.arm)
    print(evaluate.sweep_table(rows))
    best = max(rows, key=lambda r: (r[1].f1, -r[0]))
    print(f"best F1 at vanish_seconds={best[0]} ({best[1].f1:.2f})")
    print("The optimum depends on how long AVL gaps actually last, and in the\n"
          "synthetic world that duration is an assumption. Re-run on a real\n"
          "recording before treating this as tuned.")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from . import record

    feeds = record.feeds_for_base(args.base) if args.base else None
    print(f"recording to {args.out}/ every {args.interval}s; ctrl-c to stop")
    record.record(args.out, interval=args.interval, duration=args.duration,
                  feeds=feeds)
    if os.path.isdir(args.out):
        for lane in sorted(os.listdir(args.out)):
            d = os.path.join(args.out, lane)
            if os.path.isdir(d):
                print(f"  {lane:18s} {len(os.listdir(d))} snapshots")
    print("\nNo ground truth, so `eval` will not score this. It is what\n"
          "`replay` and the dashboard should run against.")
    return 0


PRT_GTFS_URL = "https://www.rideprt.org/developerresources/GTFS.zip"


def cmd_gtfs(args: argparse.Namespace) -> int:
    """Fetch PRT's static GTFS, which is what gives the map real geometry.

    The realtime feed carries a route id and a coordinate and nothing else, so
    without this the map can draw vehicles but not the lines they run on, and
    routes show as bare ids rather than "61C McKeesport - Homestead".
    """
    import urllib.request

    from . import gtfs_static

    out = args.out
    if not args.skip_download:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        print(f"downloading {PRT_GTFS_URL}")
        try:
            req = urllib.request.Request(
                PRT_GTFS_URL, headers={"User-Agent": "dispatch/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r, open(out, "wb") as fh:
                fh.write(r.read())
        except Exception as exc:
            print(f"  failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(f"  download it by hand to {out} and re-run with "
                  f"--skip-download", file=sys.stderr)
            return 1
        print(f"  saved {os.path.getsize(out):,} bytes -> {out}")

    routes = gtfs_static.load(out)
    print(gtfs_static.summary(routes))
    for r in list(routes.values())[:8]:
        d = r.to_dict()
        print(f"  {d['label']:>6s}  {d['mode']:10s} {len(d['shape']):3d} pts  "
              f"{d['name'][:44]}")
    if len(routes) > 8:
        print(f"  ... and {len(routes) - 8} more")
    print("\nThe map picks this up automatically. Start it with:")
    print("  python3 -m dispatch.cli serve --live")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Rebuild everything and re-derive every number in the README.

    Exists because "it works on my machine" is not evidence. This regenerates
    the world from a fixed seed, runs the tests, scores both the triage arms
    and the arrival predictors, and prints the headline figures. Anyone who
    clones the repo can run it and get the same output.
    """
    import subprocess
    import unittest

    from . import evaluate, evaluate_predictions as ep
    from .fixture_docs import write_all
    from .replay import load_truth
    from .simulate import World

    ok = True
    print("1/4  building the labelled world (seed 7)")
    World(start_epoch=args.start, hours=3.0, seed=7).write(args.world)
    write_all(DEFAULT_DOCS, args.start)
    truth = load_truth(args.world)
    pos = sum(1 for sc in truth["scenarios"] if sc["should_notify"])
    print(f"     {truth['ticks']} snapshots, {len(truth['scenarios'])} scenarios,"
          f" {pos} should reach the user")

    print("\n2/4  tests")
    loader = unittest.TestLoader()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    suite = loader.discover(os.path.join(here, "tests"))
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    print(f"     {result.testsRun} tests, "
          f"{len(result.failures)} failures, {len(result.errors)} errors")
    ok = ok and result.wasSuccessful()

    print("\n3/4  triage arms")
    arms = evaluate.compare(args.world, ["detector", "rules", "nemotron"])
    print(evaluate.table(arms))
    rules = next((a for a in arms if a.arm == "rules"), None)
    if rules:
        print(f"     rules baseline: F1 {rules.f1:.2f}, recall {rules.recall:.2f},"
              f" median lead {rules.median_lead_min} min")
        ok = ok and rules.recall == 1.0

    print("\n4/4  arrival predictors")
    pa = ep.compare(args.world)
    print(ep.arms_table(pa))
    learned = dict(pa).get("segment (learned)")
    naive = dict(pa).get("speed (constant)")
    if learned and naive:
        a, b = learned.rows.get("all"), naive.rows.get("all")
        print(f"     learned segments beat constant speed: "
              f"{b.median_abs:.0f}s -> {a.median_abs:.0f}s median")
        ok = ok and a.median_abs < b.median_abs
        d = learned.rows.get("disrupted vehicle")
        n = learned.rows.get("normal operation")
        if d and n and n.median_abs:
            print(f"     and fall apart on a disrupted vehicle: "
                  f"{n.median_abs:.0f}s -> {d.median_abs:.0f}s "
                  f"({d.median_abs/n.median_abs:.0f}x worse)")

    print("\n" + ("all checks passed" if ok else "SOMETHING FAILED, see above"))
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import record
    from .llm import NVIDIA_MODEL, NVIDIA_URL, NemotronBackend
    from .timeutil import describe_tz

    print("environment")
    print(f"  timezone  {describe_tz()}")
    print()
    print("live feeds")
    for lane, status in record.doctor().items():
        print(f"  {lane:18s} {status}")
    backend = NemotronBackend()
    print("\ntriage backend")
    print(f"  endpoint  {NVIDIA_URL}")
    print(f"  model     {NVIDIA_MODEL}")
    print(f"  api key   {'set' if backend.available else 'MISSING (NVIDIA_API_KEY)'}")
    if not backend.available:
        print("  -> rules backend still works; `eval` marks the nemotron arm\n"
              "     as not run rather than scoring the fallback as the model")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Everything needed for a cold-start demo, in one command."""
    print("1/3  generating a labelled world")
    cmd_fixtures(argparse.Namespace(
        world=args.world, docs=DEFAULT_DOCS, hours=3.0, seed=7,
        start=1758300000,
    ))
    print("\n2/3  scoring the triage arms")
    cmd_eval(argparse.Namespace(
        world=args.world, arms=None, no_ablation=False, out="EVAL.md",
    ))
    print("\n3/3  starting the dashboard")
    return cmd_serve(argparse.Namespace(
        world=args.world, port=args.port, db=None, no_open=args.no_open,
    ))


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.world, port=args.port, db=args.db,
          open_browser=not args.no_open,
          live=getattr(args, "live", False), base=getattr(args, "base", None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dispatch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="list ingest adapters")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("fixtures", help="generate a labelled synthetic world")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--docs", default=DEFAULT_DOCS)
    p.add_argument("--hours", type=float, default=3.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--start", type=int, default=None)
    p.set_defaults(func=cmd_fixtures)

    p = sub.add_parser("replay", help="run a world through the pipeline")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--db", default=None)
    p.add_argument("--no-docs", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("eval", help="score triage arms against ground truth")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--arms", nargs="*", default=None)
    p.add_argument("--no-ablation", action="store_true")
    p.add_argument("--out", default="EVAL.md")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("predict-eval",
                       help="score arrival predictions against observed arrivals")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--gtfs", default=None)
    p.add_argument("--out", default="PREDICTIONS.md")
    p.add_argument("--mode", default="segment", choices=("segment", "speed"))
    p.add_argument("--horizon", type=int, default=1200,
                   help="refuse beyond this many seconds; 0 to never refuse")
    p.add_argument("--arms", action="store_true",
                   help="also score the constant-speed predictor for comparison")
    p.add_argument("--horizon-sweep", action="store_true")
    p.set_defaults(func=cmd_predict_eval)

    p = sub.add_parser("sweep", help="sweep a detector threshold")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--arm", default="rules")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("record", help="poll live feeds to disk")
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--duration", type=int, default=None)
    p.add_argument("--base", default=None,
                   help="override feed host, e.g. http://127.0.0.1:8800")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("gtfs", help="fetch PRT static GTFS for map geometry")
    p.add_argument("--out", default="fixtures/prt-gtfs.zip")
    p.add_argument("--skip-download", action="store_true",
                   help="just inspect the file already at --out")
    p.set_defaults(func=cmd_gtfs)

    p = sub.add_parser("verify",
                       help="rebuild, test, and re-derive every published number")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--start", type=int, default=1758300000)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("doctor", help="check feeds and model endpoint")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "demo", help="fixtures + eval + dashboard, one command")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="run the dashboard")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default=None)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="poll the real PRT feed and map the whole system")
    p.add_argument("--base", default=None,
                   help="override feed host (for tools/mock_prt.py)")
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
