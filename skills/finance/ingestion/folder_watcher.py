"""Watches ~/finance-inbox/ for new PDFs and XLSX files; dispatches to the right parser.

Single-threaded: files are processed sequentially in arrival order. Spec §5.1
locks this for V1 (debugging simplicity).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from skills.finance.ingestion._common import (
    AmbiguousCredentialError,
    Bank,
    CredentialNotFoundError,
    ParseResult,
    SourceMeta,
    detect_bank_from_filename,
    password_lookup,
)
from skills.finance.ingestion.pipeline import ingest
from skills.finance.lib.settings import settings
from skills.finance.lib.users import AYUSHI_USER_ID, RAJAT_USER_ID
from skills.finance.monitoring.alerts import send_alert

logger = logging.getLogger(__name__)

# Account UUIDs from 003_seed.local.sql
ACCOUNT_IDS: dict[str, UUID] = {
    "icici_cc": UUID("10000000-0000-0000-0000-000000000003"),
    "amex_cc": UUID("10000000-0000-0000-0000-000000000005"),
    "paytm_upi": UUID("10000000-0000-0000-0000-000000000006"),
    "icici_savings": UUID("10000000-0000-0000-0000-000000000001"),
    "phonepe_upi": UUID("20000000-0000-0000-0000-000000000003"),
}

# Account owner (DB user_id) per bank. Default is Rajat; PhonePe is Ayushi's
# UPI source-of-truth account (she has no Paytm).
ACCOUNT_OWNERS: dict[str, str] = {
    "icici_cc": RAJAT_USER_ID,
    "amex_cc": RAJAT_USER_ID,
    "paytm_upi": RAJAT_USER_ID,
    "icici_savings": RAJAT_USER_ID,
    "phonepe_upi": AYUSHI_USER_ID,
}

EXPECTED_EXTENSION: dict[str, set[str]] = {
    "icici_cc": {".pdf"},
    "amex_cc": {".xlsx", ".xls"},
    "paytm_upi": {".xlsx", ".xls"},
    "icici_savings": {".pdf", ".xlsx", ".xls"},
    "phonepe_upi": {".pdf"},
}


async def dispatch_to_parser(
    file_path: Path,
    bank: Bank,
    password: str | None,
) -> None:
    """Call the right parser, then ingest. Send Telegram summary after ingest."""
    if bank == "icici_cc":
        from skills.finance.ingestion.parsers.icici_cc import parse as icici_parse
        parse_result = await asyncio.to_thread(icici_parse, file_path, password or "")
        source = SourceMeta(source="manual_pdf", source_ref=file_path.name)
    elif bank == "amex_cc":
        from skills.finance.ingestion.parsers.amex_cc import parse as amex_parse
        parse_result = await asyncio.to_thread(amex_parse, file_path)
        source = SourceMeta(source="manual_xlsx", source_ref=file_path.name)
    elif bank == "paytm_upi":
        from skills.finance.ingestion.parsers.paytm_upi import parse as paytm_parse
        parse_result = await asyncio.to_thread(paytm_parse, file_path)
        source = SourceMeta(source="manual_xlsx", source_ref=file_path.name)
    elif bank == "icici_savings":
        if file_path.suffix.lower() in (".xls", ".xlsx"):
            from skills.finance.ingestion.parsers.icici_savings_xls import parse as savings_xls_parse
            parse_result = await asyncio.to_thread(savings_xls_parse, file_path)
            source = SourceMeta(source="manual_xlsx", source_ref=file_path.name)
        else:
            from skills.finance.ingestion.parsers.icici_savings import parse as savings_parse
            savings_password = await asyncio.to_thread(password_lookup, "icici_savings", "1896")
            parse_result = await asyncio.to_thread(savings_parse, file_path, savings_password)
            source = SourceMeta(source="manual_pdf", source_ref=file_path.name)
    elif bank == "phonepe_upi":
        from skills.finance.ingestion.parsers.phonepe_upi import parse as phonepe_parse
        phonepe_password = await asyncio.to_thread(password_lookup, "phonepe_upi", "XXXX15")
        parse_result = await asyncio.to_thread(phonepe_parse, file_path, phonepe_password)
        source = SourceMeta(source="manual_pdf", source_ref=file_path.name)
    else:
        raise ValueError(f"Unknown bank: {bank}")

    account_id = ACCOUNT_IDS[bank]
    log_entry = await ingest(parse_result, account_id, source, user_id=ACCOUNT_OWNERS.get(bank, RAJAT_USER_ID))
    await _send_summary(bank, file_path.name, log_entry, parse_result)


async def _send_summary(
    bank: Bank,
    filename: str,
    log_entry: dict[str, Any],
    parse_result: ParseResult,
) -> None:
    """Send a per-statement summary message to the main bot.

    Plain text — no parse_mode. Filenames contain underscores which Telegram's
    Markdown parser treats as italic markers and rejects on imbalance.
    """
    from skills.finance.bot.main import bot as main_bot
    status = log_entry["status"]
    if status == "success":
        derived = parse_result.declared_totals.get("_derived_from_rows", False)
        annotation = ""
        if derived:
            annotation = (
                "\nNote: declared totals derived from row sums; validator "
                "effectively skipped (no Total row in source)."
            )
        text = (
            f"📥 {bank.upper().replace('_', ' ')} {filename} ingested\n"
            f"{log_entry['rows_added']} rows, "
            f"₹{log_entry['extracted_total']} (declared ₹{log_entry['declared_total']}) — totals match ✓"
            f"{annotation}"
        )
        await main_bot.send_message(
            chat_id=settings.telegram_chat_id_rajat, text=text,
        )
    elif status == "skipped_duplicate":
        await main_bot.send_message(
            chat_id=settings.telegram_chat_id_rajat,
            text=f"📥 {bank.upper().replace('_', ' ')} {filename}: already ingested previously (skipped).",
        )
    # 'total_check_failed' alerts go via send_alert from inside pipeline; no double-message.


async def handle_new_file(file_path: Path) -> None:
    """Top-level handler for any new file in the inbox.

    Routes by filename token-match + extension match. On unknown / ambiguous /
    extension-mismatch: rename to <name>.rejected so re-scans don't re-trigger;
    send alert.
    """
    name = file_path.name
    if name.endswith(".rejected"):
        return

    ext = file_path.suffix.lower()
    if ext not in (".pdf", ".xlsx", ".xls"):
        return

    bank = detect_bank_from_filename(name)
    if bank is None:
        n = name.lower()
        is_icici = "icici" in n
        is_amex = ("amex" in n) or ("american" in n)
        if is_icici and is_amex:
            msg = f"Ambiguous filename '{name}' — matches both ICICI and AMEX. Rejected."
        else:
            msg = (
                f"Filename '{name}' doesn't match any known bank pattern. "
                f"Rename to include 'icici_cc_' or 'amex_cc_' and re-drop. Rejected."
            )
        rejected = file_path.parent / f"{name}.rejected"
        await asyncio.to_thread(file_path.rename, rejected)
        await send_alert(msg)
        return

    expected_exts = EXPECTED_EXTENSION[bank]
    if ext not in expected_exts:
        msg = (
            f"Bank '{bank}' expects {expected_exts} files; got '{ext}' for '{name}'. Rejected."
        )
        rejected = file_path.parent / f"{name}.rejected"
        await asyncio.to_thread(file_path.rename, rejected)
        await send_alert(msg)
        return

    password: str | None = None
    if bank == "icici_cc":
        try:
            password = await asyncio.to_thread(password_lookup, bank)
        except (AmbiguousCredentialError, CredentialNotFoundError, FileNotFoundError) as e:
            # FileNotFoundError covers the case where credentials.yaml itself
            # is absent — surface as a normal alert rather than a silent
            # coroutine crash.
            logger.exception("password lookup failed for %s", bank)
            rejected = file_path.parent / f"{name}.rejected"
            await asyncio.to_thread(file_path.rename, rejected)
            await send_alert(f"Password lookup for {bank} failed: {e}. {name} rejected.")
            return

    try:
        await dispatch_to_parser(file_path, bank=bank, password=password)
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch failed for %s", name)
        await send_alert(f"Ingestion failed for {name}: {e}")


class _FileEventHandler(FileSystemEventHandler):
    """Bridges sync watchdog callbacks to the async event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self._lock = asyncio.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_handle(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_handle(event, path_attr="dest_path")

    def _maybe_handle(self, event: FileSystemEvent, path_attr: str = "src_path") -> None:
        if event.is_directory:
            return
        path = Path(getattr(event, path_attr))
        if path.suffix.lower() not in (".pdf", ".xlsx", ".xls"):
            return
        asyncio.run_coroutine_threadsafe(self._serialized_handle(path), self.loop)

    async def _serialized_handle(self, path: Path) -> None:
        async with self._lock:
            await handle_new_file(path)


async def run() -> None:
    """Long-running task — start in app.py alongside aiogram + APScheduler."""
    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    handler = _FileEventHandler(loop)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()
    logger.info("folder_watcher started on %s", inbox)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("folder_watcher stopping")
    finally:
        observer.stop()
        observer.join(timeout=5)
        logger.info("folder_watcher stopped cleanly")
