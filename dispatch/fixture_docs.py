"""Itinerary documents for the Xtract lane.

Generated rather than checked in, because their departure times have to land
inside whatever window the simulator is running. A confirmation email for a bus
that left in 2023 tells you nothing about whether extraction works.

Six formats, one per registered document adapter, so `dispatch sources` and the
dashboard are demonstrating real coverage rather than a claim about coverage.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from .timeutil import resolve_tz

# Resolved lazily-ish through a helper so a machine with no tz database
# (Windows without the optional `tzdata` package) still works. See
# dispatch/timeutil.py.
TZ = resolve_tz()


def _local(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(TZ)


def _clock(epoch: int) -> str:
    dt = _local(epoch)
    return dt.strftime("%I:%M %p").lstrip("0")


def _datestamp(epoch: int) -> str:
    return _local(epoch).strftime("%a, %b %d, %Y")


def _rfc2822(epoch: int) -> str:
    return _local(epoch).strftime("%a, %d %b %Y %H:%M:%S %z")


def write_documents(out_dir: str, start_epoch: int) -> list[str]:
    """Write one document per adapter. Returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    paths: list[str] = []

    bus_depart = start_epoch + 40 * 60      # 61C, threatened by the 18-min stall
    airport_depart = start_epoch + 100 * 60  # 28X, threatened by the 52-min stall
    train_depart = start_epoch + 150 * 60
    class_start = start_epoch + 45 * 60

    # -- email: the bus leg ------------------------------------------------
    eml = f"""From: Panther Dining Scheduling <scheduling@example-dining.com>
To: you@pitt.edu
Subject: Shift confirmation for {_datestamp(bus_depart)}
Date: {_rfc2822(start_epoch - 7200)}
Message-ID: <shift-8821@example-dining.com>
Content-Type: text/plain; charset="utf-8"

Your shift is confirmed.

Date: {_datestamp(bus_depart)}
Report time: {_clock(bus_depart + 25 * 60)}
Location: Litchfield Towers, Pittsburgh PA

Suggested travel: Route 61C departing Forbes & Murray at {_clock(bus_depart)}.
Travel time is about 22 minutes. Please arrive five minutes early to badge in.

Late arrivals must be reported to the shift lead before the start of the shift.
"""
    p = os.path.join(out_dir, "shift-confirmation.eml")
    with open(p, "w") as fh:
        fh.write(eml)
    paths.append(p)

    # -- email: the airport leg, with a flight on it -----------------------
    eml2 = f"""From: Trips <no-reply@example-air.com>
To: you@pitt.edu
Subject: You're booked: PIT to BOS
Date: {_rfc2822(start_epoch - 86400)}
Message-ID: <bk-41190@example-air.com>
Content-Type: text/plain; charset="utf-8"

Confirmation JZ4Q1M

Flight AA 1729
Departs PIT {_datestamp(airport_depart)} at {_clock(airport_depart + 95 * 60)}
Arrives BOS 
Bag drop closes 45 minutes before departure.

Getting there: Route 28X Airport Flyer, boarding downtown at
{_clock(airport_depart)}. Allow 55 minutes to the terminal.
"""
    p = os.path.join(out_dir, "airport-booking.eml")
    with open(p, "w") as fh:
        fh.write(eml2)
    paths.append(p)

    # -- plain text: Amtrak eTicket ---------------------------------------
    txt = f"""AMTRAK eTICKET - DO NOT DISCARD

Reservation N4K2QD
Train 43 Pennsylvanian
{_datestamp(train_depart)}

Departs PITTSBURGH, PA (PGH) at {_clock(train_depart)}
Arrives PHILADELPHIA, PA (PHL)

One adult, coach. Seat assigned at boarding.
This train operates once daily. Missed departures are not rebooked
automatically; contact 1-800-USA-RAIL.
"""
    p = os.path.join(out_dir, "amtrak-eticket.txt")
    with open(p, "w") as fh:
        fh.write(txt)
    paths.append(p)

    # -- ics: a fixed commitment ------------------------------------------
    ics_dt = datetime.fromtimestamp(class_start, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    ics_end = datetime.fromtimestamp(class_start + 4500, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Pitt//Course Schedule//EN
BEGIN:VEVENT
UID:ece0401-recitation@pitt.edu
DTSTAMP:{ics_dt}
DTSTART:{ics_dt}
DTEND:{ics_end}
SUMMARY:ECE 0401 recitation - quiz 3
LOCATION:Benedum Hall 319
DESCRIPTION:Quiz counts toward final grade. No makeups.
END:VEVENT
END:VCALENDAR
"""
    p = os.path.join(out_dir, "course-schedule.ics")
    with open(p, "w") as fh:
        fh.write(ics)
    paths.append(p)

    # -- csv: a roster the user is on -------------------------------------
    csv_text = (
        "date,role,report_time,route,notes\n"
        f"{_local(bus_depart).strftime('%Y-%m-%d')},closer,"
        f"{_local(bus_depart + 25 * 60).strftime('%H:%M')},Route 61C,"
        "badge in at side entrance\n"
        f"{_local(train_depart).strftime('%Y-%m-%d')},off,,,travel day\n"
    )
    p = os.path.join(out_dir, "shift-roster.csv")
    with open(p, "w") as fh:
        fh.write(csv_text)
    paths.append(p)

    # -- html: a booking page saved from a browser ------------------------
    html = f"""<!doctype html>
<html><head><title>Reservation N4K2QD</title></head>
<body>
<h1>Your trip is confirmed</h1>
<p>Reservation <strong>N4K2QD</strong></p>
<table>
  <tr><th>Service</th><td>Train 43</td></tr>
  <tr><th>Date</th><td>{_datestamp(train_depart)}</td></tr>
  <tr><th>Departs</th><td>{_clock(train_depart)} from Pittsburgh (PGH)</td></tr>
</table>
<p>Connecting service: Route 28X to the airport is not part of this booking.</p>
</body></html>
"""
    p = os.path.join(out_dir, "reservation.html")
    with open(p, "w") as fh:
        fh.write(html)
    paths.append(p)

    return paths


def write_pdf(out_dir: str, start_epoch: int) -> str | None:
    """A one-page PDF built by hand, so the PDF adapter has real input.

    Written with raw PDF operators rather than a library: reportlab is not
    installed everywhere and a boarding pass is three lines of text. Returns
    None if the adapter has no text extractor available.
    """
    os.makedirs(out_dir, exist_ok=True)
    depart = start_epoch + 100 * 60
    lines = [
        "BOARDING PASS",
        "Flight AA 1729   PIT to BOS",
        f"Date {_datestamp(depart)}",
        f"Departs {_clock(depart + 95 * 60)}   Gate B31",
        "Confirmation JZ4Q1M",
    ]
    content = "BT /F1 12 Tf 60 740 Td 16 TL\n"
    for line in lines:
        safe = line.replace("(", r"\(").replace(")", r"\)")
        content += f"({safe}) Tj T*\n"
    content += "ET"
    stream = content.encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
        f"{xref_at}\n%%EOF\n"
    ).encode()

    path = os.path.join(out_dir, "boarding-pass.pdf")
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def write_all(out_dir: str, start_epoch: int | None = None) -> list[str]:
    start_epoch = start_epoch or int(time.time())
    paths = write_documents(out_dir, start_epoch)
    pdf = write_pdf(out_dir, start_epoch)
    if pdf:
        paths.append(pdf)
    return paths
