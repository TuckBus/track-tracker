"""Tests for the parts that would fail silently.

Bias: cover the things that break without anyone noticing. A wrong colour in
the dashboard is obvious at a glance; a protobuf field read from the wrong
offset, an alert counted 500 times, or a lead-time metric measured against the
wrong timestamp all look completely fine on screen and quietly invalidate the
result you are showing a judge.

Three of these are regression tests for bugs that actually shipped into a
working demo during the build. They are marked REGRESSION.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatch import gtfsrt, wire  # noqa: E402
from dispatch.detect import Detector, haversine_m, load_terminal_stops  # noqa: E402
from dispatch.llm import RulesBackend, _loose_json, clamp_verdict  # noqa: E402
from dispatch.pipeline import Pipeline  # noqa: E402
from dispatch.schema import ItineraryItem, Observation, Signal, Verdict  # noqa: E402
from dispatch.simulate import World  # noqa: E402
from dispatch.sources.base import DOCUMENTS, TELEMETRY, load_documents  # noqa: E402

FIXED_NOW = 1758300000


def vehicle_feed(entries, timestamp=FIXED_NOW):
    """Build a VehiclePositions feed from (vehicle_id, route, lat, lon, ...)."""
    vehicles = []
    for e in entries:
        vehicles.append(
            gtfsrt.VehiclePos(
                entity_id=f"{e['vehicle_id']}@{e.get('timestamp', timestamp)}",
                vehicle_id=e["vehicle_id"],
                trip=gtfsrt.TripRef(trip_id=e.get("trip_id", "T1"),
                                    route_id=e["route_id"]),
                lat=e["lat"], lon=e["lon"],
                timestamp=e.get("timestamp", timestamp),
                current_status=e.get("status", "IN_TRANSIT_TO"),
                current_stop_sequence=e.get("seq", 5),
                stop_id=e.get("stop_id", ""),
            )
        )
    return gtfsrt.encode_feed(
        gtfsrt.Feed(timestamp=timestamp, version="2.0", vehicles=vehicles)
    )


class TestWireFormat(unittest.TestCase):
    """The protobuf reader is hand-written, so it gets the most scrutiny."""

    def test_varint_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 2**31, 2**53):
            buf = wire.write_varint(n)
            got, pos = wire.read_varint(buf, 0)
            self.assertEqual(got, n)
            self.assertEqual(pos, len(buf))

    def test_float_and_double_precision(self):
        lat = 40.44061
        raw = wire.enc_float(1, lat)
        fields = dict((f, v) for f, _w, v in
                      ((f, w, v) for f, w, v in wire.iter_fields(raw)))
        self.assertAlmostEqual(wire.as_float(fields[1]), lat, places=4)

    def test_feed_roundtrip_preserves_every_field(self):
        raw = vehicle_feed([{
            "vehicle_id": "3021", "route_id": "61C",
            "lat": 40.44061, "lon": -79.99589,
            "status": "STOPPED_AT", "seq": 7, "stop_id": "S12",
        }])
        feed = gtfsrt.decode_feed(raw)
        self.assertEqual(feed.timestamp, FIXED_NOW)
        self.assertEqual(len(feed.vehicles), 1)
        v = feed.vehicles[0]
        self.assertEqual(v.vehicle_id, "3021")
        self.assertEqual(v.trip.route_id, "61C")
        self.assertAlmostEqual(v.lat, 40.44061, places=4)
        self.assertEqual(v.current_status, "STOPPED_AT")
        self.assertEqual(v.current_stop_sequence, 7)
        self.assertEqual(v.stop_id, "S12")

    def test_alert_roundtrip(self):
        raw = gtfsrt.encode_feed(gtfsrt.Feed(
            timestamp=FIXED_NOW, version="2.0",
            alerts=[gtfsrt.ServiceAlert(
                entity_id="a1", effect="SIGNIFICANT_DELAYS",
                header="61C delays", description="Disabled bus.",
                route_ids=["61C"], active_start=FIXED_NOW - 600)],
        ))
        feed = gtfsrt.decode_feed(raw)
        self.assertEqual(len(feed.alerts), 1)
        a = feed.alerts[0]
        self.assertEqual(a.effect, "SIGNIFICANT_DELAYS")
        self.assertEqual(a.route_ids, ["61C"])
        self.assertEqual(a.active_start, FIXED_NOW - 600)

    def test_truncated_payload_raises_not_hangs(self):
        raw = vehicle_feed([{"vehicle_id": "1", "route_id": "61C",
                             "lat": 40.4, "lon": -79.9}])
        with self.assertRaises(Exception):
            gtfsrt.decode_feed(raw[:len(raw) // 2] + b"\xff")

    def test_unknown_fields_are_skipped(self):
        """Forward compatibility: PRT adding a field must not break us."""
        raw = vehicle_feed([{"vehicle_id": "1", "route_id": "61C",
                             "lat": 40.4, "lon": -79.9}])
        raw += wire.enc_string(99, "a field from the future")
        feed = gtfsrt.decode_feed(raw)
        self.assertEqual(len(feed.vehicles), 1)


class TestPortability(unittest.TestCase):
    """REGRESSION.

    Windows ships no IANA timezone database, so zoneinfo raised
    ZoneInfoNotFoundError at import time and 7 tests died with a traceback
    whose last line was the only clue. The project stays dependency-free, so
    the fix is to degrade to system local time rather than require `tzdata`.
    """

    def test_timezone_resolves_without_a_tz_database(self):
        import builtins

        from dispatch import timeutil

        timeutil._cache.clear()
        real_import = builtins.__import__

        def blocked(name, *a, **k):
            if name == "zoneinfo":
                raise ImportError("simulating a machine with no tzdata")
            return real_import(name, *a, **k)

        builtins.__import__ = blocked
        try:
            tz = timeutil.resolve_tz("America/New_York")
            self.assertIsNotNone(tz)
            # Must produce a usable, aware datetime, not just avoid raising.
            from datetime import datetime
            self.assertIsNotNone(datetime.now(tz).utcoffset())
            self.assertIn("no tz database", timeutil.describe_tz("America/New_York"))
        finally:
            builtins.__import__ = real_import
            timeutil._cache.clear()

    def test_clock_formatting_uses_no_platform_specific_codes(self):
        """REGRESSION.

        pipeline.py formatted draft times with "%-I:%M %p". The dash modifier
        is a GNU extension: it strips the leading zero on Linux and raises
        ValueError on Windows, so every INTERVENE draft crashed the pipeline
        on a Windows laptop while passing CI on Linux.
        """
        from dispatch.timeutil import fmt_clock, fmt_day

        # 2026-09-19 16:25 Eastern
        epoch = 1758313500
        out = fmt_clock(epoch)
        self.assertRegex(out, r"^\d{1,2}:\d{2} (AM|PM)$")
        self.assertFalse(out.startswith("0"), f"leading zero not stripped: {out}")
        self.assertEqual(fmt_clock(None), "")
        self.assertRegex(fmt_day(epoch), r"^[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{2}$")

    def test_no_gnu_only_strftime_codes_anywhere(self):
        """Guard the whole package, not just the one line that broke."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "dispatch"
        offenders = []
        for path in root.rglob("*.py"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if "strftime" in line and ("%-" in line or "%#" in line):
                    offenders.append(f"{path.name}:{i}")
        self.assertEqual(offenders, [], f"non-portable strftime in {offenders}")

    def test_document_times_are_not_read_as_utc(self):
        """The old fallback read local departure times as UTC: a 4 hour error
        that did not raise and silently corrupted every slack calculation."""
        from dispatch.llm import _parse_local
        from dispatch.timeutil import resolve_tz

        epoch = _parse_local("2026-09-19 14:30")
        self.assertIsNotNone(epoch)
        from datetime import datetime
        local = datetime.fromtimestamp(epoch, tz=resolve_tz())
        self.assertEqual((local.hour, local.minute), (14, 30))


class TestPrediction(unittest.TestCase):
    """The arrival predictor, including the cases where it refuses to answer."""

    def _geo(self):
        from dispatch.predict import RouteGeometry
        # A straight line east along a constant latitude, ~430m between points.
        pts = [(40.4400, -79.9600 + i * 0.005) for i in range(6)]
        g = RouteGeometry(route_id="TEST", points=pts)
        g.attach_stops([(f"S{i}", pts[i][0], pts[i][1]) for i in range(6)])
        return g

    def _obs(self, lat, lon, t, vid="v1"):
        return Observation(source_type="prt-bus", vehicle_id=vid,
                           route_id="TEST", trip_id="T1", observed_at=t,
                           lat=lat, lon=lon, status="IN_TRANSIT_TO")

    def test_projection_puts_an_on_route_point_on_the_route(self):
        g = self._geo()
        along, off = g.project(40.4400, -79.9550)
        self.assertLess(off, 5.0)
        self.assertGreater(along, 0)
        self.assertLess(along, g.length)

    def test_stops_ahead_are_only_ahead(self):
        g = self._geo()
        mid = g.length / 2
        ahead = g.stops_ahead(mid, limit=10)
        self.assertTrue(all(d > 0 for _sid, d in ahead))
        self.assertTrue(all(b >= a for (_x, a), (_y, b) in
                            zip(ahead, ahead[1:])))

    def test_moving_vehicle_gets_a_believable_eta(self):
        from dispatch.predict import Predictor
        g = self._geo()
        p = Predictor({"TEST": g})
        # 10 m/s east for two minutes.
        for i in range(7):
            t = FIXED_NOW + i * 20
            lon = -79.9600 + (i * 200) / (111_320 * math.cos(math.radians(40.44)))
            p.observe([self._obs(40.4400, lon, t)], t)
        key = next(iter(p.tracks))
        speed, n = p.tracks[key].speed_mps()
        self.assertGreater(speed, 5.0)
        self.assertLess(speed, 15.0)
        preds = p.predict_vehicle(key, now=FIXED_NOW + 120)
        self.assertTrue(preds)
        first = preds[0]
        self.assertEqual(first.basis, "speed")
        self.assertIsNotNone(first.eta_s)
        # distance / speed, within a wide tolerance
        self.assertAlmostEqual(first.eta_s, first.distance_m / speed, delta=30)

    def test_stationary_vehicle_is_refused_not_guessed(self):
        """REGRESSION-shaped: dividing by a speed of zero is worse than silence."""
        from dispatch.predict import Predictor
        p = Predictor({"TEST": self._geo()})
        for i in range(8):
            t = FIXED_NOW + i * 20
            p.observe([self._obs(40.4400, -79.9550, t)], t)
        key = next(iter(p.tracks))
        preds = p.predict_vehicle(key, now=FIXED_NOW + 160)
        self.assertTrue(preds)
        self.assertEqual(preds[0].basis, "stalled")
        self.assertIsNone(preds[0].eta_s)
        self.assertEqual(preds[0].confidence, 0.0)

    def test_new_vehicle_has_insufficient_data(self):
        from dispatch.predict import Predictor
        p = Predictor({"TEST": self._geo()})
        p.observe([self._obs(40.4400, -79.9590, FIXED_NOW)], FIXED_NOW)
        key = next(iter(p.tracks))
        self.assertEqual(p.predict_vehicle(key, now=FIXED_NOW)[0].basis,
                         "insufficient_data")

    def test_new_trip_resets_the_speed_estimate(self):
        """A bus starting its next trip jumps backwards along the shape. Without
        a reset the speed estimate goes negative and every ETA is nonsense."""
        from dispatch.predict import Predictor
        g = self._geo()
        p = Predictor({"TEST": g})
        for i in range(6):
            t = FIXED_NOW + i * 20
            lon = -79.9600 + (i * 300) / (111_320 * math.cos(math.radians(40.44)))
            p.observe([self._obs(40.4400, lon, t)], t)
        key = next(iter(p.tracks))
        before = p.tracks[key].trips
        # back to the start of the route
        p.observe([self._obs(40.4400, -79.9600, FIXED_NOW + 140)], FIXED_NOW + 140)
        self.assertEqual(p.tracks[key].trips, before + 1)
        self.assertGreaterEqual(p.tracks[key].speed_mps()[0], 0.0)

    def test_arrivals_board_puts_refusals_last(self):
        from dispatch.predict import Predictor
        g = self._geo()
        p = Predictor({"TEST": g})
        k = 111_320 * math.cos(math.radians(40.44))
        for i in range(7):
            t = FIXED_NOW + i * 20
            p.observe([self._obs(40.4400, -79.9600 + (i * 200) / k, t, "moving")], t)
            p.observe([self._obs(40.4400, -79.9585, t, "parked")], t)
        board = p.arrivals("S4", now=FIXED_NOW + 120)
        self.assertGreaterEqual(len(board), 2)
        self.assertIsNotNone(board[0].eta_s)
        self.assertIsNone(board[-1].eta_s)

    def test_learned_segments_beat_constant_speed(self):
        """The segment model has to earn its place, not be asserted.

        Also guards the fixture: if the simulator ever loses its per-location
        speed profiles, this fails, because a world with one uniform speed
        cannot distinguish the two predictors. That is how the null result
        happened the first time.
        """
        from dispatch import evaluate_predictions as ep
        world = _shared_world()
        arms = dict(ep.compare(world, gtfs=os.path.join(world, "gtfs-static.zip")))
        naive = arms["speed (constant)"].rows.get("all")
        learned = arms["segment (learned)"].rows.get("all")
        self.assertTrue(naive and naive.n and learned and learned.n)
        self.assertLess(learned.median_abs, naive.median_abs,
                        "learned segments did not beat constant speed; has the "
                        "fixture lost its speed profiles?")
        self.assertLess(learned.p90_abs, naive.p90_abs)

    def test_horizon_policy_refuses_instead_of_guessing_far_ahead(self):
        from dispatch.predict import Predictor
        g = self._geo()
        p = Predictor({"TEST": g}, mode="speed", horizon_s=60)
        k = 111_320 * math.cos(math.radians(40.44))
        for i in range(7):
            t = FIXED_NOW + i * 20
            p.observe([self._obs(40.4400, -79.9600 + (i * 100) / k, t)], t)
        key = next(iter(p.tracks))
        preds = p.predict_vehicle(key, limit=6, now=FIXED_NOW + 120)
        far = [x for x in preds if x.basis == "beyond_horizon"]
        self.assertTrue(far, "nothing was refused despite a 60s horizon")
        self.assertTrue(all(x.eta_s is None for x in far))

    def test_segment_speeds_learn_a_slow_stretch(self):
        from dispatch.predict import SegmentSpeeds
        seg = SegmentSpeeds(route_id="TEST", length=2000.0)
        # 2 m/s through the first 400m, 12 m/s through the next 400m
        for _ in range(3):
            seg.record(0, 400, 200)
            seg.record(400, 800, 33)
        slow = seg.speeds[seg.index(200)]
        fast = seg.speeds[seg.index(600)]
        self.assertLess(slow, 4.0)
        self.assertGreater(fast, 8.0)
        # integrating should cost more than the fast stretch alone
        t = seg.travel_time(0, 800, fallback_mps=8.0)
        self.assertGreater(t, 800 / 12.0)

    def test_prediction_eval_finds_the_disruption_gap(self):
        """The headline finding: good on a moving bus, useless on a broken one."""
        from dispatch import evaluate_predictions as ep
        world = _shared_world()
        rep = ep.run(world)
        self.assertGreater(rep.scored, 500)
        normal = rep.rows.get("normal operation")
        bad = rep.rows.get("disrupted vehicle")
        self.assertTrue(normal and normal.n, "no normal-operation predictions")
        self.assertTrue(bad and bad.n, "no disrupted-vehicle predictions")
        self.assertLess(normal.median_abs, 120)
        self.assertGreater(bad.median_abs, normal.median_abs * 5)
        # refusals must be counted, not silently scored
        self.assertIn("stalled", rep.basis_counts)


class TestGeo(unittest.TestCase):
    def test_haversine_against_known_distance(self):
        # Cathedral of Learning to the Carnegie Museum, roughly 500m.
        d = haversine_m(40.44417, -79.95306, 40.44333, -79.94944)
        self.assertTrue(300 < d < 700, f"got {d}m")

    def test_zero_distance(self):
        self.assertEqual(haversine_m(40.0, -79.0, 40.0, -79.0), 0.0)


class TestDocumentAdapters(unittest.TestCase):
    """Xtract: every registered format parses, and offsets are real."""

    @classmethod
    def setUpClass(cls):
        from dispatch.fixture_docs import write_all
        cls.tmp = tempfile.mkdtemp()
        cls.paths = write_all(cls.tmp, FIXED_NOW)
        cls.docs = load_documents(cls.paths)

    def test_every_registered_adapter_has_a_fixture(self):
        seen = {d.source_type for d in self.docs}
        missing = set(DOCUMENTS) - seen
        self.assertEqual(missing, set(), f"no fixture exercises {missing}")

    def test_all_documents_yield_text(self):
        for d in self.docs:
            self.assertTrue(d.text.strip(), f"{d.source_id} produced no text")

    def test_locator_offsets_are_within_the_text(self):
        for d in self.docs:
            for offset, name in d.locator_map:
                self.assertGreaterEqual(offset, 0)
                self.assertLessEqual(offset, len(d.text))
                self.assertTrue(name)

    def test_extracted_field_spans_contain_the_claimed_value(self):
        """Provenance has to be verifiable, not decorative.

        For every extracted leg, the character span recorded for route_hint
        must actually contain that route number in the source document. This
        is the property that lets the dashboard highlight evidence.
        """
        backend = RulesBackend()
        checked = 0
        for doc in self.docs:
            for leg in backend.extract(doc):
                prov = leg.field_provenance.get("route_hint")
                if not prov or prov.char_start is None:
                    continue
                span = doc.text[prov.char_start:prov.char_end]
                # Whitespace is normalised on both sides: a flight prints as
                # "AA 1729" but canonicalises to "AA1729", and provenance
                # verification has to tolerate that without tolerating a
                # wrong offset.
                self.assertIn(
                    re.sub(r"\s+", "", leg.route_hint),
                    re.sub(r"\s+", "", span),
                    f"{doc.source_id}: span {span!r} does not support "
                    f"{leg.route_hint!r}",
                )
                checked += 1
        self.assertGreater(checked, 0, "no spans were checked")

    def test_known_values_are_found(self):
        backend = RulesBackend()
        hints = set()
        for doc in self.docs:
            for leg in backend.extract(doc):
                if leg.route_hint:
                    hints.add(leg.route_hint)
        for expected in ("61C", "28X", "43"):
            self.assertIn(expected, hints)

    def test_unknown_extension_degrades_to_text(self):
        path = os.path.join(self.tmp, "mystery.xyzzy")
        with open(path, "w") as fh:
            fh.write("Route 71B departs at 9:15 AM")
        docs = load_documents([path])
        self.assertEqual(len(docs), 1)
        self.assertIn("71B", docs[0].text)


class TestDetector(unittest.TestCase):
    def _stream(self, detector, lat, lon, *, minutes, route="61C",
                vehicle="v1", status="IN_TRANSIT_TO", stop_id="", seq=5):
        """Feed the same position for `minutes` and collect signals."""
        signals = []
        for i in range(int(minutes * 3)):  # 20s ticks
            t = FIXED_NOW + i * 20
            obs = Observation(
                source_type="prt-bus", vehicle_id=vehicle, route_id=route,
                trip_id="T1", observed_at=t, lat=lat, lon=lon,
                status=status, stop_id=stop_id, stop_sequence=seq,
            )
            signals.extend(detector.ingest([obs], t))
        return signals

    def test_stall_fires_after_threshold(self):
        det = Detector(layover_aware=False)
        signals = self._stream(det, 40.44, -79.95, minutes=10)
        self.assertTrue(any(s.kind == "STALL" for s in signals))

    def test_no_stall_below_threshold(self):
        det = Detector(layover_aware=False)
        signals = self._stream(det, 40.44, -79.95, minutes=5)
        self.assertFalse(any(s.kind == "STALL" for s in signals))

    def test_stall_emitted_once_not_every_snapshot(self):
        """The whole product depends on not re-alerting every 20 seconds."""
        det = Detector(layover_aware=False)
        signals = self._stream(det, 40.44, -79.95, minutes=30)
        self.assertEqual(len([s for s in signals if s.kind == "STALL"]), 1)

    def test_terminal_stop_suppresses_stall(self):
        det = Detector(terminal_stops={"TERM1"}, layover_aware=True)
        signals = self._stream(det, 40.44, -79.95, minutes=20,
                               status="STOPPED_AT", stop_id="TERM1", seq=0)
        self.assertFalse(any(s.kind == "STALL" for s in signals))

    def test_layover_join_is_what_suppresses_it(self):
        """Same input, join disabled, signal appears. Prices the ablation."""
        det = Detector(terminal_stops={"TERM1"}, layover_aware=False)
        signals = self._stream(det, 40.44, -79.95, minutes=20,
                               status="STOPPED_AT", stop_id="TERM1", seq=0)
        self.assertTrue(any(s.kind == "STALL" for s in signals))

    def test_movement_resets_the_stall_latch(self):
        det = Detector(layover_aware=False)
        first = self._stream(det, 40.44, -79.95, minutes=10)
        self.assertEqual(len([s for s in first if s.kind == "STALL"]), 1)
        # Move 2km away, then stall again: must re-fire.
        second = []
        for i in range(40):
            t = FIXED_NOW + 3000 + i * 20
            obs = Observation(
                source_type="prt-bus", vehicle_id="v1", route_id="61C",
                trip_id="T1", observed_at=t, lat=40.46, lon=-79.97,
                status="IN_TRANSIT_TO", stop_sequence=5,
            )
            second.extend(det.ingest([obs], t))
        self.assertTrue(any(s.kind == "STALL" for s in second))

    def test_jitter_feature_is_tight_for_a_stationary_vehicle(self):
        det = Detector(layover_aware=False)
        signals = self._stream(det, 40.44, -79.95, minutes=10)
        stall = next(s for s in signals if s.kind == "STALL")
        self.assertIn("jitter_m", stall.evidence)
        self.assertLess(stall.evidence["jitter_m"], 5.0)

    def test_vanished_fires_after_silence(self):
        det = Detector(layover_aware=False, vanish_seconds=600)
        obs = Observation(source_type="prt-bus", vehicle_id="v9",
                          route_id="61C", trip_id="T1", observed_at=FIXED_NOW,
                          lat=40.44, lon=-79.95, status="IN_TRANSIT_TO",
                          stop_sequence=5)
        det.ingest([obs], FIXED_NOW)
        signals = det.ingest([], FIXED_NOW + 700)
        self.assertTrue(any(s.kind == "VANISHED" for s in signals))

    def test_thresholds_are_injectable(self):
        det = Detector(layover_aware=False, stall_seconds=120)
        signals = self._stream(det, 40.44, -79.95, minutes=4)
        self.assertTrue(any(s.kind == "STALL" for s in signals))


class TestStaticGtfs(unittest.TestCase):
    def test_terminal_stops_are_first_and_last_of_each_trip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "stop_times.txt",
                "trip_id,stop_id,stop_sequence\n"
                "T1,A,0\nT1,B,1\nT1,C,2\n"
                "T2,X,0\nT2,Y,1\n",
            )
        path = tempfile.mktemp(suffix=".zip")
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        terminals = load_terminal_stops(path)
        self.assertEqual(terminals, {"A", "C", "X", "Y"})

    def test_missing_zip_returns_empty_not_crash(self):
        self.assertEqual(load_terminal_stops("/nonexistent.zip"), set())


