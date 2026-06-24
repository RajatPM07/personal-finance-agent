# PFA Financial Summary Dashboard — Design Spec

**Date:** 2026-06-24  
**Status:** Approved  
**Scope:** Personal-use web dashboard showing financial summary Jan 2026 → latest data

---

## Goal

A bookmarkable, always-available web page that gives Rajat a one-glance financial summary across all accounts. No auth, no interactivity, no sharing — personal review tool only.

---

## Architecture

- **App:** Standalone Next.js 14 (App Router) at `/Users/rajat/AntiGravity/pfa-dashboard`
- **Hosting:** Vercel (free tier)
- **Data:** Supabase Postgres, queried server-side via `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`
- **Rendering:** Server Components only — all DB queries run at request time, no client-side data fetching
- **Caching:** None — fresh data on every load
- **Stack:** Next.js 14, Tailwind CSS, shadcn/ui, Recharts

The dashboard app is entirely separate from the PFA repo. It reads from the same Supabase instance but never writes.

---

## Data Constraints

All queries must:
- Filter `is_deleted = false`
- Filter `date >= '2026-01-01'`
- Use `direction = 'out'` for spend, `direction = 'in'` for income
- Use `amount` (always positive unsigned)
- Use `raw_merchant` for merchant display (NOT `normalized_merchant` — NULL until W5 normalization)
- Join `categories` on `category_id` for category names
- Join `accounts` on `account_id` for account nicknames
- Never filter on `user_id` (single-user V1, UUID not available)

---

## Dashboard Sections

### 1. Header
`Financial Summary · Jan – [latest month] 2026`  
Subtitle: last updated timestamp.

### 2. KPI Strip (4 cards)
| Card | Query |
|------|-------|
| Total Spend | SUM(amount) WHERE direction='out' AND category != 'Self Transfer' |
| Total Income | SUM(amount) WHERE direction='in' |
| Net | Income − Spend |
| Transactions | COUNT(*) all active rows |

### 3. Monthly Spend Bar Chart
- X-axis: Jan → latest month (date_trunc month)
- Y-axis: SUM(amount) WHERE direction='out' AND category NOT IN ('Self Transfer', 'Wallet Load')
- Recharts `BarChart`

### 4. Spend by Category (Horizontal Bar)
- Top 10 categories by spend
- Exclude: Self Transfer, Wallet Load
- direction='out' only
- Recharts `BarChart` horizontal

### 5. Spend by Account (Donut)
- Per `accounts.nickname`
- direction='out', exclude Self Transfer category
- Recharts `PieChart`

### 6. Top 10 Merchants Table
- Columns: Merchant (`raw_merchant`), Category, Total Spend
- direction='out', exclude Self Transfer
- Sorted by total spend DESC

### 7. Needs Review Flag
- Count of transactions WHERE `categories.name = 'Needs Review'`
- Shown as a warning card if count > 0

---

## File Structure

```
pfa-dashboard/
├── app/
│   ├── page.tsx          # Single page, Server Component
│   └── layout.tsx        # Root layout, Tailwind
├── lib/
│   ├── supabase.ts       # Server-side Supabase client
│   └── data.ts           # All query functions
├── components/
│   ├── KpiCard.tsx
│   ├── MonthlySpendChart.tsx
│   ├── CategoryChart.tsx
│   ├── AccountDonut.tsx
│   ├── MerchantsTable.tsx
│   └── NeedsReviewBadge.tsx
├── .env.local            # SUPABASE_URL, SUPABASE_SERVICE_KEY (gitignored)
└── ...
```

---

## Environment Variables

| Var | Source |
|-----|--------|
| `SUPABASE_URL` | PFA `.env` → `SUPABASE_URL` |
| `SUPABASE_SERVICE_KEY` | PFA `.env` → `SUPABASE_SERVICE_KEY` |

Added to Vercel via `vercel env add` before production deploy.

---

## Non-Goals

- No auth / password gate
- No write operations
- No real-time updates (refresh = reload)
- No drill-down / transaction list
- No Ayushi's accounts (not in DB yet)
