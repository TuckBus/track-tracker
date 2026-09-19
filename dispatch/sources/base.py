"""Source adapters.

The rule: nothing downstream of this package knows what format anything
arrived in. Detectors see `Observation`. Extraction sees `Document`. Adding a
source means writing one class with one method and decorating it -- no changes
to detection, triage, matching, storage, or the dashboard.

    @register_document("jsonld")
    class JsonLdSource(DocumentSource):
        extensions = (".jsonld",)
        def parse(self, blob, source_id): ...

That is the whole contract. `python -m dispatch.cli sources` prints whatever
is registered, which is how we show it rather than assert it.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Protocol, runtime_checkable

from ..schema import Document, Observation, OfficialAlert


@runtime_checkable
class TelemetrySource(Protocol):
    """Turns bytes from a live feed into normalised Observations."""

    name: str
    media: str

    def parse(
        self, blob: bytes, retrieved_at: int
    ) -> tuple[list[Observation], list[OfficialAlert]]: ...


@runtime_checkable
class DocumentSource(Protocol):
    """Turns a file into one searchable Document with landmark offsets."""

    name: str
    extensions: tuple[str, ...]

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document: ...


TELEMETRY: dict[str, TelemetrySource] = {}
DOCUMENTS: dict[str, DocumentSource] = {}


def register_telemetry(name: str) -> Callable:
    def deco(cls):
        cls.name = name
        TELEMETRY[name] = cls()
        return cls

    return deco


def register_document(name: str) -> Callable:
    def deco(cls):
        cls.name = name
        DOCUMENTS[name] = cls()
        return cls

    return deco


def document_source_for(path: str) -> DocumentSource | None:
    """Dispatch on extension, falling back to the plain-text reader.

    A source we do not recognise still gets read as text rather than
    rejected -- degrading is better than refusing when someone drops an
    unexpected confirmation file on us at 3am.
    """
    lowered = path.lower()
    for src in DOCUMENTS.values():
        if any(lowered.endswith(ext) for ext in src.extensions):
            return src
    return DOCUMENTS.get("text")


def load_documents(paths: Iterable[str]) -> list[Document]:
    import os

    out = []
    for p in paths:
        src = document_source_for(p)
        if src is None:
            continue
        with open(p, "rb") as fh:
            blob = fh.read()
        out.append(src.parse(blob, os.path.basename(p), int(time.time())))
    return out
