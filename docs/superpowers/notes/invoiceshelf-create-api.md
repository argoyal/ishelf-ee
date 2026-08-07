# InvoiceShelf 2.4.1 — create-expense API contract (pinned from source)

Read from `InvoiceShelf/InvoiceShelf` @ tag **2.4.1** (`app/Http/Requests/ExpenseRequest.php`, `routes/api.php`). Instance is 2.4.1, so 3.x (the repo default branch) does **not** apply.

## Endpoint
- **Create:** `POST /api/v1/expenses` — `Route::apiResource('expenses', ExpensesController::class)`.
- Company scoping via the existing `company` request header (already handled by `InvoiceShelfClient`).

## Fields (from `ExpenseRequest::rules()`)
| field | required? | notes |
|---|---|---|
| `expense_date` | **required** | `YYYY-MM-DD` |
| `expense_category_id` | **required** | int — resolve from name |
| `amount` | **required** | integer **minor units** (matches export's `scale_amount`) |
| `currency_id` | **required** | int — resolve from currency code |
| `exchange_rate` | conditional | **required only if** `currency_id` != company's default currency; else the server treats it as 1 |
| `expense_number` | nullable | string |
| `payment_method_id` | nullable | int |
| `customer_id` | nullable | int |
| `notes` | nullable | string |
| `attachment_receipt` | nullable | **FILE** — mimes: jpg,png,pdf,doc,docx,xls,xlsx,ppt,pptx; max 20000 KB (20 MB) |

## Receipt attach
- `attachment_receipt` is validated as a **`file`** → the create request must be **`multipart/form-data`** when a receipt is included (all fields become form fields + the file part named `attachment_receipt`). Without a receipt, plain JSON works.
- (There is also a separate `UploadReceiptController` at an upload endpoint, but inline `attachment_receipt` on create is one call — use that.)

## List endpoints (for resolvers)
- **Categories:** `GET /api/v1/categories` (`apiResource('categories', ExpenseCategoriesController)`) — resolve by `name`.
- **Currencies:** `GET /api/v1/currencies` (`CurrenciesController`) — resolve by `code`. **Response shape may be a bare list or `{data:[…]}`** → the client's list methods must handle both.

## Refinements to the plan
1. Confirmed: `attachment_receipt` (multipart), `/categories`, `/currencies`, `POST /expenses`, amount in minor units — plan is accurate.
2. Add an optional `--exchange-rate` flag; needed only when the expense currency differs from the company's default currency (otherwise omit).
3. `list_categories`/`list_currencies` must accept bare-list-or-`{data:}` responses (use an `_as_list` helper).
