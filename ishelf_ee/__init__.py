"""InvoiceShelf expense exporter — CSV + receipts zip for a client and date range."""
import argparse
import csv
import datetime
import decimal
import json
import os
import re
import sys
import typing
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

CSV_COLUMNS = ["expense_number", "expense_date", "amount", "currency", "notes", "receipt_file"]
CSV_COLUMNS_ALL = CSV_COLUMNS + ["expense_id", "company", "client", "category", "exchange_rate", "created_at"]

Config = typing.NamedTuple("Config", [
    ("url", str), ("email", str), ("password", str), ("company_id", str),
])

_REQUIRED = {
    "url": "INVOICESHELF_URL",
    "email": "INVOICESHELF_EMAIL",
    "password": "INVOICESHELF_PASSWORD",
}


def _read_env_file(path):
    values = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def load_config(path):
    file_vals = _read_env_file(path)

    def get(key):
        return os.environ.get(key, file_vals.get(key, "")).strip()

    resolved = {field: get(env_key) for field, env_key in _REQUIRED.items()}
    missing = [env_key for field, env_key in _REQUIRED.items() if not resolved[field]]
    if missing:
        raise ValueError(
            "Missing required config: %s. Set them as environment variables, or in a "
            "config file (~/.config/ishelf-ee/config.env, ./config.env, or --config PATH)."
            % ", ".join(sorted(missing)))
    company_id = get("INVOICESHELF_COMPANY_ID") or "1"
    return Config(url=resolved["url"], email=resolved["email"],
                  password=resolved["password"], company_id=company_id)


def _config_candidates():
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    candidates = []
    env_path = os.environ.get("INVOICESHELF_CONFIG")
    if env_path:
        candidates.append(env_path)
    candidates.append(os.path.join(os.getcwd(), "config.env"))
    candidates.append(os.path.join(xdg, "ishelf-ee", "config.env"))
    candidates.append(os.path.join(home, ".ishelf-ee.env"))
    return candidates


def find_config_file(explicit=None):
    """Resolve which config file to read. An explicit --config path is used as
    given; otherwise the first existing well-known location is returned, or None
    (env vars alone may satisfy the required config)."""
    if explicit:
        return explicit
    for path in _config_candidates():
        if os.path.exists(path):
            return path
    return None


def parse_ddmmyyyy(s):
    if not isinstance(s, str) or len(s) != 8 or not s.isdigit():
        raise ValueError("Date must be 8 digits in DDMMYYYY format, got: %r" % s)
    return datetime.datetime.strptime(s, "%d%m%Y").date()


def to_api_date(d):
    return d.strftime("%Y-%m-%d")


def to_filename_date(d):
    return d.strftime("%d%m%Y")


def validate_range(start, end):
    if start > end:
        raise ValueError("start date must not be after end date")


def scale_amount(minor, precision=2):
    minor = int(minor)
    precision = int(precision)
    if precision <= 0:
        return str(minor)
    sign = "-" if minor < 0 else ""
    digits = str(abs(minor)).rjust(precision + 1, "0")
    return "%s%s.%s" % (sign, digits[:-precision], digits[-precision:])


_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')


def sanitize_filename(name):
    cleaned = _UNSAFE.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned or "receipt"


class ZipNamer:
    def __init__(self):
        self._used = set()

    def allocate(self, desired):
        desired = sanitize_filename(desired)
        if desired not in self._used:
            self._used.add(desired)
            return desired
        root, ext = os.path.splitext(desired)
        i = 1
        while True:
            candidate = "%s_%d%s" % (root, i, ext)
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate
            i += 1