class TestTriageInvariants(unittest.TestCase):
    """Guardrails that live in code, not in the prompt.

    A model asked nicely not to escalate will sometimes escalate anyway, so
    the limits that matter are enforced after the model returns.
    """

    def _signal(self):
        return Signal(kind="STALL", route_id="61C", vehicle_id="v1",
                      source_type="prt-bus", detected_at=FIXED_NOW,
                      first_seen_at=FIXED_NOW - 600)

    def test_cannot_exceed_log_without_an_itinerary_match(self):
        v = Verdict(act="INTERVENE", severity=3, reason_code="MODEL_SAID_SO",
                    confidence=0.99)
        clamped = clamp_verdict(v, None, FIXED_NOW, {})
        self.assertIn(clamped.act, ("SUPPRESS", "LOG"))

    def test_scheduled_layover_evidence_forces_suppress(self):
        item = ItineraryItem(kind="transit", label="Route 61C",
                             depart_at=FIXED_NOW + 600, route_hint="61C")
        v = Verdict(act="INTERVENE", severity=3, reason_code="X",
                    confidence=0.9)
        clamped = clamp_verdict(v, item, FIXED_NOW, {"scheduled_layover": True})
        self.assertEqual(clamped.act, "SUPPRESS")

    def test_loose_json_survives_a_fenced_prose_reply(self):
        """The failure mode that actually happens with chat models."""
        raw = ('Sure! Here is the analysis:\n\n```json\n'
               '{"act":"NOTIFY","severity":2,"reason_code":"OK",'
               '"confidence":0.7}\n```\nHope that helps.')
        data = _loose_json(raw)
        self.assertEqual(data["act"], "NOTIFY")
        self.assertEqual(data["severity"], 2)

    def test_reasoning_preamble_does_not_hijack_the_verdict(self):
        """REGRESSION.

        Nemotron 3.5 Lightning narrates before answering, and the narration
        contains draft JSON. Taking the first balanced object returned the
        model's rough work instead of its conclusion. Scan back to front and
        prefer an object carrying an answer key.
        """
        raw = (
            "Here's a thinking process:\n"
            "1. Looks like a stall. Maybe {\"act\": \"LOG\"}?\n"
            "2. No, slack is 4 minutes.\n"
            "Final answer:\n"
            '{"act":"INTERVENE","severity":3,"reason_code":"SLACK_EXHAUSTED",'
            '"confidence":0.8}'
        )
        self.assertEqual(_loose_json(raw)["act"], "INTERVENE")

    def test_think_blocks_are_stripped(self):
        out = _loose_json('<think>{"act":"LOG"}</think>{"act":"NOTIFY"}')
        self.assertEqual(out["act"], "NOTIFY")

    def test_loose_json_raises_on_genuine_garbage(self):
        with self.assertRaises(Exception):
            _loose_json("I'm afraid I can't help with that.")


