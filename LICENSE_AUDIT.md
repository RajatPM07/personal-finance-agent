# License Audit

## Direct dependencies (MIT / Apache-2 / Unlicense — safe)
- aiogram: MIT
- litellm: MIT
- bout: MIT
- casparser: MIT (default PDFMiner backend only — do NOT use [fast] extra, it pulls AGPL PyMuPDF)
- mftool: MIT
- pykiteconnect: MIT
- pikepdf: MPL-2.0
- pdfplumber: MIT
- camelot-py: MIT
- rapidfuzz: MIT
- greggles/mcc-codes: Unlicense
- xaneem/hdfc-credit-card-statement-parser: MIT (forked)
- saurabhgupta050890/transaction-sms-parser: MIT (ported TS→Py)
- bankstatementparser dedup pattern: Apache-2

## Reference only (AGPL/GPL — DO NOT import code)
- sarim2000/pennywiseai-tracker: AGPL-3 (SMS patterns — 44 banks)
- ananthakumaran/paisa: AGPL-3
- ritesh-kanwar/Cashiro: GPL-3 (UPI parsing patterns)
- maybe-finance/maybe: AGPL-3, archived
- firefly-iii/firefly-iii: AGPL-3 (dedup pattern replicated, not imported)
- bahuma20/firefly-iii-ai-categorize: AGPL-3
- HarrisonTotty/tcat: GPL-3 (YAML-regex taxonomy pattern)
- python-telegram-bot: LGPL-3 / GPL-3
