# InvoiceShelf Expense Exporter

Export a client's expenses from a self-hosted InvoiceShelf (2.4.1) instance to CSV,
and bundle their receipts into a zip.

## Setup
1. Requires Python 3.8+ (standard library only — no pip install).
2. `cp config.env.example config.env` and fill in your URL, email, and password.
   `INVOICESHELF_COMPANY_ID` is optional — set it, or select the company per run
   with `--company` (see below).

## Usage
    python3 export_expenses.py --client "Acme Corp" --start 01042025 --end 30062025
    python3 export_expenses.py --company "My Company" --client "Acme Corp" --start 01042025 --end 30062025
    python3 export_expenses.py --customer-id 12 --start 01042025 --end 30062025 --out ./exports
    python3 export_expenses.py --client "Acme Corp" --start 01042025 --end 30062025 --dry-run

Dates are DDMMYYYY, inclusive. Outputs `<Client>_<start>-<end>.csv` and
`<Client>_<start>-<end>_receipts.zip` in `--out` (default `exports/`).

### Choosing the company
If you run more than one company in InvoiceShelf, pass `--company "Name"` to scope
the export; the name is resolved to its id (case-insensitive, exact match preferred).
`--company` overrides `INVOICESHELF_COMPANY_ID`. If neither is given, the default
company id (`1`) is used, and InvoiceShelf falls back to your first company if that
id isn't yours. On an ambiguous or unknown name, the tool lists the available
companies as `id — name` so you can pick.

## Tests
    python3 -m unittest discover -s tests -v
