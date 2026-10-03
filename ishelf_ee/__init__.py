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
        # InvoiceShelf's API exposes only `formatted_created_at` (the creation date in the company's
        # display format); fall back to a raw `created_at` if a future API/version provides one.
        "created_at": expense.get("formatted_created_at") or expense.get("created_at") or "",
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

    def delete_expenses(self, ids):
        # InvoiceShelf deletes expenses via POST /expenses/delete with an ids array
        # (the canonical, tested endpoint; the RESTful destroy route also exists).
        return self._request("POST", "/expenses/delete", body={"ids": list(ids)})

    def list_invoices(self, customer_id=None, from_date=None, to_date=None):
        return self._list("/invoices", customer_id, from_date, to_date)

    def create_invoice(self, body):
        return self._request("POST", "/invoices", body=body)

    def delete_invoices(self, ids):
        # The server refuses to delete an invoice that still has payments.
        return self._request("POST", "/invoices/delete", body={"ids": list(ids)})

    def list_payments(self, customer_id=None, from_date=None, to_date=None):
        return self._list("/payments", customer_id, from_date, to_date)

    def create_payment(self, body):
        # InvoiceShelf 2.4.1 payments take no file attachment (no field, no upload route).
        return self._request("POST", "/payments", body=body)

    def delete_payments(self, ids):
        # Deleting a payment adds its amount back to the invoice's due amount.
        return self._request("POST", "/payments/delete", body={"ids": list(ids)})

    def _list(self, path, customer_id, from_date, to_date):
        query = {"limit": "all"}
        if customer_id is not None:
            query["customer_id"] = customer_id
        if from_date:
            query["from_date"] = from_date
        if to_date:
            query["to_date"] = to_date
        return _as_list(self._request("GET", path, query=query))

    def next_number(self, key, customer_id=None):
        """The server's next invoice/payment number, in the company's configured format."""
        query = {"key": key}
        if customer_id is not None:
            query["userId"] = customer_id
        resp = self._request("GET", "/next-number", query=query) or {}
        if not resp.get("success") or not resp.get("nextNumber"):
            raise ApiError("Could not get the next %s number: %s" % (key, resp.get("message") or resp))
        return resp["nextNumber"]

    def get_company_currency_id(self):
        resp = self._request("GET", "/company/settings", query=[("settings[]", "currency")])
        return int(resp["currency"])

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
    return int(resolve_customer(client, name)["id"])


def resolve_customer(client, name):
    """Like resolve_customer_id, but returns the whole customer record (needed for
    its currency_id when billing)."""
    customers = client.find_customers(name)
    if not customers:
        raise LookupError("No customer found matching %r." % name)
    exact = [c for c in customers if (c.get("name") or "").lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) == 0 and len(customers) == 1:
        return customers[0]
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


def resolve_expense_by_number(client, number):
    """Find the single company-scoped expense whose expense_number matches. Raises
    LookupError if none or more than one match (delete is destructive — never guess)."""
    target = str(number)
    matches = [e for e in client.list_expenses() if str(e.get("expense_number") or "") == target]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError("No expense with number %r under this company." % number)
    listing = "\n".join("  id=%s date=%s amount=%s" % (
        e.get("id"), e.get("expense_date"), e.get("amount")) for e in matches)
    raise LookupError("Multiple expenses match number %r:\n%s" % (number, listing))


def _resolve_by_number(items, field, number, kind):
    """Find the one record whose `field` equals number exactly. Raises LookupError
    on none or several (delete is destructive — never guess)."""
    target = str(number)
    matches = [i for i in items if str(i.get(field) or "") == target]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError("No %s with number %r under this company." % (kind, number))
    listing = "\n".join("  id=%s" % i.get("id") for i in matches)
    raise LookupError("Multiple %ss match number %r:\n%s" % (kind, number, listing))


def resolve_invoice_by_number(client, number):
    return _resolve_by_number(client.list_invoices(), "invoice_number", number, "invoice")


def resolve_payment_by_number(client, number):
    return _resolve_by_number(client.list_payments(), "payment_number", number, "payment")