def expense_to_row(expense, receipt_file, *, all_fields=False, category_map=None, company_name=None):
    currency = expense.get("currency") or {}
    precision = currency.get("precision", 2)
    if precision in (None, ""):
        precision = 2
    row = {
        "expense_number": expense.get("expense_number") or "",
        "expense_date": expense.get("expense_date") or "",
        "amount": scale_amount(expense.get("amount") or 0, precision),
        "currency": currency.get("code") or "",
        "notes": expense.get("notes") or "",
        "receipt_file": receipt_file or "",
    }
    if not all_fields:
        return row
    category_map = category_map or {}
    # Client: prefer a nested customer object, else the raw customer_id.
    customer = expense.get("customer") or {}
    client_name = customer.get("name") or (
        str(expense["customer_id"]) if expense.get("customer_id") is not None else "")
    # Category: prefer a nested category object, else resolve the id via the map.
    cat = expense.get("category") or expense.get("expense_category")
    cat_name = cat.get("name") if isinstance(cat, dict) else ""
    if not cat_name:
        cat_name = category_map.get(expense.get("expense_category_id"), "")
    row.update({
        "expense_id": expense.get("id") or "",
        "company": company_name or "",
        "client": client_name,
        "category": cat_name,
        "exchange_rate": expense.get("exchange_rate") or "",
        "created_at": expense.get("created_at") or "",
    })
    return row


