# Design: `ee` full-corpus export (`--export-all-fields` + `all` scope)

**Date:** 2026-08-08 · **Repo:** `ishelf-ee` · **Branch:** `feat/export-all-fields`
**Status:** approved, pre-implementation

## Problem

The brain's expense-logging work needs a **learning corpus**: every historical InvoiceShelf
expense, with full fields and its attachment, to mine attribution signals and seed the graph.
Today's `ee` export can't produce it:
- It emits only 6 columns (`expense_number, expense_date, amount, currency, notes, receipt_file`)
  — dropping `company`, `client`, `category`, which are exactly the fields signals derive from.
- It is **per-client**: `run_export` requires a `customer_id` resolved from `--client`, so one run
  covers a single client + date range, not the whole history across all companies/clients.

## Goal

Extend the existing export (no new tool, no new dependency) so one command can produce the full
corpus, while leaving current behavior byte-for-byte unchanged when the new options are absent.

Two independent, composable additions:
1. **`--export-all-fields`** — widen the CSV to a superset of columns.
2. **`--company all` / `--client all`** — widen the scope to every company / every client.

`ee --company all --client all --export-all-fields` yields the whole corpus in one file.

## Global constraints (inherited from the repo)

- **Stdlib-only Python** — no third-party dependencies. (This is why output stays CSV, not `.xlsx`.)
- TDD; tests mock `urlopen`/`_request` (no live calls in the suite).
- Backward compatibility is a hard requirement: absent the new options, output is identical to today.

## Behavior

### `--export-all-fields` (columns)
- **Absent (default):** the current 6 columns, in the current order, unchanged.
- **Present:** the 6 existing columns first, then appended:
  `expense_id, company, client, category, exchange_rate, created_at`.
  - `category` — resolved from the expense's `expense_category_id` via a `category_id → name`
    map built from `list_categories()` (categories are company-scoped, so the map is rebuilt
    per company).
  - `client` — resolved from the expense's `customer` / `customer_id` to the customer name.
  - `company` — the name of the company the expense belongs to (known from the company being
    queried; in single-company mode, the resolved `--company`).
  - `expense_id`, `exchange_rate`, `created_at` — taken from the expense object.
- The flag is purely additive and independent of scope: it is valid with a single named client.

### `--company all` / `--client all` (scope)
- **`--company all`:** iterate every company from `list_companies()`, switching the `company`
  request header per company before its expense calls. (Auth token is company-agnostic; only the
  header changes.)
- **`--client all`:** call `list_expenses` with **no `customer_id`** — the API already supports an
  unfiltered company-scoped query (as `count_expenses_on_date` does), returning all clients'
  expenses (and client-less ones).
- Either sentinel is independent of `--export-all-fields`: `--company all` with default columns is
  valid (6-column output across all companies).
- **Dates optional in `all` scope:** when scope is `all`, `--start`/`--end` may be omitted → no
  date filter (all-time). Named-client mode keeps `--start`/`--end` required, as today.

### Output
- **Single-scope (named company + named client):** unchanged — one CSV + one receipts zip named
  as today (`<client>_<start>-<end>`).
- **`all` scope:** ONE combined corpus CSV + ONE combined receipts zip. The `company`/`client`
  columns distinguish rows. `ZipNamer` already de-duplicates receipt filenames across companies.
  Filename stem: `all-expenses` + date range (or `all-time` when dates omitted).

## Supporting code changes

- `list_expenses(customer_id=None, from_date=None, to_date=None)` — omit whichever query params
  are `None` (currently all three are always sent).
- `expense_to_row(expense, receipt_file, *, all_fields=False, category_map=None, company_name=None)`
  — returns the 6-column dict as today; when `all_fields`, adds the 6 extra resolved columns.
- `CSV_COLUMNS` stays the default; a second `CSV_COLUMNS_ALL = CSV_COLUMNS + [...]` for the wide mode.
- `run_export(...)` gains the scope/columns parameters and, in `all` scope, loops companies →
  (per company) rebuild category map → list all expenses → accumulate rows + receipts into the
  single combined CSV/zip.
- CLI: accept `all` as a sentinel for `--company`/`--client`; make `--start`/`--end` optional when
  either is `all`; add the `--export-all-fields` store_true flag.

## First implementation step (contract pinning)

Before writing row/resolution code, pin the live `GET /expenses` response shape against the real
instance: does it return `category` and `customer` **nested**, or only `expense_category_id` /
`customer_id`? Record it in `docs/superpowers/notes/` (as the create-API was pinned). Tests then
mock that exact shape. The resolver is written to whichever the API actually returns (nested → read
directly; IDs only → resolve via `list_categories()` + a customer-id→name map).

## Testing (TDD, mocked)

- **Backward compat:** default invocation → exactly the 6 current columns, current order (guard
  against accidental widening).
- **`--export-all-fields`:** wide header + rows; `category_id`/`customer_id` correctly resolved to
  names via a mocked `list_categories()` / customer data; `expense_id`/`exchange_rate`/`created_at`
  passed through.
- **`--client all`:** `list_expenses` called with no `customer_id`; rows from multiple customers
  appear with correct per-row `client`.
- **`--company all`:** iterates mocked `list_companies()`; header switched per company; rows tagged
  with correct per-row `company`; single combined CSV + zip produced.
- **Dates optional in `all` mode:** omitting `--start/--end` sends no date filter; named-client mode
  still errors without them.
- **Receipt de-dup across companies:** two expenses with same receipt filename in different
  companies → distinct zip entries, correct `receipt_file` per row.

## Out of scope (later pieces)

Graph ingestion of the corpus (Piece 2), signal taxonomy (Piece 3), brain rules (Piece 4). This
piece only produces the corpus artifact (CSV + attachments).
