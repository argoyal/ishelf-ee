# `ee create` — log an expense to InvoiceShelf

**Date:** 2026-08-07
**Status:** Draft (brainstorm output — pending review)
**Scope:** First slice of the supermemory *expense connector*: extend the `ee` CLI to **create** an expense in InvoiceShelf (with a receipt attachment), driven by a Claude agent. Attribution is **manual** for now. Everything else (rules engine, graph fact-store, statement reconciliation, Telegram front-end, voice/actions) is deferred — see Roadmap.

---

## Problem / context

`ee` today only **exports** expenses (CSV + receipts) from a self-hosted InvoiceShelf 2.4.1 instance. The expense pipeline needs the inverse: **create** an expense. The near-term driver is a **Claude agent** (using the user's existing Claude license — no paid API): the user pastes a receipt image into the agent chat, Claude reads it (vision), extracts amount/date/vendor/currency, the user/Claude decide which company it belongs to, and the agent calls `ee create …` to log it with the receipt attached.

This keeps the LLM/vision work on the existing license and makes the deterministic "invoice piece" a plain CLI the agent invokes.

## Goals

1. `ee create` — create one expense in InvoiceShelf for a chosen company, with amount/date/currency/category/notes and an **attached receipt file**.
2. Preserve today's behavior: `ee export …` works exactly as before (backward-compatible).
3. Reuse the existing `InvoiceShelfClient` auth (`login` → Bearer token, `company` header) and resolver pattern.
4. `--dry-run` prints the exact request without writing.
5. Stdlib-only (no new runtime dependencies), matching the current package.
6. A thin `log-expense` capability in `~/.brain` so any agent knows the flow.

## Non-goals (deferred to later slices)

Attribution **rules engine** (auto-pick company); the **graph fact-store** / emergent Expense facts; **statement reconciliation** (matching receipts ↔ card-statement lines); the **Telegram** front-end (receipt inbox + confirm UI); **voice notes → intent → action**; bulk/batch creation; editing/deleting expenses; RAG over past transactions.

## Guiding constraints

- **Stdlib-only** — extend `urllib`-based client; no new deps (the package advertises zero runtime deps).
- **Backward-compatible** — existing `ee --client … --start … --end …` invocation keeps exporting.
- **Verify the InvoiceShelf API first** — the exact 2.4.1 create-expense field set + receipt-attach mechanism is a due-diligence gate (Task 1), like pinning the Kùzu API.
- **Never read/echo secrets** — `config.env` holds the password; the client reads it internally (Mode-1); tests/dry-run must not print it.
- **TDD** — pure logic unit-tested; a `--dry-run` request preview; a gated live smoke for manual verification (a live API can't be unit-tested).
- **Manual attribution** — the company is passed in via `--company`; no inference in this slice.

---

## Design

### CLI — subcommands (backward-compatible)
Introduce subcommands while keeping the old invocation working:
- `ee export …` — the current exporter (all current flags unchanged).
- `ee create …` — new.
- **No subcommand → defaults to `export`** (so existing scripts/`ee --client … --start …` keep working). Implementation: detect whether `argv[0]` is a known subcommand; if not, prepend `export`.

### `ee create` flags
`--company "Name"` (required; resolved via existing `resolve_company_id`), `--amount 12.34` (decimal), `--currency USD` (code), `--date DDMMYYYY`, `--category "Name"` (resolved to id), `--notes "…"` (a.k.a. `--vendor`), `--customer "Name"` (optional; resolved via `resolve_customer_id`), `--receipt PATH` (attach), `--config PATH`, `--dry-run`.

### Client additions (in `ishelf_ee/__init__.py`)
- `create_expense(*, company_id, expense_date, amount_minor, currency_id, category_id, notes, customer_id=None, receipt=None) -> dict` — `POST /expenses`. If `receipt` is given, send as **multipart/form-data** (the receipt is a file); otherwise JSON.
- `resolve_category_id(client, name)` and `resolve_currency_id(client, code)` — mirror `resolve_company_id` (GET the list, exact-match, helpful error listing options).
- **Multipart support** — the current `_request` only encodes JSON bodies. Add a small stdlib multipart encoder (`multipart/form-data` boundary + fields + file part) and a `_request` path (or a sibling `_request_multipart`) for the create-with-receipt call.
- `amount_to_minor(decimal_str, precision) -> int` — inverse of the existing `scale_amount` (e.g. `"12.34", 2 → 1234`).

### Data flow (Claude-driven, this slice)
```
user pastes receipt image in Claude chat
  → Claude (vision) extracts amount, date, currency, vendor
  → user/Claude choose the company (manual)
  → Claude saves the image and runs:
       ee create --company "AsterHQ" --amount 12.34 --currency USD \
                 --date 07082026 --category "Software" --notes "Anthropic" \
                 --receipt /path/to/receipt.png
  → InvoiceShelf expense created, receipt attached
```

### The InvoiceShelf create-expense API (to be pinned — Task 1)
Verify against the running instance / InvoiceShelf 2.4.1 docs *before* coding `create_expense`:
- Endpoint + method (expected `POST /api/v1/expenses`).
- Required fields (`expense_date`, `amount` in minor units, `expense_category_id`?, `currency_id`?, `customer_id`?, `notes`, company via header).
- **Receipt attach**: multipart file field name (e.g. `attachment_receipt`) vs a separate upload endpoint vs base64.
- Whether a **category** is mandatory (affects `--category` required-ness).

---

## Components

- **C1. InvoiceShelf create-expense API due-diligence** — hit the instance (a throwaway/dry probe or docs) to pin the create endpoint + field set + receipt mechanism; record findings. Gate for C4/C5.
- **C2. Subcommand CLI** — `export`/`create` subparsers; no-subcommand → `export`; `export` behavior unchanged (regression-tested).
- **C3. Amount + body helpers** — `amount_to_minor`; build the create request body from args + resolved ids.
- **C4. Multipart receipt upload** — stdlib multipart encoder + client path; unit-tested on the encoded bytes.
- **C5. `create_expense` + `resolve_category_id`/`resolve_currency_id`** — the client method(s), matching the pinned API.
- **C6. `ee create` command wiring + `--dry-run`** — parse → resolve company/customer/category/currency → build request → (dry-run prints it) → POST → print result.
- **C7. Brain `log-expense` capability** — `~/.brain/ontology/domains/expenses.md` (thin) + `ontology/capabilities/expenses/log-expense.md`: the extract → decide-company → `ee create` flow. The seam the future rules engine + Telegram plug into.

## Testing / verification

- **Pure logic (unit, TDD):** subcommand routing (incl. no-subcommand→export); `amount_to_minor` (incl. precision, sign, rounding rules — decide truncate vs round and test it); request-body construction; **multipart encoding** (assert the byte structure: boundary, field parts, file part headers).
- **Backward-compat:** existing export path still parses + runs (regression test the arg routing).
- **`--dry-run`:** prints the exact method/URL/body (secrets redacted) and exits without network.
- **Gated live smoke (manual):** against the real instance behind an env flag — create a tiny expense with a dummy receipt, confirm it appears in InvoiceShelf, then note it can be deleted. Not part of the unit suite.
- **No-secrets:** tests + dry-run never print the password/token.

## Rollout / task order
1. C1 API due-diligence (pin the create-expense contract).
2. C2 subcommand CLI (+ backward-compat regression).
3. C3 amount/body helpers.
4. C4 multipart encoder.
5. C5 `create_expense` + category/currency resolvers.
6. C6 `ee create` wiring + `--dry-run` + gated live smoke.
7. C7 brain `log-expense` capability.
8. README: document `ee create`.

## Out of scope / Roadmap (the vision — recorded per owner)

This slice is the *engine's action*. The larger vision it plugs into, in order:
- **Attribution rules engine** — a resolver cascade (rules → RAG+LLM → human-fallback) that auto-picks the company/category; rules are authored data in the brain; every human correction becomes a precedent and, when stable, is promoted to a rule.
- **Statement reconciliation** — the card statement is the transaction anchor; receipts (email + Telegram) enrich + attribute (the *invoice account* disambiguates a vendor that spans companies, e.g. Anthropic across AsterHQ/personal/PeopleEquation).
- **Emergent fact store** — Expense facts in the brain (Git-LFS), projected into the Kùzu graph.
- **Telegram front-end** — the brain's agentic surface: receipts in (photos of physical receipts), "which company?" confirmations out; later **voice notes → intent → action** across all the brain's capabilities. Deferred until the Claude-license-vs-paid-API question is resolved (for now the Claude agent chat is the surface).

## Open questions
1. InvoiceShelf 2.4.1 create-expense exact fields + receipt-attach mechanism (Task 1 pins this).
2. Is `expense_category_id` required? (decides whether `--category` is mandatory).
3. Currency handling — pass `currency_id` (resolved from code) or a code directly? (pin in Task 1).
4. Amount rounding policy for `amount_to_minor` (truncate vs round-half-up) — pick one, test it.