def write_csv(rows, path, columns=CSV_COLUMNS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


_TIMEOUT = 60

# Some InvoiceShelf instances sit behind Cloudflare, whose bot rules ban the
# default "Python-urllib/x.y" User-Agent (error 1010). Present a normal browser
# UA instead. Override with the INVOICESHELF_USER_AGENT env var if that is also
# filtered.
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class InvoiceShelfClient:
    def __init__(self, config):
        self.config = config
        self.base = config.url.rstrip("/") + "/api/v1"
        self.token = None
        # Mutable so a resolved --company can override the config default before
        # any company-scoped call (customers, expenses, receipts) is made.
        self.company_id = config.company_id
        self.user_agent = os.environ.get("INVOICESHELF_USER_AGENT") or _USER_AGENT

    def _request(self, method, path, query=None, body=None, auth=True, raw=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["company"] = str(self.company_id)
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = err.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise ApiError("HTTP %s for %s: %s" % (err.code, url, detail), status=err.code)
        except urllib.error.URLError as err:
            raise ApiError("Could not reach %s: %s" % (url, err.reason))
        if raw:
            return payload
        return json.loads(payload.decode("utf-8"))

    def login(self):
        resp = self._request("POST", "/auth/login", body={
            "username": self.config.email,
            "password": self.config.password,
            "device_name": "expense-exporter",
        }, auth=False)
        self.token = resp.get("token")
        if not self.token:
            raise ApiError("Login succeeded but no token returned")
        return self.token

    def list_companies(self):
        # Returns every company the authenticated user belongs to (no server-side
        # name filter). The 'company' middleware falls back to the user's first
        # company when the header is absent/invalid, so this call works before a
        # specific company is chosen.
        resp = self._request("GET", "/companies")
        return resp.get("data", [])

    def find_customers(self, name):
        resp = self._request("GET", "/customers", query={"search": name, "limit": "all"})
        return resp.get("data", [])

    def list_expenses(self, customer_id=None, from_date=None, to_date=None):
        query = {"limit": "all"}
        if customer_id is not None:
            query["customer_id"] = customer_id
        if from_date:
            query["from_date"] = from_date
        if to_date:
            query["to_date"] = to_date
        resp = self._request("GET", "/expenses", query=query)
        return resp.get("data", [])

    def download_receipt(self, expense_id):
        return self._request("GET", "/expenses/%s/show/receipt" % expense_id, raw=True)

    def list_categories(self):
        return _as_list(self._request("GET", "/categories", query={"limit": "all"}))

    def list_payment_methods(self):
        return _as_list(self._request("GET", "/payment-methods", query={"limit": "all"}))

    def list_currencies(self):
        return _as_list(self._request("GET", "/currencies"))

    def count_expenses_on_date(self, api_date):
        """Company-scoped count of expenses whose expense_date is api_date (YYYY-MM-DD)."""
        resp = self._request("GET", "/expenses",
                             query={"from_date": api_date, "to_date": api_date, "limit": "all"})
        return len(_as_list(resp))

    def create_expense(self, body, receipt=None):
        if receipt is None:
            return self._request("POST", "/expenses", body=body)
        filename, content, ctype = receipt
        data, content_type = encode_multipart(
            {k: v for k, v in body.items()},
            file_field=("attachment_receipt", filename, content, ctype))
        return self._request_multipart("POST", "/expenses", data, content_type)

    def _request_multipart(self, method, path, data, content_type):
        url = self.base + path
        headers = {"Accept": "application/json", "User-Agent": self.user_agent,
                   "Content-Type": content_type, "company": str(self.company_id)}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = err.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise ApiError("HTTP %s for %s: %s" % (err.code, url, detail), status=err.code)


def resolve_customer_id(client, name):
    customers = client.find_customers(name)
    if not customers:
        raise LookupError("No customer found matching %r." % name)
    exact = [c for c in customers if (c.get("name") or "").lower() == name.lower()]
    if len(exact) == 1:
        return int(exact[0]["id"])
    if len(exact) == 0 and len(customers) == 1:
        return int(customers[0]["id"])
    candidates = exact if exact else customers
    listing = "\n".join("  %s — %s" % (c.get("id"), c.get("name")) for c in candidates)
    raise LookupError(
        "Multiple customers match %r. Rerun with --customer-id <id>:\n%s" % (name, listing))


def resolve_company_id(client, name):
    companies = client.list_companies()
    lname = name.lower()
    exact = [c for c in companies if (c.get("name") or "").lower() == lname]
    if len(exact) == 1:
        return int(exact[0]["id"])
    if exact:
        matches = exact  # >1 exact match (unusual)
    else:
        matches = [c for c in companies if lname in (c.get("name") or "").lower()]
        if len(matches) == 1:
            return int(matches[0]["id"])
    if not matches:
        available = "\n".join(
            "  %s — %s" % (c.get("id"), c.get("name")) for c in companies) or "  (none)"
        raise LookupError(
            "No company found matching %r. Available companies:\n%s" % (name, available))
    listing = "\n".join("  %s — %s" % (c.get("id"), c.get("name")) for c in matches)
    raise LookupError(
        "Multiple companies match %r. Use an exact --company name or set "
        "INVOICESHELF_COMPANY_ID to one of:\n%s" % (name, listing))


def amount_to_minor(amount_str, precision=2):
    try:
        d = decimal.Decimal(str(amount_str))
    except decimal.InvalidOperation:
        raise ValueError("amount must be a decimal number, got %r" % (amount_str,))
    scaled = d.scaleb(int(precision)).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP)
    return int(scaled)


def build_expense_body(*, expense_date, amount_minor, category_id, currency_id, notes,
                       customer_id=None, exchange_rate=None, expense_number=None,
                       payment_method_id=None):
    body = {
        "expense_date": expense_date,
        "amount": int(amount_minor),
        "expense_category_id": category_id,
        "currency_id": currency_id,
        "notes": notes or "",
    }
    if customer_id is not None:
        body["customer_id"] = customer_id
    if exchange_rate is not None:
        body["exchange_rate"] = exchange_rate
    if expense_number:
        body["expense_number"] = expense_number
    if payment_method_id is not None:
        body["payment_method_id"] = payment_method_id
    return body


def next_expense_number(date_ddmmyyyy, existing_count):
    """Auto number: <DDMMYYYY><NN>, NN = existing_count + 1 (2-digit, e.g. 0708202601)."""
    return "%s%02d" % (date_ddmmyyyy, int(existing_count) + 1)


def encode_multipart(fields, file_field=None):
    boundary = "----ee" + uuid.uuid4().hex
    b = boundary.encode()
    parts = []
    for name, value in (fields or {}).items():
        parts.append(b"--" + b + b"\r\n")
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        parts.append(str(value).encode("utf-8") + b"\r\n")
    if file_field is not None:
        name, filename, content, ctype = file_field
        parts.append(b"--" + b + b"\r\n")
        parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)).encode())
        parts.append(("Content-Type: %s\r\n\r\n" % ctype).encode())
        parts.append(content + b"\r\n")
    parts.append(b"--" + b + b"--\r\n")
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _as_list(resp):
    return resp if isinstance(resp, list) else (resp or {}).get("data", [])


