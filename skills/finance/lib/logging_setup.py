from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from skills.finance.lib.settings import settings

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    log_dir = Path(settings.finance_log_path)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    # Clear any pre-configured handlers (e.g. from libraries)
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        log_dir / "pfa.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(stdout_handler)
