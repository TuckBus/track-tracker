"""Importing this package registers every adapter."""

from .base import (  # noqa: F401
    DOCUMENTS,
    TELEMETRY,
    DocumentSource,
    TelemetrySource,
    document_source_for,
    load_documents,
    register_document,
    register_telemetry,
)
from . import documents, telemetry  # noqa: F401,E402

__all__ = [
    "TELEMETRY",
    "DOCUMENTS",
    "TelemetrySource",
    "DocumentSource",
    "register_telemetry",
    "register_document",
    "document_source_for",
    "load_documents",
]
