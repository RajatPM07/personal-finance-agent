"""Gmail bank statement downloader.

Runs daily via launchd (com.rajat.pfa.gmail.plist). Fetches PDF statements
from known bank senders, saves them to ~/finance-inbox/ where folder_watcher
auto-ingests them into the pipeline.

First-run setup (one-time, ~10 minutes):
  1. Go to https://console.cloud.google.com/
  2. Create a project → enable Gmail API → create OAuth2 credentials
     (Application type: Desktop app) → download JSON
  3. Save it as:  credentials/gmail_oauth.json  (must be "Desktop app" type, not "Web app")
  4. Run ONCE manually to grant consent:
       .venv/bin/python scripts/gmail_statement_fetch.py
     A browser window opens. Sign in as sharma.rajat70@gmail.com and allow.
     Token saved to credentials/gmail_token.json — subsequent runs are headless.

Idempotency: processed emails are labelled "PFA-Processed" in Gmail so
re-runs skip them. The label is auto-created on first run.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Bootstrap ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

INBOX = Path(os.environ.get("FINANCE_INBOX_PATH", str(Path.home() / "finance-inbox")))
PENDING = INBOX / "pending"
LOG_DIR = Path(os.environ.get("FINANCE_LOG_PATH", str(Path.home() / "finance-logs")))

CREDS_FILE = PROJECT_ROOT / "credentials" / "gmail_oauth.json"
TOKEN_FILE = PROJECT_ROOT / "credentials" / "gmail_token.json"
PROCESSED_LABEL_NAME = "PFA-Processed"

# Gmail API scope: read messages + add labels. No send, no delete.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ── Logging ────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "gmail_fetch.log"),
    ],
)
logger = logging.getLogger(__name__)


# ── Bank search rules ──────────────────────────────────────────────────────
# 'filename_prefix' must match detect_bank_from_filename() in _common.py:
#   "icici_cc"       → icici + cc tokens
#   "icici_savings"  → icici + savings tokens
#   "hdfc_cc"        → unknown to pipeline → goes to PENDING subfolder
#
# Adjust query strings if your statement emails differ. Test by searching
# exactly these queries in Gmail yourself first.
SEARCH_RULES = [
    {
        "label": "ICICI CC",
        "query": (
            'from:alerts@icicibank.com has:attachment filename:pdf '
            '"credit card" -label:PFA-Processed'
        ),
        "filename_prefix": "icici_cc",
        "pending": False,
    },
    {
        "label": "ICICI Savings",
        "query": (
            'from:alerts@icicibank.com has:attachment filename:pdf '
            '"savings account" -label:PFA-Processed'
        ),
        "filename_prefix": "icici_savings",
        "pending": False,
    },
    {
        "label": "HDFC CC",
        "query": (
            'from:alerts@hdfcbank.net has:attachment filename:pdf '
            'subject:statement -label:PFA-Processed'
        ),
        "filename_prefix": "hdfc_cc",
        "pending": True,  # no parser yet — lands in INBOX/pending/ with Telegram alert
    },
]


# ── Auth ───────────────────────────────────────────────────────────────────
def _get_credentials() -> Credentials:
    """Load stored token, refreshing if expired. On first run: browser OAuth flow."""
    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing Gmail OAuth token")
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                logger.error(
                    "credentials/gmail_oauth.json not found. "
                    "See script docstring for setup instructions."
                )
                sys.exit(1)
            # Validate credential type — InstalledAppFlow requires "installed", not "web"
            raw = json.loads(CREDS_FILE.read_text())
            if "web" in raw and "installed" not in raw:
                logger.error(
                    "credentials/gmail_oauth.json is a 'Web app' credential. "
                    "InstalledAppFlow requires a 'Desktop app' credential. "
                    "Go to Google Cloud Console → APIs & Services → Credentials → "
                    "Create Credentials → OAuth client ID → Desktop app."
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
            logger.info("OAuth consent granted. Token saved.")

        TOKEN_FILE.write_text(creds.to_json())

    return creds


# ── Gmail helpers ──────────────────────────────────────────────────────────
def _get_or_create_label(service, name: str) -> str:
    """Return the label ID for `name`, creating it if it doesn't exist."""
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl["name"] == name:
            return lbl["id"]

    new_label = (
        service.users()
        .labels()
        .create(userId="me", body={"name": name, "labelListVisibility": "labelHide"})
        .execute()
    )
    logger.info("Created Gmail label: %s", name)
    return new_label["id"]