def _resolve_by_name(items, name, kind, key="name"):
    lname = (name or "").lower()
    exact = [i for i in items if (i.get(key) or "").lower() == lname]
    if len(exact) == 1:
        return int(exact[0]["id"])
    matches = exact or [i for i in items if lname in (i.get(key) or "").lower()]
    if len(matches) == 1:
        return int(matches[0]["id"])
    listing = "\n".join("  %s — %s" % (i.get("id"), i.get(key)) for i in (matches or items)) or "  (none)"
    raise LookupError("Ambiguous or absent %s %r. Options:\n%s" % (kind, name, listing))


def resolve_category_id(client, name):
    return _resolve_by_name(client.list_categories(), name, "category")


def resolve_currency_id(client, code):
    return _resolve_by_name(client.list_currencies(), code, "currency", key="code")


def resolve_payment_method_id(client, name):
    return _resolve_by_name(client.list_payment_methods(), name, "payment method")


SUBCOMMANDS = ("export", "create")


def split_subcommand(argv):
    argv = list(argv or [])
    if argv and argv[0] in SUBCOMMANDS:
        return argv[0], argv[1:]
    return "export", argv


ExportSummary = typing.NamedTuple("ExportSummary", [
    ("expense_count", int), ("receipts_downloaded", int),
    ("receipt_failures", list), ("csv_path", str), ("zip_path", typing.Optional[str]),
])


def _category_map(client):
    return {c.get("id"): c.get("name") for c in client.list_categories()}


def _collect_rows(client, expenses, namer, *, all_fields=False, category_map=None,
                  company_name=None, dry_run=False):
    rows, stored, failures = [], [], []
    receipts_available = 0
    for expense in expenses:
        meta = expense.get("attachment_receipt_meta")
        receipt_file = ""
        if meta:
            receipts_available += 1
            entry = namer.allocate("%s__%s" % (
                expense.get("expense_number") or expense.get("id"),
                meta.get("file_name") or "receipt"))
            if dry_run:
                receipt_file = entry
            else:
                try:
                    data = client.download_receipt(expense["id"])
                    stored.append((entry, data))
                    receipt_file = entry
                except Exception as err:  # noqa: BLE001 - report, don't abort
                    failures.append((expense.get("expense_number") or expense.get("id"), str(err)))
        rows.append(expense_to_row(expense, receipt_file, all_fields=all_fields,
                                   category_map=category_map, company_name=company_name))
    return rows, stored, failures, receipts_available


def _write_export(rows, stored, failures, receipts_available, expense_count,
                  out_dir, stem, columns, dry_run):
    csv_path = os.path.join(out_dir, stem + ".csv")
    zip_path = os.path.join(out_dir, stem + "_receipts.zip")
    downloaded = receipts_available if dry_run else len(stored)
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
        write_csv(rows, csv_path, columns=columns)
        if stored:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in stored:
                    zf.writestr(name, data)
        else:
            zip_path = None
    else:
        zip_path = zip_path if receipts_available else None
    return ExportSummary(
        expense_count=expense_count, receipts_downloaded=downloaded,
        receipt_failures=failures, csv_path=csv_path, zip_path=zip_path)


def run_export(client, *, customer_id, client_label, start_date, end_date, out_dir,
               dry_run=False, all_fields=False):
    expenses = client.list_expenses(
        customer_id, to_api_date(start_date), to_api_date(end_date))
    stem = "%s_%s-%s" % (
        sanitize_filename(client_label),
        to_filename_date(start_date), to_filename_date(end_date))
    category_map = _category_map(client) if all_fields else None
    columns = CSV_COLUMNS_ALL if all_fields else CSV_COLUMNS
    namer = ZipNamer()
    rows, stored, failures, receipts_available = _collect_rows(
        client, expenses, namer, all_fields=all_fields, category_map=category_map,
        company_name=None, dry_run=dry_run)
    return _write_export(rows, stored, failures, receipts_available, len(expenses),
                         out_dir, stem, columns, dry_run)


