"""InvoiceShelf expense exporter — CSV + receipts zip for a client and date range."""
import csv
import datetime
import json
import os
import re
import typing
import urllib.error
import urllib.parse
import urllib.request
import zipfile

CSV_COLUMNS = ["expense_number", "expense_date", "amount", "currency", "notes", "receipt_file"]

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
        raise ValueError("Missing required config: %s" % ", ".join(sorted(missing)))
    company_id = get("INVOICESHELF_COMPANY_ID") or "1"
    return Config(url=resolved["url"], email=resolved["email"],
                  password=resolved["password"], company_id=company_id)


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


def expense_to_row(expense, receipt_file):
    currency = expense.get("currency") or {}
    precision = currency.get("precision", 2)
    if precision in (None, ""):
        precision = 2
    return {
        "expense_number": expense.get("expense_number") or "",
        "expense_date": expense.get("expense_date") or "",
        "amount": scale_amount(expense.get("amount") or 0, precision),
        "currency": currency.get("code") or "",
        "notes": expense.get("notes") or "",
        "receipt_file": receipt_file or "",
    }


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


_TIMEOUT = 60


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class InvoiceShelfClient:
    def __init__(self, config):
        self.config = config
        self.base = config.url.rstrip("/") + "/api/v1"
        self.token = None

    def _request(self, method, path, query=None, body=None, auth=True, raw=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["company"] = str(self.config.company_id)
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

    def find_customers(self, name):
        resp = self._request("GET", "/customers", query={"search": name, "limit": "all"})
        return resp.get("data", [])

    def list_expenses(self, customer_id, from_date, to_date):
        resp = self._request("GET", "/expenses", query={
            "customer_id": customer_id,
            "from_date": from_date,
            "to_date": to_date,
            "limit": "all",
        })
        return resp.get("data", [])

    def download_receipt(self, expense_id):
        return self._request("GET", "/expenses/%s/show/receipt" % expense_id, raw=True)


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


ExportSummary = typing.NamedTuple("ExportSummary", [
    ("expense_count", int), ("receipts_downloaded", int),
    ("receipt_failures", list), ("csv_path", str), ("zip_path", typing.Optional[str]),
])


def run_export(client, *, customer_id, client_label, start_date, end_date, out_dir, dry_run=False):
    expenses = client.list_expenses(
        customer_id, to_api_date(start_date), to_api_date(end_date))
    stem = "%s_%s-%s" % (
        sanitize_filename(client_label),
        to_filename_date(start_date), to_filename_date(end_date))
    csv_path = os.path.join(out_dir, stem + ".csv")
    zip_path = os.path.join(out_dir, stem + "_receipts.zip")

    namer = ZipNamer()
    rows = []
    stored = []  # (name, bytes)
    failures = []
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
        rows.append(expense_to_row(expense, receipt_file))

    downloaded = receipts_available if dry_run else len(stored)

    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
        write_csv(rows, csv_path)
        if stored:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in stored:
                    zf.writestr(name, data)
        else:
            zip_path = None
    else:
        zip_path = zip_path if receipts_available else None

    return ExportSummary(
        expense_count=len(expenses), receipts_downloaded=downloaded,
        receipt_failures=failures, csv_path=csv_path, zip_path=zip_path)