def _search_messages(service, query: str) -> list[dict]:
    """Return all message stubs matching `query` (handles pagination)."""
    msgs: list[dict] = []
    page_token: str | None = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        msgs.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return msgs


def _pdf_attachments(service, msg_id: str) -> list[tuple[str, str, str]]:
    """Return [(filename, attachment_id, mime_type)] for all PDF parts in a message."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    parts = msg.get("payload", {}).get("parts", [])
    pdfs: list[tuple[str, str, str]] = []

    def _walk(parts_list: list[dict]) -> None:
        for part in parts_list:
            sub = part.get("parts")
            if sub:
                _walk(sub)
            mime = part.get("mimeType", "")
            att_id = part.get("body", {}).get("attachmentId")
            name = part.get("filename", "")
            if att_id and (mime == "application/pdf" or name.lower().endswith(".pdf")):
                pdfs.append((name, att_id, mime))

    _walk(parts)
    return pdfs


def _download_attachment(service, msg_id: str, att_id: str) -> bytes:
    """Fetch attachment bytes from Gmail."""
    data = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=msg_id, id=att_id)
        .execute()
    )
    return base64.urlsafe_b64decode(data["data"])


def _mark_processed(service, msg_id: str, label_id: str) -> None:
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ── Telegram alert (direct requests call, no aiogram) ──────────────────────
def _telegram_alert(text: str) -> None:
    try:
        import requests

        token = os.environ.get("TELEGRAM_ALERT_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
        if not token or not chat_id:
            logger.warning("Telegram alert env vars not set; skipping alert")
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Telegram alert failed: %s", e)


# ── Core download loop ─────────────────────────────────────────────────────
def _run(service) -> tuple[int, int]:
    """Process all rules. Returns (downloaded, skipped_pending)."""
    INBOX.mkdir(parents=True, exist_ok=True)
    PENDING.mkdir(parents=True, exist_ok=True)

    processed_label_id = _get_or_create_label(service, PROCESSED_LABEL_NAME)
    today = date.today().isoformat()
    downloaded = 0
    pending_count = 0

    for rule in SEARCH_RULES:
        logger.info("Searching: %s — query: %s", rule["label"], rule["query"])
        try:
            messages = _search_messages(service, rule["query"])
        except HttpError as e:
            logger.error("Gmail search failed for %s: %s", rule["label"], e)
            continue

        if not messages:
            logger.info("  No new messages.")
            continue

        for stub in messages:
            msg_id = stub["id"]
            try:
                pdfs = _pdf_attachments(service, msg_id)
                if not pdfs:
                    logger.info("  msg %s: no PDF attachments found", msg_id)
                    _mark_processed(service, msg_id, processed_label_id)
                    continue

                for orig_name, att_id, _mime in pdfs:
                    safe_orig = orig_name.replace("/", "_").replace(" ", "_")
                    dest_name = f"{rule['filename_prefix']}_{today}_{msg_id[:8]}_{safe_orig}"
                    dest_dir = PENDING if rule["pending"] else INBOX
                    dest_path = dest_dir / dest_name

                    if dest_path.exists():
                        logger.info("  %s already saved; skipping", dest_name)
                        continue

                    pdf_bytes = _download_attachment(service, msg_id, att_id)
                    dest_path.write_bytes(pdf_bytes)
                    logger.info("  Saved %s (%d bytes)", dest_path, len(pdf_bytes))

                    if rule["pending"]:
                        pending_count += 1
                        _telegram_alert(
                            f"📥 Gmail: {rule['label']} statement downloaded to pending/\n"
                            f"File: {dest_name}\n"
                            f"Parser not yet built — ingest manually."
                        )
                    else:
                        downloaded += 1

                _mark_processed(service, msg_id, processed_label_id)

            except HttpError as e:
                logger.error("  Error processing msg %s: %s", msg_id, e)

    return downloaded, pending_count


def main() -> int:
    try:
        creds = _get_credentials()
        service = build("gmail", "v1", credentials=creds)
    except Exception as e:
        logger.error("Gmail auth failed: %s", e)
        _telegram_alert(f"⚠️ Gmail statement fetch: auth failed — {e}")
        return 1

    downloaded, pending_count = _run(service)

    summary = (
        f"📧 Gmail statement fetch complete: "
        f"{downloaded} ingested, {pending_count} pending (HDFC)"
    )
    logger.info(summary)
    if downloaded > 0 or pending_count > 0:
        _telegram_alert(summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
