"""Week 1 smoke: prove we can decrypt a password-protected ICICI PDF and extract any text.

Row-level parsing is Week 2 (skills/finance/ingestion/parsers/icici_parser.py).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pdfplumber
import pikepdf
import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "icici_sample.pdf"
# Set ICICI_PDF_PASSWORD env var to run this test against the real fixture.
# When unset, the test skips so `make test` stays green in CI/automated runs.
PASSWORD = os.environ.get("ICICI_PDF_PASSWORD", "")


@pytest.mark.skipif(not FIXTURE.exists(), reason="real ICICI fixture not present at tests/golden_fixtures/icici_sample.pdf")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set; run with `ICICI_PDF_PASSWORD=... pytest tests/test_pdf_smoke.py` to actually exercise the smoke")
def test_pdf_decrypt_and_extract() -> None:
    # Use NamedTemporaryFile with auto-delete so decrypted PII never persists.
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        try:
            with pikepdf.open(FIXTURE, password=PASSWORD) as src:
                src.save(tmp.name)
        except pikepdf.PasswordError:
            pytest.fail("ICICI_PDF_PASSWORD env var is set but wrong — pikepdf rejected the password.")
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"pikepdf could not open the PDF: {e}")

        with pdfplumber.open(tmp.name) as pl_pdf:
            pages = pl_pdf.pages
            assert len(pages) > 0, "PDF has no pages"
            text = pages[0].extract_text() or ""
            assert text.strip(), "Could not extract text from the first page"
            assert any(s in text for s in ("Date", "Amount", "ICICI", "INR")), (
                "First-page text doesn't contain expected ICICI markers — extractor "
                "may be returning garbage. Investigate before treating as smoke pass."
            )
