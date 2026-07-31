# InvoiceShelf Expense Exporter — Design

**Date:** 2026-08-01
**Status:** Approved
**Target:** InvoiceShelf 2.4.1 (self-hosted)

## Goal

A command-line tool that, given a client (customer) name and a date range, exports
all matching expenses from a hosted InvoiceShelf instance to a CSV and bundles the
attached receipts into a zip.

## Usage

```
python export_expenses.py --client "Acme Corp" --start 01042025 --end 30062025 [--out ./exports] [--customer-id 12] [--dry-run]
```

- `--client` — customer name to resolve to a `customer_id` (case-insensitive search).
- `--start` / `--end` — inclusive range in `DDMMYYYY` format.
- `--out` — output directory (default `./exports`).
- `--customer-id` — skip the name lookup and use this id directly.
- `--dry-run` — list what would be exported; do not download receipts or write the zip.

## Approach

Remote API client. The script talks to the InvoiceShelf REST API over HTTPS. It does
not require shell access to the server and touches nothing on the host.

- **Language/runtime:** Python 3, **standard library only** (`urllib`, `json`, `csv`,
  `zipfile`, `argparse`). No `pip install` required — runs on any machine with Python 3.
- **Single file:** `export_expenses.py`, plus a `.env`-style config file for credentials.

## Configuration

A config file (e.g. `config.env` / `.env`) next to the script, read at startup:

```
INVOICESHELF_URL=https://invoices.example.com
INVOICESHELF_EMAIL=you@example.com
INVOICESHELF_PASSWORD=********
INVOICESHELF_COMPANY_ID=1
```

`INVOICESHELF_COMPANY_ID` defaults to `1` (typical for a single-company self-hosted
install). Values may also be supplied via real environment variables, which take
precedence over the file.

## API contract (confirmed against 2.4.1 source)

1. **Login** — `POST /api/v1/auth/login` with JSON `{email, password}` → returns a
   Sanctum bearer token.
2. **Auth headers** — every subsequent request sends
   `Authorization: Bearer <token>` and `company: <company_id>`.
3. **Resolve client** — `GET /api/v1/customers?search=<name>&limit=all` →
   match by name to obtain `customer_id`.
4. **List expenses** — `GET /api/v1/expenses?customer_id=<id>&from_date=<YYYY-MM-DD>&to_date=<YYYY-MM-DD>&limit=all`.
   The `applyFilters` scope applies `from_date`+`to_date` (both required together) and
   `customer_id`. Range is inclusive on both ends.
5. **Download receipt** — `GET /api/v1/expenses/<id>/show/receipt` → raw file bytes
   (one receipt per expense; endpoint returns the first media in the `receipts`
   collection). Receipt filename/mime come from the expense's `attachment_receipt_meta`.

### Money units

Amounts (`amount`, `base_amount`) are stored in minor units (cents). Human amount =
`amount / 10 ** currency.precision`, where `precision` comes from the expense's nested
`currency` object (default `2` if absent).

## Flow

1. Load config; validate required values are present.
2. `POST` login → bearer token.
3. Resolve client:
   - If `--customer-id` given, use it.
   - Else `GET /customers?search=<name>`.
     - **0 matches** → error, print closest names found, exit non-zero.
     - **>1 match** → error, print `id — name` list, ask user to rerun with `--customer-id`, exit non-zero.
     - **1 match** → use its id.
4. Parse `--start` / `--end` from `DDMMYYYY`; validate real dates and `start <= end`;
   convert to `Y-m-d`.
5. `GET` expenses with `limit=all`.
6. Ensure `--out` exists. For each expense with a receipt, download bytes and add to
   the zip, building a map of `expense_id → stored filename` (skipped under
   `--dry-run`). A download that fails is recorded with no stored filename.
7. Write the CSV using that map, so `receipt_file` reflects what actually landed in the
   zip (blank for no-receipt or failed-download expenses).
8. Print a summary: expense count, total, receipts downloaded, any receipt failures.

## Output

Written into `--out` (default `./exports`):

- `<Client>_<start>-<end>.csv`
- `<Client>_<start>-<end>_receipts.zip`

Where `<start>` / `<end>` are the `DDMMYYYY` inputs and `<Client>` is the sanitized
client name.

### CSV columns

```
expense_number, expense_date, amount, currency, notes, receipt_file
```

- `expense_date` — `YYYY-MM-DD`.
- `amount` — decimal, scaled by currency precision.
- `currency` — currency code (e.g. `USD`).
- `receipt_file` — exact filename placed in the zip, or empty if the expense has no
  receipt. Maps each CSV row 1:1 to a zip entry.

### Zip layout

Flat. Each receipt named `<expense_number>__<original_filename>` (sanitized).
Filename collisions are de-duped with a numeric suffix. If no expenses have receipts,
the zip is skipped and a note is printed.

## Error handling

- **401 / bad credentials** — clear message pointing at the config file.
- **Wrong `company_id`** — surfaced from the API error; message suggests checking
  `INVOICESHELF_COMPANY_ID`.
- **Invalid date format / impossible date / start > end** — validation error before any
  network calls.
- **Empty result set** — write a header-only CSV, print "0 expenses in range", skip zip.
- **Per-receipt download failure** — logged and collected into a summary at the end;
  does not abort the whole run. The affected expense's `receipt_file` is left blank in
  the CSV (since the CSV is written after downloads complete).

## Testing

- HTTP access sits behind a thin client class so the pure logic — `DDMMYYYY` parsing,
  amount scaling, filename sanitizing, CSV assembly, zip naming/collision handling — is
  unit-tested against recorded JSON fixtures, no live server needed.
- `--dry-run` exercises the full path (login, resolve, list) and reports what would be
  exported without downloading receipts or writing the zip.

## Out of scope

- Multiple receipts per expense (the 2.4.1 API exposes only the first via
  `show/receipt`).
- Multi-company batch export in a single run (one `company_id` per run).
- An MCP-server wrapper (possible future layer over this CLI; not built now).