def run_corpus_export(client, *, out_dir, all_fields=True, start_date=None, end_date=None,
                      dry_run=False, customer_id=None, all_companies=False):
    companies = client.list_companies() if all_companies else [None]
    frm = to_api_date(start_date) if start_date else None
    to = to_api_date(end_date) if end_date else None
    namer = ZipNamer()  # shared across companies → receipt names de-dup globally
    all_rows, all_stored, all_failures = [], [], []
    total = 0
    receipts_available = 0
    for company in companies:
        company_name = None
        if company is not None:
            client.company_id = company.get("id")
            company_name = company.get("name")
        category_map = _category_map(client) if all_fields else None
        expenses = client.list_expenses(customer_id=customer_id, from_date=frm, to_date=to)
        total += len(expenses)
        rows, stored, failures, avail = _collect_rows(
            client, expenses, namer, all_fields=all_fields, category_map=category_map,
            company_name=company_name, dry_run=dry_run)
        all_rows += rows
        all_stored += stored
        all_failures += failures
        receipts_available += avail
    if start_date and end_date:
        span = "%s-%s" % (to_filename_date(start_date), to_filename_date(end_date))
    else:
        span = "all-time"
    stem = "all-expenses_%s" % span
    columns = CSV_COLUMNS_ALL if all_fields else CSV_COLUMNS
    return _write_export(all_rows, all_stored, all_failures, receipts_available,
                         total, out_dir, stem, columns, dry_run)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Export InvoiceShelf expenses (CSV) and receipts (zip) for a client and date range.")
    parser.add_argument("--client", help="Client (customer) name to export.")
    parser.add_argument("--company", default=None,
                        help="Company name to scope the export to (resolved to its id). "
                             "Overrides INVOICESHELF_COMPANY_ID from config.")
    parser.add_argument("--customer-id", type=int, default=None,
                        help="Use this customer id directly, skipping name lookup.")
    parser.add_argument("--start", default=None,
                        help="Start date, inclusive, DDMMYYYY. Required unless --company all / --client all.")
    parser.add_argument("--end", default=None,
                        help="End date, inclusive, DDMMYYYY. Required unless --company all / --client all.")
    parser.add_argument("--export-all-fields", action="store_true",
                        help="Export the full field set (company, client, category, exchange_rate, "
                             "expense_id, created_at) instead of the default six columns.")
    parser.add_argument("--out", default="exports", help="Output directory (default: exports).")
    parser.add_argument("--config", default=None,
                        help="Path to config env file. If omitted, searches "
                             "$INVOICESHELF_CONFIG, ./config.env, "
                             "~/.config/ishelf-ee/config.env, ~/.ishelf-ee.env. "
                             "Config values can also come entirely from environment variables.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be exported; download and write nothing.")
    return parser


