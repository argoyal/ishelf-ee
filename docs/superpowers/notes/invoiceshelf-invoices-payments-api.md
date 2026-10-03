# InvoiceShelf 2.4.1 — invoices and payments API (pinned from source)

Read from `InvoiceShelf/InvoiceShelf` @ tag **2.4.1** (`app/Http/Requests/InvoicesRequest.php`,
`PaymentRequest.php`, `app/Models/Invoice.php`, `Payment.php`, `app/Support/DocumentTotals.php`,
`routes/api.php`). Checked live against the instance on 2026-10-03 (list + dry runs).

## Routes
| what | route |
|---|---|
| list / create / show | `GET` / `POST /invoices`, `GET` / `POST /payments` (`apiResource`) |
| delete | `POST /invoices/delete`, `POST /payments/delete` with `{"ids": [...]}` |
| next number | `GET /next-number?key=invoice\|payment[&userId=<customer_id>]` → `{success, nextNumber}` |
| company currency | `GET /company/settings?settings[]=currency` → `{"currency": "<id>"}` |

List filters: `customer_id`, `from_date` + `to_date` (both needed, `YYYY-MM-DD`), `limit=all`.

## Currency (the key rule)
Both invoices and payments are stored in **the customer's currency** — the server overwrites
`currency_id` with `customer.currency_id`. `exchange_rate` is **required** when that differs from
the company currency, and the server computes `exchange_rate = (company_currency != request.currency_id)
? request.exchange_rate : 1`. So the client must send `currency_id` = the customer's currency,
or the rate is silently ignored/mis-set. `exchange_rate` is `decimal(19,6)`; base (company-currency)
values are `amount * exchange_rate`, stored as integer minor units.

## Invoice create — required fields
`invoice_date`, `customer_id`, `invoice_number` (unique per company), `discount`, `discount_val`
(int), `sub_total`, `total`, `tax`, `template_name`, `items[]` with `name`, `quantity`, `price`.
The server **recomputes** `sub_total`/`total`/`tax` from the items (GHSA-8c69), but
`createItems` reads `discount_val` and `tax` on every item directly, so send them (0).
`due_date` is optional. New invoices are `DRAFT` / `UNPAID`.

## Payment create
Required: `payment_date`, `customer_id`, `amount` (minor units), `payment_number` (unique per
company). Optional: `invoice_id`, `payment_method_id`, `notes`, `exchange_rate` (required when
foreign). With `invoice_id`, the invoice's `due_amount` is reduced by `amount` and its paid status
updated. **No overpayment check on the server** — `ee` refuses amounts above `due_amount`.

**No attachments:** `PaymentRequest` has no file field and there is no payment upload route in
2.4.1, so an eFIRC can only be referenced in `notes`.

## Delete side effects
- Invoice delete fails (422) while the invoice has payments (`RelationNotExist` rule).
- Payment delete adds the amount back to the invoice's `due_amount` and resets its paid status.
