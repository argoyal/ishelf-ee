# Pinned: `GET /api/v1/expenses` response shape (list)

The corpus export (`--export-all-fields`) reads these fields per expense. `expense_to_row`
is written **shape-tolerant** (nested object preferred, id fallback) so it works whether the
instance embeds relations or returns bare ids:

| needed value | primary source | fallback |
|---|---|---|
| expense_id | `id` | — |
| amount / currency | `amount`, `currency.{code,precision}` | precision → 2 |
| category | `category.name` / `expense_category.name` | `expense_category_id` via `GET /categories` map |
| client | `customer.name` | `customer_id` (as string) |
| company | the company being queried (header) | — |
| exchange_rate / created_at | `exchange_rate`, `created_at` | "" |

**Live smoke-test (run once against the real instance to confirm which branch fires):**
`ee --company all --client all --export-all-fields --dry-run --config ~/.personal/configs/ishelf-config.env`
Then open the printed CSV path and confirm `category`/`client`/`company` are populated (names,
not blanks or bare ids). If `client` shows bare ids, the instance returns `customer_id` only —
still correct, just less readable; enhance later with a customer-id→name map if desired.
