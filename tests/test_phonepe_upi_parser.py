import os
from decimal import Decimal
from pathlib import Path

import pytest

from skills.finance.ingestion.parsers.phonepe_upi import __parser_version__, parse

FIXTURE = Path(__file__).parent / "golden_fixtures" / "phonepe_sample.pdf"
PASSWORD = os.environ.get("PHONEPE_PDF_PASSWORD", "")

pytestmark = pytest.mark.skipif(not FIXTURE.exists() or not PASSWORD, reason="PhonePe fixture/password not present")


def test_version():
    assert __parser_version__ == "phonepe-upi/v1"


def test_parses_rows():
    r = parse(FIXTURE, PASSWORD)
    assert len(r.rows) > 300              # ~353 detail lines in the sample
    assert len(r.pdf_content_hash) == 64


def test_directions_and_amounts_wellformed():
    r = parse(FIXTURE, PASSWORD)
    for row in r.rows:
        assert row.amount > Decimal("0")   # no blank/zero amounts survive
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()


def test_ordinals_contiguous():
    r = parse(FIXTURE, PASSWORD)
    assert [x.source_row_ordinal for x in r.rows] == list(range(1, len(r.rows) + 1))


def test_large_overflow_amount_captured():
    # The Jan 02 ₹70,000 transfer has its amount on the Transaction-ID line.
    r = parse(FIXTURE, PASSWORD)
    assert any(row.amount == Decimal("70000.00") for row in r.rows)


def test_credit_row_direction_in():
    r = parse(FIXTURE, PASSWORD)
    assert any(row.direction == "in" for row in r.rows)   # "Received from …" rows
