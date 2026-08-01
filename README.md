# ishelf-ee — InvoiceShelf Expense Exporter

Export a client's expenses from a self-hosted InvoiceShelf (2.4.1) instance to CSV,
and bundle their receipts into a zip. Zero runtime dependencies (Python standard
library only). Installs the `ee` command.

## Install

    pip install ishelf-ee

Requires Python 3.8+. Or run from a clone without installing:

    python3 -m ishelf_ee --client "Acme Corp" --start 01042025 --end 30062025
    # or the legacy shim:
    python3 export_expenses.py --client "Acme Corp" --start 01042025 --end 30062025

## Configure

The tool needs your instance URL and login. Provide them either as environment
variables or in a config file. **Never commit or share your filled-in config — it
holds your password.**

Required: `INVOICESHELF_URL`, `INVOICESHELF_EMAIL`, `INVOICESHELF_PASSWORD`.
Optional: `INVOICESHELF_COMPANY_ID` (default `1`; or use `--company`),
`INVOICESHELF_USER_AGENT` (see Troubleshooting).

### Option A — environment variables (work from any directory)

    export INVOICESHELF_URL=https://invoices.example.com
    export INVOICESHELF_EMAIL=you@example.com
    export INVOICESHELF_PASSWORD=your-password

### Option B — a config file

Copy the template and fill it in:

    mkdir -p ~/.config/ishelf-ee
    curl -o ~/.config/ishelf-ee/config.env \
      https://raw.githubusercontent.com/<you>/ishelf-ee/main/config.env.example
    # then edit ~/.config/ishelf-ee/config.env

The config file is discovered in this order (first match wins):

1. `--config PATH` (explicit)
2. `$INVOICESHELF_CONFIG`
3. `./config.env` (current directory)
4. `~/.config/ishelf-ee/config.env`  ← recommended for the installed `ee` command
5. `~/.ishelf-ee.env`

Environment variables always override file values.

## Usage

    ee --client "Acme Corp" --start 01042025 --end 30062025
    ee --company "My Company" --client "Acme Corp" --start 01042025 --end 30062025
    ee --customer-id 12 --start 01042025 --end 30062025 --out ./exports
    ee --client "Acme Corp" --start 01042025 --end 30062025 --dry-run

Dates are `DDMMYYYY`, inclusive. Outputs `<Client>_<start>-<end>.csv` and
`<Client>_<start>-<end>_receipts.zip` into `--out` (default `exports/`). Use
`--dry-run` to see what would be exported without downloading or writing anything.

### Choosing the company

If you run more than one company in InvoiceShelf, pass `--company "Name"` to scope
the export; the name is resolved to its id (case-insensitive, exact match preferred).
`--company` overrides `INVOICESHELF_COMPANY_ID`. On an ambiguous or unknown name, the
tool lists the available companies as `id — name` so you can pick.

## Troubleshooting

**Cloudflare "Error 1010: Access denied" / HTTP 403 on login.** The instance is
behind Cloudflare, which bans the default Python User-Agent. The tool already sends
a browser User-Agent. If your Cloudflare config still blocks it, override it:

    INVOICESHELF_USER_AGENT="Mozilla/5.0 (...)" ee ...

If it persists, allowlist your own access in the Cloudflare dashboard (e.g. a WAF
skip rule for the `/api/*` path, or turn off Bot Fight Mode for the API) — it is
your own site.

## Develop / test

    python3 -m unittest discover -s tests -v
