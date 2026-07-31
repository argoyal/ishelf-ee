# InvoiceShelf Expense Exporter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A zero-dependency Python CLI that exports a client's InvoiceShelf expenses in a date range to CSV and bundles their receipts into a zip.

**Architecture:** A single importable module `export_expenses.py` holding pure helpers (dates, money, filenames, CSV), a thin `InvoiceShelfClient` HTTP wrapper over the InvoiceShelf 2.4.1 REST API, an orchestrator `run_export`, and a `main()` CLI. Pure logic is unit-tested against fixtures; HTTP is tested by mocking `urllib`. No live server needed for tests.

**Tech Stack:** Python 3 standard library only (`urllib`, `json`, `csv`, `zipfile`, `argparse`, `unittest`). No `pip install`.

## Context

The user logs all personal and company expenses (with receipt attachments) in a self-hosted InvoiceShelf 2.4.1 instance and needs a repeatable way to pull bookkeeping exports per client and period. There is no built-in "export expenses + receipts" feature. The confirmed data model: an `Expense` belongs to a `Customer` (the "client"), stores money in minor units, and carries at most one receipt via Spatie MediaLibrary (`receipts` collection). The REST API exposes login, customer search, filtered expense listing, and receipt download — everything needed to do this remotely over HTTPS without touching the server. This plan builds that CLI. Design spec: `docs/superpowers/specs/2026-08-01-invoiceshelf-expense-exporter-design.md` (in the project repo).

## Global Constraints

- **Runtime:** Python **3.8+**, standard library only. No third-party packages. Use `typing.Optional[...]` (not `X | None`) for 3.8 compatibility.
- **Tests:** `unittest`, run via `python3 -m unittest discover -s tests -v`. No test may make a real network call.
- **API base:** all paths are under `<url>/api/v1`. Every authenticated request sends headers `Authorization: Bearer <token>`, `company: <company_id>`, `Accept: application/json`.
- **Login contract:** `POST /api/v1/auth/login` with JSON `{"username": <email>, "password": <pw>, "device_name": "expense-exporter"}` → response `{"type":"Bearer","token":"<plainTextToken>"}`. (Field is `username`, not `email`.)
- **Expense filter:** `GET /api/v1/expenses?customer_id=<id>&from_date=<YYYY-MM-DD>&to_date=<YYYY-MM-DD>&limit=all`. Range inclusive on both ends. Envelope: `{"data":[...],"meta":{...}}`.
- **Customer search:** `GET /api/v1/customers?search=<name>&limit=all` → `{"data":[{"id":..,"name":..,..},..]}`.
- **Receipt download:** `GET /api/v1/expenses/<id>/show/receipt` → raw file bytes (one receipt max).
- **Money:** amounts are minor units (integer). Human amount = `amount / 10**currency.precision`; `precision` from the expense's nested `currency` object, default `2`.
- **Receipt presence:** an expense has a receipt iff `attachment_receipt_meta` is non-null; its `file_name` field is the original filename.
- **CSV columns (exact order):** `expense_number, expense_date, amount, currency, notes, receipt_file`.
- **Dates:** CLI input is `DDMMYYYY`; output filenames reuse `DDMMYYYY`; API uses `YYYY-MM-DD`.
- **Commit** after each task.

## File Structure

- `export_expenses.py` — the whole tool (helpers + client + orchestrator + CLI). One file, per the spec.
- `tests/__init__.py` — empty, makes `tests` a package.
- `tests/test_export_expenses.py` — all unit tests.
- `config.env.example` — sample config (committed); real `config.env` is gitignored.
- `.gitignore` — ignores `config.env`, `exports/`, `__pycache__/`.
- `README.md` — usage.

Module-level constant in `export_expenses.py`:
```python
CSV_COLUMNS = ["expense_number", "expense_date", "amount", "currency", "notes", "receipt_file"]
```

---

### Task 1: Date parsing & formatting