def parse_item(spec, precision=2):
    """Parse an --item value 'NAME=PRICE' or 'NAME=PRICE@QTY' (PRICE per unit, QTY
    defaults to 1). The name may itself contain '='; the last one splits."""
    name, sep, rest = (spec or "").rpartition("=")
    name = name.strip()
    if not sep or not name:
        raise ValueError("--item must look like 'NAME=PRICE' or 'NAME=PRICE@QTY', got %r" % spec)
    price, _, qty = rest.partition("@")
    price_minor = amount_to_minor(price.strip(), precision)
    if price_minor <= 0:
        raise ValueError("--item price must be positive, got %r" % spec)
    qty = qty.strip() or "1"
    try:
        if decimal.Decimal(qty) <= 0:
            raise ValueError
    except (decimal.InvalidOperation, ValueError):
        raise ValueError("--item quantity must be a positive number, got %r" % spec)
    return {"name": name, "price_minor": price_minor, "quantity": qty}


def _item_total(item):
    total = decimal.Decimal(item["price_minor"]) * decimal.Decimal(item["quantity"])
    return int(total.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))


def resolve_exchange_rate(*, foreign, amount_minor, exchange_rate=None, inr_amount=None,
                          precision=2):
    """The exchange_rate to send, or None when the customer is billed in the company's
    own currency. For a foreign-currency customer, --inr-amount (what the bank actually
    credited, e.g. from the eFIRC) is turned into a rate so the INR value InvoiceShelf
    stores matches the bank."""
    if not foreign:
        if exchange_rate is not None or inr_amount is not None:
            raise ValueError("This customer is billed in the company currency; "
                             "drop --exchange-rate/--inr-amount.")
        return None
    if exchange_rate is not None and inr_amount is not None:
        raise ValueError("Pass only one of --exchange-rate or --inr-amount.")
    if exchange_rate is not None:
        try:
            rate = decimal.Decimal(str(exchange_rate))
        except decimal.InvalidOperation:
            raise ValueError("--exchange-rate must be a number, got %r" % (exchange_rate,))
        if rate <= 0:
            raise ValueError("--exchange-rate must be positive, got %r" % (exchange_rate,))
        return str(exchange_rate)
    if inr_amount is not None:
        inr_minor = amount_to_minor(inr_amount, precision)
        if inr_minor <= 0 or amount_minor <= 0:
            raise ValueError("--inr-amount and the amount must both be positive.")
        # exchange_rate is decimal(19,6) in InvoiceShelf.
        rate = (decimal.Decimal(inr_minor) / decimal.Decimal(amount_minor)).quantize(
            decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP)
        return str(rate)
    raise ValueError("This customer is billed in a foreign currency. Pass --inr-amount "
                     "(the INR the bank credited, e.g. from the eFIRC) or --exchange-rate.")


def base_minor(amount_minor, exchange_rate):
    """The company-currency value InvoiceShelf will store (amount * exchange_rate)."""
    if exchange_rate is None:
        return int(amount_minor)
    value = decimal.Decimal(int(amount_minor)) * decimal.Decimal(str(exchange_rate))
    return int(value.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))


def build_invoice_body(*, invoice_date, due_date, customer_id, invoice_number, currency_id,
                       exchange_rate, items, notes, template_name):
    # No discounts or taxes. The server recomputes totals from the items anyway;
    # we send matching ones because the request requires them.
    lines = []
    for item in items:
        total = _item_total(item)
        lines.append({
            "name": item["name"],
            "description": None,
            "quantity": item["quantity"],
            "price": item["price_minor"],
            "discount_type": "fixed",
            "discount": 0,
            "discount_val": 0,
            "tax": 0,
            "total": total,
        })
    sub_total = sum(line["total"] for line in lines)
    body = {
        "invoice_date": invoice_date,
        "customer_id": customer_id,
        "invoice_number": invoice_number,
        "currency_id": currency_id,
        "discount_type": "fixed",
        "discount": 0,
        "discount_val": 0,
        "sub_total": sub_total,
        "tax": 0,
        "total": sub_total,
        "template_name": template_name,
        "notes": notes or "",
        "items": lines,
        "taxes": [],
    }
    if due_date:
        body["due_date"] = due_date
    if exchange_rate is not None:
        body["exchange_rate"] = exchange_rate
    return body


def build_payment_body(*, payment_date, customer_id, payment_number, amount_minor, currency_id,
                       invoice_id, exchange_rate, payment_method_id, notes):
    body = {
        "payment_date": payment_date,
        "customer_id": customer_id,
        "payment_number": payment_number,
        "amount": int(amount_minor),
        "currency_id": currency_id,
        "notes": notes or "",
    }
    if invoice_id is not None:
        body["invoice_id"] = invoice_id
    if exchange_rate is not None:
        body["exchange_rate"] = exchange_rate
    if payment_method_id is not None:
        body["payment_method_id"] = payment_method_id
    return body


