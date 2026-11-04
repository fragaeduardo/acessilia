"""Authoritative page limits and structural-extraction failures.

The existing preflight rule only trusts a PDF count verified by the
Toolbox's PyMuPDF parser; all formats are checked after extraction.
"""

from __future__ import annotations

from typing import Any

from backend.core.manifest.models import ProcessingManifest
from backend.i18n import t
from backend.stage_messages import STAGE_DOCUMENT_TOO_MANY_PAGES


class StructuralExtractionError(RuntimeError):
    """The Toolbox could not extract the document structure (job-level failure)."""


class DocumentTooLargeError(ValueError):
    """The document has more pages than ``MAX_PAGES`` (job-level failure)."""


def _check_page_count(page_count: int) -> None:
    """Reject an oversized document as a whole with a user-facing message.

    A verified PDF count may be checked before costly extraction. The
    authoritative manifest count is checked afterwards for every format,
    including those without a reliable preflight count. Neither check allows
    partial processing beyond MAX_PAGES.

    Raises:
        DocumentTooLargeError: When the page count exceeds MAX_PAGES.
    """
    from backend.config.settings import settings

    if page_count > settings.max_pages:
        raise DocumentTooLargeError(
            t(STAGE_DOCUMENT_TOO_MANY_PAGES).format(
                page_count=page_count, max_pages=settings.max_pages
            )
        )


def _check_page_limit_before_extraction(inspection: dict[str, Any] | None) -> None:
    """Reject only PDF counts verified by the existing remote PDF parser.

    Scanned content streams, unknown counts and non-PDF formats are not a
    reliable early limit. They retain the authoritative post-extraction check.
    No local operational parser is introduced (ADR-0003).
    """
    if not isinstance(inspection, dict):
        return
    count = inspection.get("page_count")
    if (
        inspection.get("is_pdf") is True
        and inspection.get("page_count_source") == "pymupdf"
        and type(count) is int
        and count > 0
    ):
        _check_page_count(count)


def _check_page_limit(manifest: ProcessingManifest) -> None:
    """Authoritative limit after extraction, including formats without preflight."""
    _check_page_count(len(manifest.pages))
