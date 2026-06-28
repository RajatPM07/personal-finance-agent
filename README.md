# personal-finance-agent

A Telegram-native AI agent that answers natural-language questions over your
own bank and credit card data. Built because spreadsheet-based expense tracking
creates friction nobody actually sustains, and because the data you need to make
smart financial decisions already exists in your inbox.

---

## The Problem

Most personal finance tools require either manual entry or a third-party data
broker sitting between you and your bank. Neither is sustainable. The data that
matters (statements, transaction records, confirmations) arrives in your inbox
automatically. The gap is a layer that reads it, structures it, and lets you
query it in plain language without switching apps.

## What It Does

- **Ingests via Gmail API and Telegram:** Bank and credit card statements arrive
  by email. The agent fetches, parses, and stores them. Queries come in through
  a Telegram bot, so the interface is frictionless.
- **Answers natural-language queries:** Ask questions like "how much did I spend
  on dining last month" or "what is my largest recurring expense this quarter."
  The agent returns structured answers, not raw data dumps.
- **Stores structured transaction data in Supabase:** Every parsed transaction is
  written to a Supabase table with category, amount, date, and merchant fields,
  making it queryable by the agent and auditable by the user.
- **Runs 24/7 as a personal deployment:** Single-user, always-on. No dashboard
  to log into. The interface lives where you already are.
- **Handles multi-source data in one view:** Consolidates statements from
  multiple accounts into a single queryable dataset.

## Architecture

```
Gmail Inbox (bank and card statements)
        |
        v
  Gmail API Fetch
        |
        v
  Statement Parser (Python)
        |
        v
  Supabase (transactions table)
        ^
        |
  Claude / LLM query layer
        ^
        |
  Telegram Bot (user query)
        |
        v
  Natural-language answer --> Telegram reply
```

## Tech Stack

| Layer | Tool |
|---|---|
| Ingestion | Gmail API |
| Interface | Telegram Bot API |
| Storage | Supabase (Postgres) |
| LLM | Claude (Anthropic) |
| Orchestration | Python |
| Deployment | Always-on personal server |

## What I Learned (PM Lens)

- **Telegram over a web app was the right call.** A dashboard would have taken
  3x longer to build and created a login step nobody wants for a single-user
  tool. Choosing the interface with zero incremental friction was a product
  decision, not just a technical one.
- **Gmail API ingestion beats manual uploads.** The moment any step requires
  the user to remember to do something, the tool stops being used. Automatic
  ingestion from email removes the only recurring friction point.
- **Structured storage unlocks everything downstream.** Storing raw statement
  text would have been faster to build initially but would have made every query
  expensive and slow. Paying the parsing cost upfront at ingestion time made the
  query layer fast and cheap. Classic build-vs-runtime tradeoff.

## Status

Single-user, 24/7 personal deployment. Not production-hardened for multi-user
scale, by design.

## Setup

1. Clone the repo.
2. Copy `.env.example` to `.env` and fill in your credentials:

```
GMAIL_CLIENT_ID=your-gmail-client-id
GMAIL_CLIENT_SECRET=your-gmail-client-secret
SUPABASE_URL=your-supabase-url
SUPABASE_KEY=your-supabase-anon-key
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
ANTHROPIC_API_KEY=your-anthropic-api-key
```

3. Run the ingestion script to fetch and parse existing statements.
4. Start the Telegram bot listener.

Full setup instructions in `SETUP.md`.
