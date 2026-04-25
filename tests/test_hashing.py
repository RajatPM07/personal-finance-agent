from datetime import UTC, date, datetime

from skills.finance.lib.hashing import import_hash_pdf, import_hash_time_bearing

PV = "test_parser@1.0.0"

def test_time_bearing_hash_differs_when_time_differs():
    t1 = datetime(2026, 4, 21, 13, 5, tzinfo=UTC)
    t2 = datetime(2026, 4, 21, 21, 10, tzinfo=UTC)
    h1 = import_hash_time_bearing("acct-1", t1, 350.00, "swiggy", PV)
    h2 = import_hash_time_bearing("acct-1", t2, 350.00, "swiggy", PV)
    assert h1 != h2

def test_time_bearing_hash_stable_across_identical_inputs():
    t = datetime(2026, 4, 21, 13, 5, tzinfo=UTC)
    assert import_hash_time_bearing("acct-1", t, 350.00, "swiggy", PV) == \
           import_hash_time_bearing("acct-1", t, 350.00, "swiggy", PV)

def test_time_bearing_hash_rejects_naive_datetime():
    import pytest
    with pytest.raises(ValueError):
        import_hash_time_bearing("acct-1", datetime(2026, 4, 21, 13, 5), 350.00, "swiggy", PV)

def test_pdf_hash_disambiguates_by_row_ordinal():
    """Two ₹350 Swiggy orders in the same PDF on the same day — ordinal must save us."""
    base = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                normalized_description="swiggy", pdf_content_hash="pdf-abc", parser_version=PV)
    h_row7 = import_hash_pdf(source_row_ordinal=7, **base)
    h_row19 = import_hash_pdf(source_row_ordinal=19, **base)
    assert h_row7 != h_row19

def test_pdf_hash_stable_when_same_pdf_reparsed():
    kwargs = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                  normalized_description="swiggy", pdf_content_hash="pdf-abc",
                  source_row_ordinal=7, parser_version=PV)
    assert import_hash_pdf(**kwargs) == import_hash_pdf(**kwargs)

def test_pdf_hash_changes_on_parser_version_bump():
    base = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                normalized_description="swiggy", pdf_content_hash="pdf-abc",
                source_row_ordinal=7)
    h_v1 = import_hash_pdf(parser_version="bout@0.4.2", **base)
    h_v2 = import_hash_pdf(parser_version="bout@0.5.0", **base)
    assert h_v1 != h_v2  # parser bump must force fresh ingest

def test_pdf_hash_differs_across_different_pdfs():
    base = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                normalized_description="swiggy", source_row_ordinal=7, parser_version=PV)
    h_a = import_hash_pdf(pdf_content_hash="pdf-aaa", **base)
    h_b = import_hash_pdf(pdf_content_hash="pdf-bbb", **base)
    assert h_a != h_b  # documents rolling-overlap behavior; resolved by fuzzy pass + /dedup
