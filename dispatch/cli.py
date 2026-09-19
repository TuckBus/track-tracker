"""Dispatch command line.

    python3 -m dispatch.cli sources            what can be ingested
    python3 -m dispatch.cli fixtures           build a labelled world
    python3 -m dispatch.cli replay             run a world through the pipeline
    python3 -m dispatch.cli eval               score the triage arms
    python3 -m dispatch.cli sweep              tune a detector threshold
    python3 -m dispatch.cli record --out DIR   poll the live feeds
    python3 -m dispatch.cli doctor             feeds and model endpoint reachable
    python3 -m dispatch.cli serve              the dashboard
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


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.world, port=args.port, db=args.db,
          open_browser=not args.no_open)
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

    p = sub.add_parser("doctor", help="check feeds and model endpoint")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="run the dashboard")
    p.add_argument("--world", default=DEFAULT_WORLD)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default=None)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