INVOICE_COLUMNS = ["invoice_number", "invoice_date", "due_date", "customer", "currency", "total",
                   "due_amount", "exchange_rate", "base_total", "status", "paid_status", "notes",
                   "invoice_id", "created_at"]
PAYMENT_COLUMNS = ["payment_number", "payment_date", "customer", "invoice_number", "currency",
                   "amount", "exchange_rate", "base_amount", "payment_method", "notes",
                   "payment_id", "created_at"]
INVOICE_LIST_COLUMNS = ["invoice_number", "invoice_date", "customer", "currency", "total",
                        "due_amount", "base_total", "paid_status"]
PAYMENT_LIST_COLUMNS = ["payment_number", "payment_date", "customer", "invoice_number",
                        "currency", "amount", "base_amount", "payment_method"]


def _nested_name(record, key, field="name"):
    obj = record.get(key)
    return (obj.get(field) or "") if isinstance(obj, dict) else ""


def _precision(record):
    currency = record.get("currency") or {}
    precision = currency.get("precision", 2)
    return 2 if precision in (None, "") else precision


def _scaled(value, precision=2):
    return "" if value in (None, "") else scale_amount(value, precision)


def invoice_to_row(inv):
    p = _precision(inv)
    return {
        "invoice_number": inv.get("invoice_number") or "",
        "invoice_date": inv.get("invoice_date") or "",
        "due_date": inv.get("due_date") or "",
        "customer": _nested_name(inv, "customer") or str(inv.get("customer_id") or ""),
        "currency": _nested_name(inv, "currency", "code"),
        "total": _scaled(inv.get("total"), p),
        "due_amount": _scaled(inv.get("due_amount"), p),
        "exchange_rate": inv.get("exchange_rate") or "",
        # base_* values are in the company currency (2 decimals for INR).
        "base_total": _scaled(inv.get("base_total")),
        "status": inv.get("status") or "",
        "paid_status": inv.get("paid_status") or "",
        "notes": inv.get("notes") or "",
        "invoice_id": inv.get("id") or "",
        "created_at": inv.get("formatted_created_at") or inv.get("created_at") or "",
    }


def payment_to_row(pay):
    p = _precision(pay)
    return {
        "payment_number": pay.get("payment_number") or "",
        "payment_date": pay.get("payment_date") or "",
        "customer": _nested_name(pay, "customer") or pay.get("name") or "",
        "invoice_number": _nested_name(pay, "invoice", "invoice_number") or pay.get("invoice_number") or "",
        "currency": _nested_name(pay, "currency", "code"),
        "amount": _scaled(pay.get("amount"), p),
        "exchange_rate": pay.get("exchange_rate") or "",
        "base_amount": _scaled(pay.get("base_amount")),
        "payment_method": _nested_name(pay, "payment_method") or pay.get("payment_mode") or "",
        "notes": pay.get("notes") or "",
        "payment_id": pay.get("id") or "",
        "created_at": pay.get("formatted_created_at") or pay.get("created_at") or "",
    }


def format_table(rows, columns):
    widths = {c: max([len(c)] + [len(str(r.get(c, ""))) for r in rows]) for c in columns}
    lines = ["  ".join(c.ljust(widths[c]) for c in columns)]
    for r in rows:
        lines.append("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(line.rstrip() for line in lines)


SUBCOMMANDS = ("export", "create", "delete", "invoice", "payment")


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


def build_delete_parser():
    p = argparse.ArgumentParser(
        prog="ee delete", description="Delete one expense in InvoiceShelf by its expense number.")
    p.add_argument("--company", required=True,
                   help="Company the expense belongs to (expense numbers are company-scoped).")
    p.add_argument("--expense-number", dest="expense_number", required=True,
                   help="The expense_number to delete (e.g. 1509202603).")
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="Show the expense that would be deleted; delete nothing.")
    return p