class TestAlertHandling(unittest.TestCase):
    def _alert_feed(self, t, active_start):
        return gtfsrt.encode_feed(gtfsrt.Feed(
            timestamp=t, version="2.0",
            alerts=[gtfsrt.ServiceAlert(
                entity_id="alert-1", effect="SIGNIFICANT_DELAYS",
                header="61C delays", description="d",
                route_ids=["61C"], active_start=active_start)],
        ))

    def test_published_at_is_first_sighting_not_active_start(self):
        """REGRESSION.

        Agencies backdate active_period.start to when the disruption began.
        Using it as publication time made detections look 80 minutes *late*
        and silently destroyed the headline metric.
        """
        onset = FIXED_NOW - 3600
        raw = self._alert_feed(FIXED_NOW, onset)
        _obs, alerts = TELEMETRY["prt-bus"].parse(raw, FIXED_NOW)
        self.assertEqual(alerts[0].published_at, FIXED_NOW)
        self.assertEqual(alerts[0].active_start, onset)

    def test_republished_alerts_are_deduped(self):
        """REGRESSION.

        GTFS-RT republishes every active alert on every poll. A 3-hour replay
        produced 1,578 rows for six alerts.
        """
        pipe = Pipeline(backend=RulesBackend(), detector=Detector())
        for i in range(50):
            t = FIXED_NOW + i * 20
            _obs, alerts = TELEMETRY["prt-bus"].parse(
                self._alert_feed(t, FIXED_NOW), t)
            pipe.observe([], alerts, t)
        self.assertEqual(len(pipe.official), 1)

    def test_dedupe_keeps_the_earliest_publication_time(self):
        pipe = Pipeline(backend=RulesBackend(), detector=Detector())
        for t in (FIXED_NOW + 600, FIXED_NOW, FIXED_NOW + 300):
            _obs, alerts = TELEMETRY["prt-bus"].parse(
                self._alert_feed(t, FIXED_NOW), t)
            pipe.observe([], alerts, t)
        self.assertEqual(pipe.official[0].published_at, FIXED_NOW)