def run_export_cli(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        company_all = (args.company or "").lower() == "all"
        client_all = (args.client or "").lower() == "all"
        corpus = company_all or client_all

        start = end = None
        if args.start and args.end:
            start = parse_ddmmyyyy(args.start)
            end = parse_ddmmyyyy(args.end)
            validate_range(start, end)
        elif not corpus:
            raise ValueError("Provide --start and --end (DDMMYYYY), "
                             "or use --company all / --client all for the full corpus.")

        if company_all and (
                (args.client and not client_all) or args.customer_id is not None):
            raise ValueError(
                "--company all cannot be combined with a specific --client/--customer-id; "
                "use --client all (or omit it) for the full corpus, or name a single --company.")

        if not corpus and not args.client and args.customer_id is None:
            raise ValueError("Provide --client NAME or --customer-id ID.")

        config = load_config(find_config_file(args.config))
        client = InvoiceShelfClient(config)
        client.login()

        if corpus:
            if not company_all and args.company:
                client.company_id = resolve_company_id(client, args.company)
            customer_id = None
            if not client_all:
                customer_id = (args.customer_id if args.customer_id is not None
                               else resolve_customer_id(client, args.client))
            summary = run_corpus_export(
                client, out_dir=args.out, all_fields=args.export_all_fields,
                start_date=start, end_date=end, dry_run=args.dry_run,
                customer_id=customer_id, all_companies=company_all)
        else:
            if args.company:
                client.company_id = resolve_company_id(client, args.company)
            if args.customer_id is not None:
                customer_id = args.customer_id
            else:
                customer_id = resolve_customer_id(client, args.client)
            label = args.client or ("customer-%s" % customer_id)
            summary = run_export(
                client, customer_id=customer_id, client_label=label,
                start_date=start, end_date=end, out_dir=args.out,
                dry_run=args.dry_run, all_fields=args.export_all_fields)
    except (ValueError, LookupError, ApiError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1

    print("Expenses: %d | Receipts: %d | Failures: %d"
          % (summary.expense_count, summary.receipts_downloaded, len(summary.receipt_failures)))
    if summary.expense_count == 0:
        print("0 expenses in range.")
    if args.dry_run:
        print("Dry run — nothing written. CSV would be: %s" % summary.csv_path)
    else:
        print("CSV: %s" % summary.csv_path)
        print("Zip: %s" % (summary.zip_path or "(none — no receipts)"))
    for number, msg in summary.receipt_failures:
        print("  ! receipt failed for %s: %s" % (number, msg), file=sys.stderr)
    return 0


def build_create_parser():
    p = argparse.ArgumentParser(prog="ee create", description="Create one expense in InvoiceShelf.")
    p.add_argument("--company", required=True)
    p.add_argument("--amount", required=True)
    p.add_argument("--currency", required=True, help="Currency code, e.g. USD.")
    p.add_argument("--date", required=True, help="Expense date, DDMMYYYY.")
    p.add_argument("--category", required=True, help="Expense category name.")
    p.add_argument("--notes", "--vendor", dest="notes", default="")
    p.add_argument("--client", "--customer", dest="client", default=None,
                   help="Client/customer name to attribute the expense to (optional).")
    p.add_argument("--expense-number", dest="expense_number", default=None,
                   help="Expense number. If omitted, auto-generated as DDMMYYYY+NN "
                        "(NN = count of expenses on that date + 1, e.g. 0708202601).")
    p.add_argument("--exchange-rate", dest="exchange_rate", default=None,
                   help="Required only if the currency differs from the company default.")
    p.add_argument("--payment-method", dest="payment_method", default=None,
                   help="Payment method name, e.g. 'Credit Card' (optional). Resolved to its "
                        "InvoiceShelf payment_method_id.")
    p.add_argument("--receipt", default=None, help="Path to a receipt file to attach.")
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true")
    return p


def run_create_cli(argv=None):
    args = build_create_parser().parse_args(argv)
    try:
        date = to_api_date(parse_ddmmyyyy(args.date))
        amount_minor = amount_to_minor(args.amount, 2)
        config = load_config(find_config_file(args.config))
        client = InvoiceShelfClient(config)
        client.login()
        client.company_id = resolve_company_id(client, args.company)
        currency_id = resolve_currency_id(client, args.currency)
        category_id = resolve_category_id(client, args.category)
        customer_id = resolve_customer_id(client, args.client) if args.client else None
        payment_method_id = (resolve_payment_method_id(client, args.payment_method)
                             if args.payment_method else None)
        number = args.expense_number or next_expense_number(args.date, client.count_expenses_on_date(date))
        body = build_expense_body(
            expense_date=date, amount_minor=amount_minor, category_id=category_id,
            currency_id=currency_id, notes=args.notes, customer_id=customer_id,
            exchange_rate=args.exchange_rate, expense_number=number,
            payment_method_id=payment_method_id)
        if args.dry_run:
            print("DRY RUN — POST /expenses")
            print(json.dumps(body, indent=2, sort_keys=True))
            if args.receipt:
                print("receipt: %s (multipart field 'attachment_receipt')" % args.receipt)
            return 0
        receipt = None
        if args.receipt:
            with open(args.receipt, "rb") as f:
                receipt = (os.path.basename(args.receipt), f.read(), "application/octet-stream")
        out = client.create_expense(body, receipt=receipt)
        exp = out.get("data") or out
        print("Created expense id: %s" % exp.get("id"))
        return 0
    except (ValueError, LookupError, ApiError, OSError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    sub, rest = split_subcommand(argv)
    if sub == "create":
        return run_create_cli(rest)
    return run_export_cli(rest)


if __name__ == "__main__":
    sys.exit(main())
