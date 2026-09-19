"""Replay a directory of raw feed snapshots through the pipeline.

The recorder writes raw bytes; replay reads them back through the same
adapters. That is deliberate. It means:

  - the demo is deterministic, which matters when the venue wifi dies;
  - the eval is reproducible, because the same bytes produce the same
    Signals every time;
  - a bug found at judging can be reproduced later from the recording.

Snapshot layout, one file per poll, named by feed timestamp:

    <dir>/prt-bus/<epoch>.pb
    <dir>/prt-bus-alerts/<epoch>.pb
    <dir>/amtrak/<epoch>.json        (optional)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .detect import Detector, load_terminal_stops
from .llm import Backend, get_backend
from .pipeline import Pipeline, Store
from .schema import Observation, OfficialAlert
from .sources.base import TELEMETRY, load_documents

LANES = (
    # directory name, adapter name
    ("prt-bus", "prt-bus"),
    ("prt-bus-alerts", "prt-bus"),
    ("prt-rail", "prt-rail"),
    ("prt-rail-alerts", "prt-rail"),
    ("amtrak", "amtrak"),
)


@dataclass
class Snapshot:
    epoch: int
    observations: list[Observation]
    alerts: list[OfficialAlert]


def _read_lane(root: str, lane: str, adapter: str) -> dict[int, tuple[list, list]]:
    path = os.path.join(root, lane)
    if not os.path.isdir(path):
        return {}
    src = TELEMETRY.get(adapter)
    if src is None:
        return {}
    out: dict[int, tuple[list, list]] = {}
    for name in os.listdir(path):
        stem, _, ext = name.rpartition(".")
        if ext not in ("pb", "json") or not stem.isdigit():
            continue
        epoch = int(stem)
        with open(os.path.join(path, name), "rb") as fh:
            blob = fh.read()
        try:
            obs, alerts = src.parse(blob, epoch)
        except Exception:
            # A corrupt snapshot should cost us one frame, not the run.
            continue
        out[epoch] = (obs, alerts)
    return out


def load_snapshots(root: str) -> list[Snapshot]:
    """Merge every lane into one timeline ordered by feed timestamp."""
    merged: dict[int, Snapshot] = {}
    for lane, adapter in LANES:
        for epoch, (obs, alerts) in _read_lane(root, lane, adapter).items():
            snap = merged.setdefault(epoch, Snapshot(epoch, [], []))
            snap.observations.extend(obs)
            snap.alerts.extend(alerts)
    return [merged[k] for k in sorted(merged)]


def truth_path(root: str) -> str:
    return os.path.join(root, "truth.json")


def load_truth(root: str) -> dict | None:
    path = truth_path(root)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def run(
    root: str,
    *,
    backend: Backend | None = None,
    layover_aware: bool = True,
    doc_paths: list[str] | None = None,
    store_path: str | None = None,
    quiet: bool = True,
) -> Pipeline:
    """Replay every snapshot under `root` and return the finished Pipeline."""
    gtfs_zip = os.path.join(root, "gtfs-static.zip")
    terminals = load_terminal_stops(gtfs_zip) if os.path.exists(gtfs_zip) else set()

    pipe = Pipeline(
        backend=backend or get_backend(),
        detector=Detector(terminal_stops=terminals, layover_aware=layover_aware),
        store=Store(store_path) if store_path else None,
    )

    if doc_paths:
        pipe.load_documents(load_documents(doc_paths))

    snapshots = load_snapshots(root)
    for i, snap in enumerate(snapshots):
        pipe.observe(snap.observations, snap.alerts, snap.epoch)
        if not quiet and i % 50 == 0:
            print(f"  .. {i}/{len(snapshots)} snapshots, "
                  f"{len(pipe.findings)} findings")

    # Alerts that arrive after a signal fires still count for lead time.
    pipe.reconcile_alerts()
    return pipe


def default_docs(root: str) -> list[str]:
    docs_dir = os.path.join(root, "docs")
    if not os.path.isdir(docs_dir):
        docs_dir = os.path.join("fixtures", "docs")
    if not os.path.isdir(docs_dir):
        return []
    return [
        os.path.join(docs_dir, n)
        for n in sorted(os.listdir(docs_dir))
        if not n.startswith(".")
    ]