class TestEvalIntegrity(unittest.TestCase):
    def test_nemotron_arm_refuses_to_run_without_a_key(self):
        """REGRESSION.

        A missing NVIDIA_API_KEY produced a full set of rules-backend
        verdicts labelled 'nemotron' with an identical F1. That number would
        have gone in front of a judge.
        """
        from dispatch import evaluate

        world = _shared_world()
        saved = os.environ.pop("NVIDIA_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                evaluate.run_arm(world, "nemotron")
        finally:
            if saved is not None:
                os.environ["NVIDIA_API_KEY"] = saved

    def test_lead_time_is_only_credited_to_true_positives(self):
        from dispatch import evaluate

        world = _shared_world()
        res, _pipe = evaluate.run_arm(world, "rules")
        self.assertLessEqual(len(res.lead_times_s), res.tp)


class TestEndToEnd(unittest.TestCase):
    def test_every_real_disruption_on_the_itinerary_is_surfaced(self):
        from dispatch import evaluate

        world = _shared_world()
        res, _pipe = evaluate.run_arm(world, "rules")
        self.assertEqual(res.fn, 0, f"missed {res.fn_by_class}")
        self.assertEqual(res.recall, 1.0)

    def test_triage_beats_no_triage_on_precision(self):
        from dispatch import evaluate

        world = _shared_world()
        raw, _ = evaluate.run_arm(world, "detector")
        triaged, _ = evaluate.run_arm(world, "rules")
        self.assertGreater(triaged.precision, raw.precision)

    def test_nothing_is_ever_sent(self):
        """Drafts only. An agent that emails your colleagues on a model's
        judgement is a liability, so the send path must not exist."""
        from dispatch import replay

        world = _shared_world()
        pipe = replay.run(world, doc_paths=replay.default_docs(world))
        for draft in pipe.drafts:
            self.assertIn("DRAFTED", draft["status"])

    def test_replay_is_deterministic(self):
        from dispatch import replay

        world = _shared_world()
        a = replay.run(world, doc_paths=replay.default_docs(world))
        b = replay.run(world, doc_paths=replay.default_docs(world))
        self.assertEqual(
            [f.signal.signal_id for f in a.findings],
            [f.signal.signal_id for f in b.findings],
        )


_WORLD_DIR: str | None = None


def _shared_world() -> str:
    """Build one labelled world for the whole suite."""
    global _WORLD_DIR
    if _WORLD_DIR is None:
        tmp = tempfile.mkdtemp()
        world = os.path.join(tmp, "world")
        World(start_epoch=FIXED_NOW, hours=3.0, seed=7).write(world)
        from dispatch.fixture_docs import write_all
        write_all(os.path.join(world, "docs"), FIXED_NOW)
        _WORLD_DIR = world
    return _WORLD_DIR


class TestSimulatorLabels(unittest.TestCase):
    def test_scenarios_do_not_share_vehicles(self):
        """Two scenarios on one vehicle makes the second unobservable."""
        world = World(start_epoch=FIXED_NOW, hours=3.0, seed=7)
        ids = [s.vehicle_id for s in world.scenarios]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ground_truth_requires_disruption_and_relevance(self):
        world = World(start_epoch=FIXED_NOW, hours=3.0, seed=7)
        for sc in world.scenarios:
            expected = sc.disruptive and sc.on_itinerary
            self.assertEqual(sc.should_notify, expected)

    def test_benign_scenarios_are_the_majority(self):
        """If the negatives are rare the precision number means nothing."""
        world = World(start_epoch=FIXED_NOW, hours=3.0, seed=7)
        positives = sum(1 for s in world.scenarios if s.should_notify)
        self.assertLess(positives, len(world.scenarios) / 2)

    def test_regenerating_clears_the_previous_generation(self):
        """REGRESSION.

        Snapshots are named by feed timestamp, so regenerating with a
        different --start left the old files behind and replay read both
        timelines interleaved. Vehicles teleported between generations, the
        stall anchor reset on every jump, and recall silently fell from 1.00
        to 0.50 with no error raised.
        """
        tmp = tempfile.mkdtemp()
        world = os.path.join(tmp, "w")
        World(start_epoch=FIXED_NOW, hours=0.3, seed=7).write(world)
        first = set(os.listdir(os.path.join(world, "prt-bus")))

        World(start_epoch=FIXED_NOW + 500_000, hours=0.3, seed=7).write(world)
        second = set(os.listdir(os.path.join(world, "prt-bus")))

        self.assertFalse(first & second, "stale snapshots survived regeneration")

    def test_eval_refuses_a_world_with_stale_snapshots(self):
        from dispatch import evaluate

        tmp = tempfile.mkdtemp()
        world = os.path.join(tmp, "w")
        World(start_epoch=FIXED_NOW, hours=0.3, seed=7).write(world)
        # Plant a snapshot from a different era, as a second `fixtures` run did.
        lane = os.path.join(world, "prt-bus")
        stray = sorted(os.listdir(lane))[0]
        with open(os.path.join(lane, stray), "rb") as fh:
            blob = fh.read()
        with open(os.path.join(lane, f"{FIXED_NOW + 900_000}.pb"), "wb") as fh:
            fh.write(blob)

        with self.assertRaises(RuntimeError) as ctx:
            evaluate.run_arm(world, "rules")
        self.assertIn("stale", str(ctx.exception).lower())

    def test_fixtures_parse_through_the_live_adapter(self):
        world = World(start_epoch=FIXED_NOW, hours=0.2, seed=7)
        raw, _alerts = world.snapshot(FIXED_NOW)
        obs, _ = TELEMETRY["prt-bus"].parse(raw, FIXED_NOW)
        self.assertEqual(len(obs), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