def run_delete_cli(argv=None):
    args = build_delete_parser().parse_args(argv)
    try:
        config = load_config(find_config_file(args.config))
        client = InvoiceShelfClient(config)
        client.login()
        client.company_id = resolve_company_id(client, args.company)
        exp = resolve_expense_by_number(client, args.expense_number)
        eid = exp.get("id")
        if args.dry_run:
            print("DRY RUN — would delete expense number %s (id %s, date %s, amount %s)" % (
                args.expense_number, eid, exp.get("expense_date"), exp.get("amount")))
            return 0
        client.delete_expenses([eid])
        print("Deleted expense number %s (id %s)" % (args.expense_number, eid))
        return 0
    except (ValueError, LookupError, ApiError, OSError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1


def _connect(args):
    """Log in and scope the client to --company."""
    config = load_config(find_config_file(args.config))
    client = InvoiceShelfClient(config)
    client.login()
    client.company_id = resolve_company_id(client, args.company)
    return client


def _customer_currency_code(client, customer):
    code = _nested_name(customer, "currency", "code")
    if code:
        return code
    wanted = int(customer["currency_id"])
    for cur in client.list_currencies():
        if int(cur.get("id")) == wanted:
            return cur.get("code") or ""
    return ""


def _print_base(label, amount_minor, rate):
    if rate is not None:
        print("%s %s x rate %s = %s in company currency" % (
            label, scale_amount(amount_minor), rate, scale_amount(base_minor(amount_minor, rate))))


def _add_rate_flags(p):
    p.add_argument("--inr-amount", dest="inr_amount", default=None,
                   help="Foreign-currency customers only: the INR amount the bank credited "
                        "(e.g. from the eFIRC). The exchange rate is worked out from it.")
    p.add_argument("--exchange-rate", dest="exchange_rate", default=None,
                   help="Foreign-currency customers only: the rate to use instead of --inr-amount.")


def build_invoice_create_parser():
    p = argparse.ArgumentParser(prog="ee invoice create", description="Create one invoice in InvoiceShelf.")
    p.add_argument("--company", required=True)
    p.add_argument("--client", "--customer", dest="client", required=True,
                   help="Customer to bill. The invoice is always in this customer's currency.")
    p.add_argument("--date", required=True, help="Invoice date, DDMMYYYY.")
    p.add_argument("--due-date", dest="due_date", default=None, help="Due date, DDMMYYYY (optional).")
    p.add_argument("--item", action="append", required=True,
                   help="Line item 'NAME=PRICE' or 'NAME=PRICE@QTY' (PRICE per unit). Repeatable.")
    p.add_argument("--currency", default=None,
                   help="Optional safety check: fail unless the customer's currency is this code.")
    _add_rate_flags(p)
    p.add_argument("--invoice-number", dest="invoice_number", default=None,
                   help="Invoice number. If omitted, InvoiceShelf's next number is used.")
    p.add_argument("--notes", default="")
    p.add_argument("--template", default="invoice1", help="Invoice PDF template (default invoice1).")
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true")
    return p


def run_invoice_create_cli(argv=None):
    args = build_invoice_create_parser().parse_args(argv)
    try:
        date = to_api_date(parse_ddmmyyyy(args.date))
        due = to_api_date(parse_ddmmyyyy(args.due_date)) if args.due_date else None
        items = [parse_item(spec) for spec in args.item]
        total = sum(_item_total(i) for i in items)
        client = _connect(args)
        customer = resolve_customer(client, args.client)
        currency_id = int(customer["currency_id"])
        if args.currency:
            code = _customer_currency_code(client, customer)
            if code.upper() != args.currency.upper():
                raise ValueError("%s is billed in %s, not %s (InvoiceShelf always invoices in the "
                                 "customer's currency)." % (customer.get("name"), code, args.currency))
        rate = resolve_exchange_rate(
            foreign=currency_id != client.get_company_currency_id(), amount_minor=total,
            exchange_rate=args.exchange_rate, inr_amount=args.inr_amount)
        number = args.invoice_number or client.next_number("invoice", customer["id"])
        body = build_invoice_body(
            invoice_date=date, due_date=due, customer_id=int(customer["id"]),
            invoice_number=number, currency_id=currency_id, exchange_rate=rate, items=items,
            notes=args.notes, template_name=args.template)
        if args.dry_run:
            print("DRY RUN — POST /invoices")
            print(json.dumps(body, indent=2, sort_keys=True))
            _print_base("Total", total, rate)
            return 0
        out = client.create_invoice(body)
        inv = out.get("data") or out
        print("Created invoice %s (id %s)" % (inv.get("invoice_number") or number, inv.get("id")))
        _print_base("Total", total, rate)
        return 0
    except (ValueError, LookupError, ApiError, OSError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1


def build_payment_create_parser():
    p = argparse.ArgumentParser(
        prog="ee payment create",
        description="Record one payment received, optionally against an invoice.")
    p.add_argument("--company", required=True)
    p.add_argument("--invoice", default=None,
                   help="Invoice number the payment settles (customer and currency come from it).")
    p.add_argument("--client", "--customer", dest="client", default=None,
                   help="Customer who paid. Required without --invoice; checked against it with one.")
    p.add_argument("--amount", required=True,
                   help="Amount in the customer's currency (e.g. 3781 for a USD customer).")
    p.add_argument("--date", required=True, help="Payment date (bank credit date), DDMMYYYY.")
    _add_rate_flags(p)
    p.add_argument("--payment-method", dest="payment_method", default=None,
                   help="Payment method name, e.g. 'Bank Transfer' (optional).")
    p.add_argument("--payment-number", dest="payment_number", default=None,
                   help="Payment number. If omitted, InvoiceShelf's next number is used.")
    p.add_argument("--notes", default="",
                   help="Notes, e.g. the eFIRC number (payments cannot carry attachments).")
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true")
    return p


def run_payment_create_cli(argv=None):
    args = build_payment_create_parser().parse_args(argv)
    try:
        if not args.invoice and not args.client:
            raise ValueError("Provide --invoice NUMBER or --client NAME.")
        date = to_api_date(parse_ddmmyyyy(args.date))
        amount_minor = amount_to_minor(args.amount, 2)
        if amount_minor <= 0:
            raise ValueError("--amount must be positive.")
        client = _connect(args)
        invoice = None
        if args.invoice:
            invoice = resolve_invoice_by_number(client, args.invoice)
            customer_id = int(invoice["customer_id"])
            currency_id = int(invoice["currency_id"])
            if args.client and int(resolve_customer(client, args.client)["id"]) != customer_id:
                raise ValueError("Invoice %s belongs to a different customer than %r."
                                 % (args.invoice, args.client))
            due = int(invoice.get("due_amount") or 0)
            if amount_minor > due:
                raise ValueError("Payment %s exceeds invoice %s's due amount %s."
                                 % (scale_amount(amount_minor), args.invoice, scale_amount(due)))
        else:
            customer = resolve_customer(client, args.client)
            customer_id = int(customer["id"])
            currency_id = int(customer["currency_id"])
        rate = resolve_exchange_rate(
            foreign=currency_id != client.get_company_currency_id(), amount_minor=amount_minor,
            exchange_rate=args.exchange_rate, inr_amount=args.inr_amount)
        payment_method_id = (resolve_payment_method_id(client, args.payment_method)
                             if args.payment_method else None)
        number = args.payment_number or client.next_number("payment", customer_id)
        body = build_payment_body(
            payment_date=date, customer_id=customer_id, payment_number=number,
            amount_minor=amount_minor, currency_id=currency_id,
            invoice_id=int(invoice["id"]) if invoice else None, exchange_rate=rate,
            payment_method_id=payment_method_id, notes=args.notes)
        if args.dry_run:
            print("DRY RUN — POST /payments")
            print(json.dumps(body, indent=2, sort_keys=True))
            _print_base("Amount", amount_minor, rate)
            return 0
        out = client.create_payment(body)
        pay = out.get("data") or out
        print("Recorded payment %s (id %s)" % (pay.get("payment_number") or number, pay.get("id")))
        _print_base("Amount", amount_minor, rate)
        return 0
    except (ValueError, LookupError, ApiError, OSError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1


# Per-document settings shared by `ee invoice …` and `ee payment …` list/export/delete.
_DOCS = {
    "invoice": {"plural": "invoices", "list": "list_invoices", "delete": "delete_invoices",
                "resolve": resolve_invoice_by_number, "to_row": invoice_to_row,
                "columns": INVOICE_COLUMNS, "list_columns": INVOICE_LIST_COLUMNS,
                "number": "invoice_number", "date": "invoice_date", "amount": "total"},
    "payment": {"plural": "payments", "list": "list_payments", "delete": "delete_payments",
                "resolve": resolve_payment_by_number, "to_row": payment_to_row,
                "columns": PAYMENT_COLUMNS, "list_columns": PAYMENT_LIST_COLUMNS,
                "number": "payment_number", "date": "payment_date", "amount": "amount"},
}


def build_doc_query_parser(doc, verb):
    p = argparse.ArgumentParser(prog="ee %s %s" % (doc, verb),
                                description="%s %s from InvoiceShelf." % (verb.title(), _DOCS[doc]["plural"]))
    p.add_argument("--company", required=True)
    p.add_argument("--client", "--customer", dest="client", default=None)
    p.add_argument("--start", default=None, help="Start date, inclusive, DDMMYYYY (needs --end).")
    p.add_argument("--end", default=None, help="End date, inclusive, DDMMYYYY (needs --start).")
    if verb == "export":
        p.add_argument("--out", default="exports", help="Output directory (default: exports).")
        p.add_argument("--dry-run", action="store_true")
    p.add_argument("--config", default=None)
    return p


def run_doc_query_cli(doc, verb, argv=None):
    spec = _DOCS[doc]
    args = build_doc_query_parser(doc, verb).parse_args(argv)
    try:
        if bool(args.start) != bool(args.end):
            raise ValueError("Pass both --start and --end, or neither (all time).")
        start = end = None
        if args.start:
            start, end = parse_ddmmyyyy(args.start), parse_ddmmyyyy(args.end)
            validate_range(start, end)
        client = _connect(args)
        customer_id = int(resolve_customer(client, args.client)["id"]) if args.client else None
        records = getattr(client, spec["list"])(
            customer_id=customer_id, from_date=to_api_date(start) if start else None,
            to_date=to_api_date(end) if end else None)
        rows = [spec["to_row"](r) for r in records]
    except (ValueError, LookupError, ApiError, OSError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1
    if verb == "list":
        print(format_table(rows, spec["list_columns"]))
        print("%d %s" % (len(rows), spec["plural"]))
        return 0
    span = ("%s-%s" % (to_filename_date(start), to_filename_date(end))) if start else "all-time"
    path = os.path.join(args.out, "%s_%s_%s.csv" % (
        spec["plural"], sanitize_filename(args.client or args.company), span))
    print("%s: %d" % (spec["plural"].title(), len(rows)))
    if args.dry_run:
        print("Dry run — nothing written. CSV would be: %s" % path)
        return 0
    os.makedirs(args.out, exist_ok=True)
    write_csv(rows, path, columns=spec["columns"])
    print("CSV: %s" % path)
    return 0


def build_doc_delete_parser(doc):
    flag = "--%s-number" % doc
    p = argparse.ArgumentParser(prog="ee %s delete" % doc,
                                description="Delete one %s in InvoiceShelf by its number." % doc)
    p.add_argument("--company", required=True)
    p.add_argument(flag, dest="number", required=True)
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="Show the %s that would be deleted; delete nothing." % doc)
    return p


def run_doc_delete_cli(doc, argv=None):
    spec = _DOCS[doc]
    args = build_doc_delete_parser(doc).parse_args(argv)
    try:
        client = _connect(args)
        rec = spec["resolve"](client, args.number)
        row = spec["to_row"](rec)
        summary = "%s %s (id %s, date %s, amount %s %s)" % (
            doc, args.number, rec.get("id"), row[spec["date"]], row[spec["amount"]], row["currency"])
        if args.dry_run:
            print("DRY RUN — would delete %s" % summary)
            return 0
        getattr(client, spec["delete"])([rec.get("id")])
        print("Deleted %s" % summary)
        return 0
    except (ValueError, LookupError, ApiError, OSError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1


DOC_VERBS = ("create", "list", "export", "delete")


def run_doc_cli(doc, argv):
    argv = list(argv or [])
    if not argv or argv[0] not in DOC_VERBS:
        print("usage: ee %s {%s} ..." % (doc, ",".join(DOC_VERBS)), file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    if verb == "create":
        return run_invoice_create_cli(rest) if doc == "invoice" else run_payment_create_cli(rest)
    if verb == "delete":
        return run_doc_delete_cli(doc, rest)
    return run_doc_query_cli(doc, verb, rest)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    sub, rest = split_subcommand(argv)
    if sub in ("invoice", "payment"):
        return run_doc_cli(sub, rest)
    if sub == "create":
        return run_create_cli(rest)
    if sub == "delete":
        return run_delete_cli(rest)
    return run_export_cli(rest)


if __name__ == "__main__":
    sys.exit(main())
