from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def tmp_inbox(tmp_path):
    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    return inbox


def test_dispatch_unknown_filename_renames_to_rejected(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "random.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert", new_callable=AsyncMock) as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "random.pdf.rejected").exists()
    assert not f.exists()
    mock_dispatch.assert_not_called()
    mock_alert.assert_called_once()


def test_dispatch_icici_pdf_calls_parser(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "icici_cc_2026_05.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.password_lookup",
               return_value="testpass"), \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch:
        asyncio.run(handle_new_file(f))
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["bank"] == "icici_cc"
    assert mock_dispatch.call_args.kwargs["password"] == "testpass"


def test_dispatch_amex_xlsx_calls_parser_no_password(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "amex_cc_2026_05.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    with patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch:
        asyncio.run(handle_new_file(f))
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["bank"] == "amex_cc"
    assert mock_dispatch.call_args.kwargs["password"] is None


def test_amex_pdf_extension_mismatch_rejected(tmp_inbox):
    """AMEX should be XLSX in V1; an AMEX PDF is a category mismatch."""
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "amex_cc_statement.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert", new_callable=AsyncMock) as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "amex_cc_statement.pdf.rejected").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called
    assert "expects" in str(mock_alert.call_args).lower()


def test_icici_xlsx_extension_mismatch_rejected(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "icici_cc_statement.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert", new_callable=AsyncMock) as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "icici_cc_statement.xlsx.rejected").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called


def test_dispatch_ambiguous_filename_alerts_and_rejects(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "icici_amex_partnership.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert", new_callable=AsyncMock) as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "icici_amex_partnership.pdf.rejected").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called
    assert "ambiguous" in str(mock_alert.call_args).lower()


def test_already_rejected_files_are_ignored(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "random.pdf.rejected"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser", new_callable=AsyncMock) as mock_dispatch, \
         patch("skills.finance.ingestion.folder_watcher.send_alert", new_callable=AsyncMock) as mock_alert:
        asyncio.run(handle_new_file(f))
    mock_dispatch.assert_not_called()
    mock_alert.assert_not_called()
    assert f.exists()
