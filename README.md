# InvoiceShelf Expense Exporter

Export a client's expenses from a self-hosted InvoiceShelf (2.4.1) instance to CSV,
and bundle their receipts into a zip.

## Setup
1. Requires Python 3.8+ (standard library only — no pip install).
2. `cp config.env.example config.env` and fill in your URL, email, password, and company id.

## Usage
    python3 export_expenses.py --client "Acme Corp" --start 01042025 --end 30062025
    python3 export_expenses.py --customer-id 12 --start 01042025 --end 30062025 --out ./exports
    python3 export_expenses.py --client "Acme Corp" --start 01042025 --end 30062025 --dry-run

Dates are DDMMYYYY, inclusive. Outputs `<Client>_<start>-<end>.csv` and
`<Client>_<start>-<end>_receipts.zip` in `--out` (default `exports/`).

## Tests
    python3 -m unittest discover -s tests -v
