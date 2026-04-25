"""Pure function: compare a ParseResult's row totals against its declared totals.

No I/O. Easy to unit-test. Used by pipeline.py before any DB write.

Caveat: AMEX XLSX exports may not include a declared totals row (see spec §6.3
+ §9.2). In that case the AMEX parser sets declared_totals from row sums, and
this validator passes tautologically. The annotation on the success message
makes the weakness visible to the user."""
from __future__ import annotations

from decimal import Decimal

from skills.finance.ingestion._common import ParseResult, ValidationResult

TOLERANCE: Decimal = Decimal("1.00")


def validate(pr: ParseResult) -> ValidationResult:
    declared_in = pr.declared_totals["total_credits"]
    declared_out = pr.declared_totals["total_spends"]

    extracted_in = sum(
        (r.amount for r in pr.rows if r.direction == "in"), Decimal("0")
    )
    extracted_out = sum(
        (r.amount for r in pr.rows if r.direction == "out"), Decimal("0")
    )

    delta_in = abs(declared_in - extracted_in)
    delta_out = abs(declared_out - extracted_out)

    return ValidationResult(
        ok=(delta_in <= TOLERANCE and delta_out <= TOLERANCE),
        delta_in=delta_in,
        delta_out=delta_out,
        rows_count=len(pr.rows),
        declared_in=declared_in,
        declared_out=declared_out,
        extracted_in=extracted_in,
        extracted_out=extracted_out,
    )