**Files:**
- Create: `export_expenses.py`
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Produces: `parse_ddmmyyyy(s: str) -> datetime.date`; `to_api_date(d: datetime.date) -> str`; `to_filename_date(d: datetime.date) -> str`; `validate_range(start: datetime.date, end: datetime.date) -> None` (raises `ValueError` if `start > end`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_export_expenses.py
import datetime
import unittest
import export_expenses as ee


class DateTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(ee.parse_ddmmyyyy("01042025"), datetime.date(2025, 4, 1))

    def test_parse_bad_length(self):
        with self.assertRaises(ValueError):
            ee.parse_ddmmyyyy("1042025")

    def test_parse_impossible_date(self):
        with self.assertRaises(ValueError):
            ee.parse_ddmmyyyy("31022025")

    def test_to_api_date(self):
        self.assertEqual(ee.to_api_date(datetime.date(2025, 4, 1)), "2025-04-01")

    def test_to_filename_date(self):
        self.assertEqual(ee.to_filename_date(datetime.date(2025, 4, 1)), "01042025")

    def test_validate_range_rejects_reversed(self):
        with self.assertRaises(ValueError):
            ee.validate_range(datetime.date(2025, 4, 2), datetime.date(2025, 4, 1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.DateTests -v`
Expected: FAIL — `AttributeError: module 'export_expenses' has no attribute 'parse_ddmmyyyy'`.

- [ ] **Step 3: Write minimal implementation**

```python
# export_expenses.py
"""InvoiceShelf expense exporter — CSV + receipts zip for a client and date range."""
import datetime

CSV_COLUMNS = ["expense_number", "expense_date", "amount", "currency", "notes", "receipt_file"]


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.DateTests -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/__init__.py tests/test_export_expenses.py
git commit -m "feat: DDMMYYYY date parsing and formatting"
```

---

### Task 2: Money scaling

**Files:**
- Modify: `export_expenses.py`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Produces: `scale_amount(minor, precision=2) -> str` — integer-exact decimal string (no float rounding).

- [ ] **Step 1: Write the failing test**

```python
class MoneyTests(unittest.TestCase):
    def test_two_decimals(self):
        self.assertEqual(ee.scale_amount(123456, 2), "1234.56")

    def test_pads_leading_zeros(self):
        self.assertEqual(ee.scale_amount(5, 2), "0.05")

    def test_zero_precision(self):
        self.assertEqual(ee.scale_amount(100, 0), "100")

    def test_precision_as_string(self):
        self.assertEqual(ee.scale_amount("5000", "2"), "50.00")

    def test_negative(self):
        self.assertEqual(ee.scale_amount(-5, 2), "-0.05")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.MoneyTests -v`
Expected: FAIL — `AttributeError: ... 'scale_amount'`.

- [ ] **Step 3: Write minimal implementation**

```python
def scale_amount(minor, precision=2):
    minor = int(minor)
    precision = int(precision)
    if precision <= 0:
        return str(minor)
    sign = "-" if minor < 0 else ""
    digits = str(abs(minor)).rjust(precision + 1, "0")
    return "%s%s.%s" % (sign, digits[:-precision], digits[-precision:])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.MoneyTests -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/test_export_expenses.py
git commit -m "feat: minor-unit money scaling"
```

---

### Task 3: Filename sanitizing & zip-name collision handling

**Files:**
- Modify: `export_expenses.py`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Produces: `sanitize_filename(name: str) -> str` (strips path separators / unsafe chars → `_`, non-empty fallback `"receipt"`); `ZipNamer` class with `.allocate(desired: str) -> str` returning a collision-free name (numeric suffix before extension).

- [ ] **Step 1: Write the failing test**

```python
class NamingTests(unittest.TestCase):
    def test_sanitize_strips_separators(self):
        self.assertEqual(ee.sanitize_filename("a/b\\c:d.pdf"), "a_b_c_d.pdf")

    def test_sanitize_empty_fallback(self):
        self.assertEqual(ee.sanitize_filename("   "), "receipt")

    def test_zip_namer_dedupes(self):
        namer = ee.ZipNamer()
        self.assertEqual(namer.allocate("r.pdf"), "r.pdf")
        self.assertEqual(namer.allocate("r.pdf"), "r_1.pdf")
        self.assertEqual(namer.allocate("r.pdf"), "r_2.pdf")

    def test_zip_namer_dedupes_no_extension(self):
        namer = ee.ZipNamer()
        self.assertEqual(namer.allocate("receipt"), "receipt")
        self.assertEqual(namer.allocate("receipt"), "receipt_1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.NamingTests -v`
Expected: FAIL — missing `sanitize_filename` / `ZipNamer`.

- [ ] **Step 3: Write minimal implementation**

```python
import os
import re

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.NamingTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/test_export_expenses.py
git commit -m "feat: filename sanitizing and zip collision handling"
```

---

### Task 4: Config loading

**Files:**
- Modify: `export_expenses.py`
- Create: `config.env.example`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Produces: `Config` (NamedTuple: `url, email, password, company_id` — all `str`); `load_config(path: str) -> Config` (parses `KEY=VALUE` lines, ignores blanks/`#`; real env vars override file; `company_id` defaults `"1"`; raises `ValueError` listing any missing required key).

- [ ] **Step 1: Write the failing test**

```python
import os
import tempfile


class ConfigTests(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_loads_and_defaults_company(self):
        path = self._write(
            "INVOICESHELF_URL=https://x.example\n"
            "# comment\n"
            "INVOICESHELF_EMAIL=me@example.com\n"
            "INVOICESHELF_PASSWORD=secret\n"
        )
        cfg = ee.load_config(path)
        self.assertEqual(cfg.url, "https://x.example")
        self.assertEqual(cfg.email, "me@example.com")
        self.assertEqual(cfg.company_id, "1")

    def test_missing_required_raises(self):
        path = self._write("INVOICESHELF_URL=https://x.example\n")
        with self.assertRaises(ValueError):
            ee.load_config(path)

    def test_env_overrides_file(self):
        path = self._write(
            "INVOICESHELF_URL=https://file.example\n"
            "INVOICESHELF_EMAIL=me@example.com\n"
            "INVOICESHELF_PASSWORD=secret\n"
        )
        os.environ["INVOICESHELF_URL"] = "https://env.example"
        self.addCleanup(os.environ.pop, "INVOICESHELF_URL", None)
        self.assertEqual(ee.load_config(path).url, "https://env.example")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.ConfigTests -v`
Expected: FAIL — missing `load_config`.

- [ ] **Step 3: Write minimal implementation**

```python
import typing

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
```

Also create `config.env.example`:
```
INVOICESHELF_URL=https://invoices.example.com
INVOICESHELF_EMAIL=you@example.com
INVOICESHELF_PASSWORD=your-password
INVOICESHELF_COMPANY_ID=1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.ConfigTests -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py config.env.example tests/test_export_expenses.py
git commit -m "feat: config loading from env file with env override"
```

---

### Task 5: Expense→row mapping & CSV writing

**Files:**
- Modify: `export_expenses.py`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: `scale_amount`, `CSV_COLUMNS`.
- Produces: `expense_to_row(expense: dict, receipt_file: str) -> dict` (keys = `CSV_COLUMNS`); `write_csv(rows: list, path: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
import csv as _csv


class RowCsvTests(unittest.TestCase):
    def _expense(self):
        return {
            "expense_number": "EXP-000001",
            "expense_date": "2025-04-15",
            "amount": 123456,
            "notes": "Taxi, airport",
            "currency": {"code": "USD", "precision": 2},
        }

    def test_expense_to_row(self):
        row = ee.expense_to_row(self._expense(), "EXP-000001__taxi.pdf")
        self.assertEqual(row, {
            "expense_number": "EXP-000001",
            "expense_date": "2025-04-15",
            "amount": "1234.56",
            "currency": "USD",
            "notes": "Taxi, airport",
            "receipt_file": "EXP-000001__taxi.pdf",
        })

    def test_expense_to_row_missing_currency_defaults_precision(self):
        exp = self._expense()
        exp.pop("currency")
        exp["amount"] = 5000
        row = ee.expense_to_row(exp, "")
        self.assertEqual(row["amount"], "50.00")
        self.assertEqual(row["currency"], "")
        self.assertEqual(row["receipt_file"], "")

    def test_write_csv_roundtrip(self):
        rows = [ee.expense_to_row(self._expense(), "r.pdf")]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.remove, path)
        ee.write_csv(rows, path)
        with open(path, newline="", encoding="utf-8") as f:
            got = list(_csv.DictReader(f))
        self.assertEqual(list(got[0].keys()), ee.CSV_COLUMNS)
        self.assertEqual(got[0]["amount"], "1234.56")
        self.assertEqual(got[0]["notes"], "Taxi, airport")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.RowCsvTests -v`
Expected: FAIL — missing `expense_to_row` / `write_csv`.

- [ ] **Step 3: Write minimal implementation**

```python
import csv


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.RowCsvTests -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/test_export_expenses.py
git commit -m "feat: expense-to-row mapping and CSV writer"
```

---

### Task 6: HTTP client (`InvoiceShelfClient`)

**Files:**
- Modify: `export_expenses.py`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: `Config`.
- Produces: `ApiError(Exception)` with `.status: Optional[int]`; `InvoiceShelfClient(config)` with `.login() -> str`, `.find_customers(name) -> list`, `.list_expenses(customer_id, from_date, to_date) -> list`, `.download_receipt(expense_id) -> bytes`. Internally routes all traffic through `_request(method, path, query=None, body=None, auth=True, raw=False)`, which tests patch via `urllib.request.urlopen`.

- [ ] **Step 1: Write the failing test**

```python
import io
import json as _json
from unittest import mock


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _fake_json_resp(obj):
    return _FakeResp(_json.dumps(obj).encode("utf-8"))


class ClientTests(unittest.TestCase):
    def _client(self):
        return ee.InvoiceShelfClient(
            ee.Config(url="https://x.example/", email="me@example.com",
                      password="secret", company_id="1"))

    def test_login_sets_token_and_sends_username(self):
        client = self._client()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = _json.loads(req.data.decode("utf-8"))
            return _fake_json_resp({"type": "Bearer", "token": "tok123"})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            token = client.login()

        self.assertEqual(token, "tok123")
        self.assertEqual(client.token, "tok123")
        self.assertTrue(captured["url"].endswith("/api/v1/auth/login"))
        self.assertEqual(captured["body"]["username"], "me@example.com")
        self.assertEqual(captured["body"]["device_name"], "expense-exporter")

    def test_list_expenses_sends_company_header_and_filters(self):
        client = self._client()
        client.token = "tok123"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _fake_json_resp({"data": [{"id": 7}], "meta": {}})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = client.list_expenses(5, "2025-04-01", "2025-06-30")

        self.assertEqual(data, [{"id": 7}])
        self.assertIn("customer_id=5", captured["url"])
        self.assertIn("from_date=2025-04-01", captured["url"])
        self.assertIn("to_date=2025-06-30", captured["url"])
        self.assertIn("limit=all", captured["url"])
        self.assertEqual(captured["headers"]["company"], "1")
        self.assertEqual(captured["headers"]["authorization"], "Bearer tok123")

    def test_download_receipt_returns_bytes(self):
        client = self._client()
        client.token = "tok123"
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _FakeResp(b"PDFBYTES")):
            self.assertEqual(client.download_receipt(9), b"PDFBYTES")

    def test_http_error_becomes_apierror(self):
        import urllib.error
        client = self._client()

        def raise_401(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message":"bad"}'))

        with mock.patch("urllib.request.urlopen", raise_401):
            with self.assertRaises(ee.ApiError) as ctx:
                client.login()
        self.assertEqual(ctx.exception.status, 401)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.ClientTests -v`
Expected: FAIL — missing `InvoiceShelfClient` / `ApiError`.

- [ ] **Step 3: Write minimal implementation**

```python
import json
import urllib.error
import urllib.parse
import urllib.request

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.ClientTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/test_export_expenses.py
git commit -m "feat: InvoiceShelf HTTP client"
```

---

### Task 7: Resolve client name → customer id

**Files:**
- Modify: `export_expenses.py`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: a client exposing `.find_customers(name) -> list`.
- Produces: `resolve_customer_id(client, name: str) -> int`. Logic: exact case-insensitive name match wins if exactly one; else if the search returns exactly one row, use it; on 0 rows raise `LookupError` (message: no match); on ambiguity (multiple, no single exact) raise `LookupError` listing `id — name` and telling the user to rerun with `--customer-id`.

- [ ] **Step 1: Write the failing test**

```python
class _StubClient:
    def __init__(self, customers):
        self._customers = customers

    def find_customers(self, name):
        return self._customers


class ResolveTests(unittest.TestCase):
    def test_single_match(self):
        client = _StubClient([{"id": 12, "name": "Acme Corp"}])
        self.assertEqual(ee.resolve_customer_id(client, "acme"), 12)

    def test_exact_match_wins_over_substring(self):
        client = _StubClient([
            {"id": 1, "name": "Acme"},
            {"id": 2, "name": "Acme Corp"},
        ])
        self.assertEqual(ee.resolve_customer_id(client, "Acme"), 1)

    def test_zero_matches_raises(self):
        with self.assertRaises(LookupError):
            ee.resolve_customer_id(_StubClient([]), "Nobody")

    def test_ambiguous_raises_with_ids(self):
        client = _StubClient([
            {"id": 1, "name": "Acme One"},
            {"id": 2, "name": "Acme Two"},
        ])
        with self.assertRaises(LookupError) as ctx:
            ee.resolve_customer_id(client, "Acme")
        self.assertIn("--customer-id", str(ctx.exception))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.ResolveTests -v`
Expected: FAIL — missing `resolve_customer_id`.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.ResolveTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/test_export_expenses.py
git commit -m "feat: resolve client name to customer id"
```

---

### Task 8: Orchestration (`run_export`)

**Files:**
- Modify: `export_expenses.py`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: a client with `.list_expenses(...)` and `.download_receipt(id) -> bytes`; `to_api_date`, `to_filename_date`, `sanitize_filename`, `ZipNamer`, `expense_to_row`, `write_csv`.
- Produces: `ExportSummary` (NamedTuple: `expense_count:int, receipts_downloaded:int, receipt_failures:list, csv_path:str, zip_path:Optional[str]`); `run_export(client, *, customer_id, client_label, start_date, end_date, out_dir, dry_run=False) -> ExportSummary`.

Behaviour:
- Lists expenses for `[start_date, end_date]` (converted via `to_api_date`).
- CSV filename `<client_label>_<DDMMYYYY-start>-<DDMMYYYY-end>.csv`; zip same stem + `_receipts.zip`.
- For each expense with non-null `attachment_receipt_meta`: planned zip entry = `ZipNamer.allocate("<expense_number>__<meta.file_name>")`. If not `dry_run`, download bytes; on success store bytes under that name and set the row's `receipt_file`; on failure append `(expense_number, error)` to failures and leave `receipt_file` blank.
- Not `dry_run`: ensure `out_dir` exists, `write_csv`, and write the zip only if at least one receipt was stored (else `zip_path=None`).
- `dry_run`: compute counts (receipts_downloaded = count of expenses with a receipt) but write nothing; `csv_path`/`zip_path` still reported as the intended paths.

- [ ] **Step 1: Write the failing test**

```python
import zipfile


class _ExportClient:
    def __init__(self, expenses, receipts=None, fail_ids=()):
        self._expenses = expenses
        self._receipts = receipts or {}
        self._fail_ids = set(fail_ids)

    def list_expenses(self, customer_id, from_date, to_date):
        return self._expenses

    def download_receipt(self, expense_id):
        if expense_id in self._fail_ids:
            raise ee.ApiError("boom", status=500)
        return self._receipts[expense_id]


def _exp(id, number, has_receipt, file_name="r.pdf"):
    e = {"id": id, "expense_number": number, "expense_date": "2025-04-10",
         "amount": 1000, "notes": "n", "currency": {"code": "USD", "precision": 2}}
    e["attachment_receipt_meta"] = {"file_name": file_name} if has_receipt else None
    return e


class RunExportTests(unittest.TestCase):
    def _out(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        return d

    def test_writes_csv_and_zip(self):
        client = _ExportClient(
            [_exp(1, "EXP-1", True), _exp(2, "EXP-2", False)],
            receipts={1: b"BYTES"})
        out = self._out()
        summary = ee.run_export(
            client, customer_id=5, client_label="Acme",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 6, 30),
            out_dir=out)
        self.assertEqual(summary.expense_count, 2)
        self.assertEqual(summary.receipts_downloaded, 1)
        self.assertTrue(summary.csv_path.endswith("Acme_01042025-30062025.csv"))
        self.assertTrue(os.path.exists(summary.zip_path))
        with zipfile.ZipFile(summary.zip_path) as z:
            self.assertEqual(z.namelist(), ["EXP-1__r.pdf"])
        with open(summary.csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        self.assertEqual(rows[0]["receipt_file"], "EXP-1__r.pdf")
        self.assertEqual(rows[1]["receipt_file"], "")

    def test_no_receipts_skips_zip(self):
        client = _ExportClient([_exp(1, "EXP-1", False)])
        summary = ee.run_export(
            client, customer_id=5, client_label="Acme",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 4, 2),
            out_dir=self._out())
        self.assertIsNone(summary.zip_path)
        self.assertTrue(os.path.exists(summary.csv_path))

    def test_download_failure_recorded_not_fatal(self):
        client = _ExportClient([_exp(1, "EXP-1", True)], fail_ids=[1])
        summary = ee.run_export(
            client, customer_id=5, client_label="Acme",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 4, 2),
            out_dir=self._out())
        self.assertEqual(summary.receipts_downloaded, 0)
        self.assertEqual(len(summary.receipt_failures), 1)
        self.assertIsNone(summary.zip_path)

    def test_dry_run_writes_nothing(self):
        client = _ExportClient([_exp(1, "EXP-1", True)], receipts={1: b"BYTES"})
        out = self._out()
        summary = ee.run_export(
            client, customer_id=5, client_label="Acme",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 4, 2),
            out_dir=out, dry_run=True)
        self.assertEqual(summary.expense_count, 1)
        self.assertFalse(os.path.exists(summary.csv_path))
        self.assertEqual(os.listdir(out), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.RunExportTests -v`
Expected: FAIL — missing `run_export` / `ExportSummary`.

- [ ] **Step 3: Write minimal implementation**

```python
import zipfile

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_export_expenses.RunExportTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py tests/test_export_expenses.py
git commit -m "feat: run_export orchestration with receipts zip"
```

---

### Task 9: CLI wiring, README, .gitignore

**Files:**
- Modify: `export_expenses.py`
- Create: `.gitignore`, `README.md`
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `build_arg_parser() -> argparse.ArgumentParser`; `main(argv=None) -> int`. Flags: `--client` (required unless `--customer-id`), `--start`, `--end` (required), `--out` (default `exports`), `--customer-id` (int, optional), `--config` (default `config.env`), `--dry-run`. Returns `0` on success, `1` on any `ValueError` / `LookupError` / `ApiError` (message printed to stderr). Prints a summary on success including failure count.

- [ ] **Step 1: Write the failing test**

```python
class CliTests(unittest.TestCase):
    def test_parser_reads_args(self):
        parser = ee.build_arg_parser()
        args = parser.parse_args(
            ["--client", "Acme", "--start", "01042025", "--end", "30062025", "--dry-run"])
        self.assertEqual(args.client, "Acme")
        self.assertEqual(args.start, "01042025")
        self.assertTrue(args.dry_run)

    def test_main_dry_run_end_to_end(self):
        out = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, out)

        cfg = ee.Config(url="https://x.example", email="me@example.com",
                        password="secret", company_id="1")
        fake = _ExportClient([_exp(1, "EXP-1", True)], receipts={1: b"B"})
        fake.login = lambda: "tok"
        fake.find_customers = lambda name: [{"id": 5, "name": "Acme"}]

        with mock.patch.object(ee, "load_config", lambda path: cfg), \
             mock.patch.object(ee, "InvoiceShelfClient", lambda config: fake):
            rc = ee.main(["--client", "Acme", "--start", "01042025",
                          "--end", "30062025", "--out", out, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(out), [])  # dry-run writes nothing

    def test_main_reports_error_nonzero(self):
        cfg = ee.Config(url="https://x.example", email="m", password="p", company_id="1")
        fake = _ExportClient([])
        fake.login = lambda: "tok"
        fake.find_customers = lambda name: []
        with mock.patch.object(ee, "load_config", lambda path: cfg), \
             mock.patch.object(ee, "InvoiceShelfClient", lambda config: fake):
            rc = ee.main(["--client", "Ghost", "--start", "01042025", "--end", "30062025"])
        self.assertEqual(rc, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_export_expenses.CliTests -v`
Expected: FAIL — missing `build_arg_parser` / `main`.

- [ ] **Step 3: Write minimal implementation**

```python
import argparse
import sys


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Export InvoiceShelf expenses (CSV) and receipts (zip) for a client and date range.")
    parser.add_argument("--client", help="Client (customer) name to export.")
    parser.add_argument("--customer-id", type=int, default=None,
                        help="Use this customer id directly, skipping name lookup.")
    parser.add_argument("--start", required=True, help="Start date, inclusive, DDMMYYYY.")
    parser.add_argument("--end", required=True, help="End date, inclusive, DDMMYYYY.")
    parser.add_argument("--out", default="exports", help="Output directory (default: exports).")
    parser.add_argument("--config", default="config.env", help="Path to config env file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be exported; download and write nothing.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        if not args.client and args.customer_id is None:
            raise ValueError("Provide --client NAME or --customer-id ID.")
        start = parse_ddmmyyyy(args.start)
        end = parse_ddmmyyyy(args.end)
        validate_range(start, end)

        config = load_config(args.config)
        client = InvoiceShelfClient(config)
        client.login()

        if args.customer_id is not None:
            customer_id = args.customer_id
        else:
            customer_id = resolve_customer_id(client, args.client)

        label = args.client or ("customer-%s" % customer_id)
        summary = run_export(
            client, customer_id=customer_id, client_label=label,
            start_date=start, end_date=end, out_dir=args.out, dry_run=args.dry_run)
    except (ValueError, LookupError, ApiError) as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1

    print("Expenses: %d | Receipts: %d | Failures: %d"
          % (summary.expense_count, summary.receipts_downloaded, len(summary.receipt_failures)))
    if args.dry_run:
        print("Dry run — nothing written. CSV would be: %s" % summary.csv_path)
    else:
        print("CSV: %s" % summary.csv_path)
        print("Zip: %s" % (summary.zip_path or "(none — no receipts)"))
    for number, msg in summary.receipt_failures:
        print("  ! receipt failed for %s: %s" % (number, msg), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Create `.gitignore`:
```
config.env
exports/
__pycache__/
*.pyc
```

Create `README.md` (usage, config setup, examples):
```markdown
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
```

- [ ] **Step 4: Run the full test suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (all tests across every class).

- [ ] **Step 5: Commit**

```bash
git add export_expenses.py .gitignore README.md tests/test_export_expenses.py
git commit -m "feat: CLI entry point, README, gitignore"
```

---

## Verification

1. **Unit suite (offline, no network):**
   ```
   python3 -m unittest discover -s tests -v
   ```
   All tests pass; confirms date/money/naming/config/CSV/client/resolve/orchestration/CLI logic.

2. **Live dry-run against the real instance** (create `config.env` first):
   ```
   python3 export_expenses.py --client "<a real client>" --start 01042025 --end 30062025 --dry-run
   ```
   Expect a printed summary: expense count and receipts count, no files written. This exercises real login, `company` header, customer resolution, and the expense filter end-to-end. If the client name is ambiguous, the error lists `id — name` pairs; rerun with `--customer-id`.

3. **Live full export:**
   ```
   python3 export_expenses.py --client "<a real client>" --start 01042025 --end 30062025
   ```
   Confirm `exports/<Client>_01042025-30062025.csv` opens with the six columns and correct amounts, and `exports/<Client>_01042025-30062025_receipts.zip` contains one file per expense that has a receipt, named `<expense_number>__<original>`. Cross-check that each non-empty `receipt_file` cell matches a zip entry.

## Notes / Decisions

- **Single-file module** honors the spec while staying unit-testable (tests import `export_expenses`; `main` runs only under `__main__`).
- **`unittest`, not pytest** — keeps the zero-dependency promise; runs with bare Python.
- **On execution**, copy this plan to `docs/superpowers/plans/2026-08-01-invoiceshelf-expense-exporter.md` in the repo alongside the committed spec (plan mode currently restricts edits to this plan file).
- **Out of scope** (from spec): multiple receipts per expense (API exposes only the first), multi-company batch runs, MCP wrapper.
