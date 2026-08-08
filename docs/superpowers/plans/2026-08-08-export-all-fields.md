# `ee` Full-Corpus Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing `ee` export so one command can produce the full historical-expense corpus (all fields + all companies/clients + attachments), while leaving current behavior byte-for-byte unchanged when the new options are absent.

**Architecture:** Two independent, composable additions to `ishelf_ee/__init__.py`: a `--export-all-fields` flag that widens the CSV columns, and `all` sentinels for `--company`/`--client` that widen the scope. Shared per-expense collection logic is factored into helpers so single-client and corpus paths don't duplicate the receipt/zip loop.

**Tech Stack:** Python 3 stdlib only (`argparse`, `csv`, `zipfile`, `urllib`); `unittest` with mocked `urllib.request.urlopen` / stub clients. No third-party dependencies.

## Global Constraints

- **Stdlib-only Python** — no third-party dependencies (this is why output stays CSV, not `.xlsx`).
- **TDD** — tests mock `urllib.request.urlopen` or use stub client objects; no live network calls in the suite.
- **Backward compatibility is a hard requirement** — absent the new options, output is byte-for-byte identical to today (same 6 columns, same order, same filenames).
- **Commits are allowed** in this repo (unlike the brain repo) — normal per-task commits on branch `feat/export-all-fields`.
- Existing columns (verbatim, order matters): `expense_number, expense_date, amount, currency, notes, receipt_file`.
- Appended columns for all-fields mode (verbatim, in order): `expense_id, company, client, category, exchange_rate, created_at`.
- Run the suite with: `python3 -m pytest tests/ -q` (or `python3 -m unittest discover tests -v`).

---

## File Structure

| Path | Change | Responsibility |
|---|---|---|
| `ishelf_ee/__init__.py` | Modify | Row builder, CSV writer, `list_expenses`, export orchestration, CLI |
| `tests/test_export_expenses.py` | Modify | Unit tests for row/csv/list_expenses/run_export/corpus/CLI |
| `docs/superpowers/notes/invoiceshelf-list-expenses.md` | Create | Pinned `GET /expenses` response shape note |
| `README.md` | Modify | Document `--export-all-fields` + `all` sentinels |

Task order: **1** (row+csv widening) → **2** (`list_expenses` optional params) → **3** (refactor helpers + `run_export` all-fields) → **4** (`run_corpus_export`) → **5** (CLI wiring) → **6** (docs). Each ends with an independently testable deliverable.

---

### Task 1: Widen the row builder and CSV writer

**Files:**
- Modify: `ishelf_ee/__init__.py` (add `CSV_COLUMNS_ALL`; extend `expense_to_row`; add `columns` param to `write_csv`)
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Produces:
  - `CSV_COLUMNS_ALL = CSV_COLUMNS + ["expense_id", "company", "client", "category", "exchange_rate", "created_at"]`
  - `expense_to_row(expense, receipt_file, *, all_fields=False, category_map=None, company_name=None) -> dict`
  - `write_csv(rows, path, columns=CSV_COLUMNS)` (columns param added; default preserves old behavior)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_export_expenses.py`, inside `class RowCsvTests` (extend the existing class):

```python
    def _expense_full(self):
        return {
            "id": 42,
            "expense_number": "EXP-000001",
            "expense_date": "2025-04-15",
            "amount": 123456,
            "notes": "Taxi, airport",
            "currency": {"code": "USD", "precision": 2},
            "expense_category_id": 8,
            "customer": {"id": 5, "name": "Ascendra Ventures"},
            "exchange_rate": "1",
            "created_at": "2025-04-15T10:00:00Z",
        }

    def test_default_row_unchanged_when_not_all_fields(self):
        row = ee.expense_to_row(self._expense(), "r.pdf")
        self.assertEqual(list(row.keys()), ee.CSV_COLUMNS)

    def test_all_fields_row_resolves_names(self):
        row = ee.expense_to_row(
            self._expense_full(), "EXP-000001__taxi.pdf",
            all_fields=True, category_map={8: "Food"}, company_name="Arpit Goyal")
        self.assertEqual(list(row.keys()), ee.CSV_COLUMNS_ALL)
        self.assertEqual(row["expense_id"], 42)
        self.assertEqual(row["company"], "Arpit Goyal")
        self.assertEqual(row["client"], "Ascendra Ventures")
        self.assertEqual(row["category"], "Food")
        self.assertEqual(row["exchange_rate"], "1")
        self.assertEqual(row["created_at"], "2025-04-15T10:00:00Z")

    def test_all_fields_category_falls_back_to_nested_object(self):
        exp = self._expense_full()
        del exp["expense_category_id"]
        exp["category"] = {"id": 8, "name": "Fuel"}
        row = ee.expense_to_row(exp, "", all_fields=True, category_map={}, company_name="X")
        self.assertEqual(row["category"], "Fuel")

    def test_all_fields_client_falls_back_to_customer_id(self):
        exp = self._expense_full()
        del exp["customer"]
        exp["customer_id"] = 77
        row = ee.expense_to_row(exp, "", all_fields=True, category_map={8: "Food"}, company_name="X")
        self.assertEqual(row["client"], "77")

    def test_write_csv_all_columns(self):
        rows = [ee.expense_to_row(self._expense_full(), "r.pdf",
                                  all_fields=True, category_map={8: "Food"}, company_name="Arpit Goyal")]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.remove, path)
        ee.write_csv(rows, path, columns=ee.CSV_COLUMNS_ALL)
        with open(path, newline="", encoding="utf-8") as f:
            got = list(_csv.DictReader(f))
        self.assertEqual(list(got[0].keys()), ee.CSV_COLUMNS_ALL)
        self.assertEqual(got[0]["category"], "Food")
        self.assertEqual(got[0]["company"], "Arpit Goyal")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_export_expenses.py::RowCsvTests -q`
Expected: FAIL (`AttributeError: module 'ishelf_ee' has no attribute 'CSV_COLUMNS_ALL'`, and `expense_to_row() got an unexpected keyword argument 'all_fields'`).

- [ ] **Step 3: Implement the widening**

In `ishelf_ee/__init__.py`, just after the existing `CSV_COLUMNS = [...]` line, add:

```python
CSV_COLUMNS_ALL = CSV_COLUMNS + ["expense_id", "company", "client", "category", "exchange_rate", "created_at"]
```

Replace the existing `expense_to_row` function with:

```python
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
```

Replace the existing `write_csv` with:

```python
def write_csv(rows, path, columns=CSV_COLUMNS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_export_expenses.py::RowCsvTests -q`
Expected: PASS (all RowCsvTests, including the pre-existing `test_expense_to_row` and `test_write_csv_roundtrip`, still green — the defaults are unchanged).

- [ ] **Step 5: Commit**

```bash
git add ishelf_ee/__init__.py tests/test_export_expenses.py
git commit -m "feat(export): widen expense_to_row + write_csv for all-fields mode"
```

---

### Task 2: Make `list_expenses` params optional

**Files:**
- Modify: `ishelf_ee/__init__.py` (`InvoiceShelfClient.list_expenses`)
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Produces: `InvoiceShelfClient.list_expenses(self, customer_id=None, from_date=None, to_date=None) -> list` — omits any query param that is `None`; always sends `limit=all`. Positional calls `list_expenses(5, "a", "b")` still work.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_export_expenses.py`, inside `class ClientTests`:

```python
    def test_list_expenses_omits_customer_when_none(self):
        client = self._client()
        client.token = "tok123"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _fake_json_resp({"data": [{"id": 1}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = client.list_expenses()  # no filters → whole company

        self.assertEqual(data, [{"id": 1}])
        self.assertNotIn("customer_id", captured["url"])
        self.assertNotIn("from_date", captured["url"])
        self.assertIn("limit=all", captured["url"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_export_expenses.py::ClientTests::test_list_expenses_omits_customer_when_none -q`
Expected: FAIL (`TypeError: list_expenses() missing 1 required positional argument: 'customer_id'`).

- [ ] **Step 3: Implement optional params**

In `ishelf_ee/__init__.py`, replace the existing `list_expenses` method with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_export_expenses.py::ClientTests -q`
Expected: PASS — the new test plus the pre-existing `test_list_expenses_sends_company_header_and_filters` (positional call still sends all three filters).

- [ ] **Step 5: Commit**

```bash
git add ishelf_ee/__init__.py tests/test_export_expenses.py
git commit -m "feat(client): list_expenses accepts optional customer_id/date filters"
```

---

### Task 3: Factor shared helpers + `run_export` all-fields support

**Files:**
- Modify: `ishelf_ee/__init__.py` (extract `_category_map`, `_collect_rows`, `_write_export`; rewrite `run_export` to use them + accept `all_fields`)
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: `expense_to_row(..., all_fields, category_map, company_name)` (Task 1), `write_csv(rows, path, columns)` (Task 1).
- Produces:
  - `_category_map(client) -> dict` — `{category_id: name}` from `client.list_categories()`.
  - `_collect_rows(client, expenses, namer, *, all_fields=False, category_map=None, company_name=None, dry_run=False) -> (rows, stored, failures, receipts_available)`.
  - `_write_export(rows, stored, failures, receipts_available, expense_count, out_dir, stem, columns, dry_run) -> ExportSummary`.
  - `run_export(client, *, customer_id, client_label, start_date, end_date, out_dir, dry_run=False, all_fields=False)` — same behavior as today when `all_fields=False`; wide CSV when `True`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_export_expenses.py`. First extend the `_ExportClient` stub (find its class definition and add a `list_categories` method and make `list_expenses` accept optional args):

```python
class _ExportClient:
    def __init__(self, expenses, receipts=None, fail_ids=(), categories=None):
        self._expenses = expenses
        self._receipts = receipts or {}
        self._fail_ids = set(fail_ids)
        self._categories = categories or [{"id": 8, "name": "Food"}]

    def list_expenses(self, customer_id=None, from_date=None, to_date=None):
        return self._expenses

    def list_categories(self):
        return self._categories

    def download_receipt(self, expense_id):
        if expense_id in self._fail_ids:
            raise ee.ApiError("boom", status=500)
        return self._receipts[expense_id]
```

(Replace the existing `_ExportClient` class with the above — it is additive: existing tests that construct `_ExportClient([...])` still work.)

Then add a new test class:

```python
class RunExportAllFieldsTests(unittest.TestCase):
    def _out(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        return d

    def _exp_full(self, id, number):
        return {"id": id, "expense_number": number, "expense_date": "2025-04-10",
                "amount": 1000, "notes": "n", "currency": {"code": "USD", "precision": 2},
                "expense_category_id": 8, "customer": {"id": 5, "name": "Ascendra"},
                "exchange_rate": "1", "created_at": "2025-04-10T00:00:00Z",
                "attachment_receipt_meta": None}

    def test_all_fields_writes_wide_csv(self):
        client = _ExportClient([self._exp_full(1, "EXP-1")])
        summary = ee.run_export(
            client, customer_id=5, client_label="Ascendra",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 6, 30),
            out_dir=self._out(), all_fields=True)
        with open(summary.csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ee.CSV_COLUMNS_ALL)
        self.assertEqual(rows[0]["category"], "Food")
        self.assertEqual(rows[0]["client"], "Ascendra")

    def test_default_still_six_columns(self):
        client = _ExportClient([self._exp_full(1, "EXP-1")])
        summary = ee.run_export(
            client, customer_id=5, client_label="Ascendra",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 6, 30),
            out_dir=self._out())  # all_fields defaults False
        with open(summary.csv_path, newline="", encoding="utf-8") as f:
            header = f.readline().strip()
        self.assertEqual(header, ",".join(ee.CSV_COLUMNS))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_export_expenses.py::RunExportAllFieldsTests -q`
Expected: FAIL (`run_export() got an unexpected keyword argument 'all_fields'`).

- [ ] **Step 3: Implement the helpers and rewrite `run_export`**

In `ishelf_ee/__init__.py`, add these three helpers immediately above the existing `run_export`:

```python
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
```

Then replace the entire body of `run_export` with:

```python
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
```

- [ ] **Step 4: Run the full export test module**

Run: `python3 -m pytest tests/test_export_expenses.py -q`
Expected: PASS — the new `RunExportAllFieldsTests` plus **all** pre-existing `RunExportTests` (the refactor preserves single-client behavior exactly: same filenames, same zip, same dry-run semantics, same zero-expense header-only CSV).

- [ ] **Step 5: Commit**

```bash
git add ishelf_ee/__init__.py tests/test_export_expenses.py
git commit -m "refactor(export): factor _collect_rows/_write_export; run_export all_fields"
```

---

### Task 4: `run_corpus_export` — all companies/clients, one combined artifact

**Files:**
- Modify: `ishelf_ee/__init__.py` (add `run_corpus_export`)
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: `_category_map`, `_collect_rows`, `_write_export` (Task 3); `list_companies()`, `list_expenses(customer_id=None, ...)` (Task 2).
- Produces: `run_corpus_export(client, *, out_dir, all_fields=True, start_date=None, end_date=None, dry_run=False, customer_id=None, all_companies=False) -> ExportSummary`. Iterates companies (all or current), sets `client.company_id` per company, lists expenses (unfiltered by customer when `customer_id is None`), accumulates into ONE combined CSV + zip via a single shared `ZipNamer` (receipt de-dup across companies). Filename stem `all-expenses_<start>-<end>` or `all-expenses_all-time` when dates are omitted.

- [ ] **Step 1: Write the failing tests**

Add a new stub + test class to `tests/test_export_expenses.py`:

```python
class _CorpusClient:
    """Stub for corpus export: multiple companies, per-company expenses."""
    def __init__(self, companies, expenses_by_company, categories=None):
        self.companies = companies
        self.expenses_by_company = expenses_by_company
        self._categories = categories or [{"id": 8, "name": "Food"}]
        self.company_id = "1"
        self.calls = []  # (company_id, customer_id) per list_expenses

    def list_companies(self):
        return self.companies

    def list_categories(self):
        return self._categories

    def list_expenses(self, customer_id=None, from_date=None, to_date=None):
        self.calls.append((self.company_id, customer_id))
        return self.expenses_by_company.get(self.company_id, [])

    def download_receipt(self, expense_id):
        return b"BYTES"


def _cexp(id, number, company_hint):
    return {"id": id, "expense_number": number, "expense_date": "2025-04-10",
            "amount": 1000, "notes": company_hint, "currency": {"code": "INR", "precision": 2},
            "expense_category_id": 8, "customer": {"id": 5, "name": "Cust-%s" % company_hint},
            "exchange_rate": "1", "created_at": "2025-04-10T00:00:00Z",
            "attachment_receipt_meta": None}


class RunCorpusExportTests(unittest.TestCase):
    def _out(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        return d

    def test_all_companies_combined_csv(self):
        client = _CorpusClient(
            companies=[{"id": 2, "name": "Arpit Goyal"}, {"id": 3, "name": "PeopleEquation"}],
            expenses_by_company={2: [_cexp(1, "A-1", "A")], 3: [_cexp(2, "B-1", "B")]})
        summary = ee.run_corpus_export(
            client, out_dir=self._out(), all_fields=True, all_companies=True)
        self.assertEqual(summary.expense_count, 2)
        with open(summary.csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ee.CSV_COLUMNS_ALL)
        companies = sorted(r["company"] for r in rows)
        self.assertEqual(companies, ["Arpit Goyal", "PeopleEquation"])
        self.assertTrue(os.path.basename(summary.csv_path).startswith("all-expenses_all-time"))

    def test_all_clients_lists_without_customer_id(self):
        client = _CorpusClient(
            companies=[{"id": 2, "name": "Arpit Goyal"}],
            expenses_by_company={2: [_cexp(1, "A-1", "A")]})
        ee.run_corpus_export(client, out_dir=self._out(), all_fields=True, all_companies=True)
        # every list_expenses call for the corpus must pass customer_id=None
        self.assertTrue(all(cust is None for (_co, cust) in client.calls))
        self.assertIn(2, [co for (co, _c) in client.calls])  # company header switched to 2

    def test_dates_appear_in_stem_when_given(self):
        client = _CorpusClient(companies=[{"id": 2, "name": "X"}],
                               expenses_by_company={2: []})
        summary = ee.run_corpus_export(
            client, out_dir=self._out(), all_fields=True, all_companies=True,
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 6, 30))
        self.assertIn("01042025-30062025", os.path.basename(summary.csv_path))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_export_expenses.py::RunCorpusExportTests -q`
Expected: FAIL (`module 'ishelf_ee' has no attribute 'run_corpus_export'`).

- [ ] **Step 3: Implement `run_corpus_export`**

In `ishelf_ee/__init__.py`, add immediately after `run_export`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_export_expenses.py::RunCorpusExportTests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ishelf_ee/__init__.py tests/test_export_expenses.py
git commit -m "feat(export): run_corpus_export for all companies/clients combined"
```

---

### Task 5: CLI wiring — `--export-all-fields`, `all` sentinels, optional dates

**Files:**
- Modify: `ishelf_ee/__init__.py` (`build_arg_parser`, `run_export_cli`)
- Test: `tests/test_export_expenses.py`

**Interfaces:**
- Consumes: `run_export` (Task 3), `run_corpus_export` (Task 4).
- Produces: CLI accepts `--export-all-fields` (store_true → `args.export_all_fields`); `--company all` / `--client all` route to `run_corpus_export`; `--start`/`--end` optional in corpus mode, still required otherwise.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_export_expenses.py`, inside `class CliTests`:

```python
    def test_parser_accepts_export_all_fields(self):
        args = ee.build_arg_parser().parse_args(
            ["--client", "Acme", "--start", "01042025", "--end", "30062025", "--export-all-fields"])
        self.assertTrue(args.export_all_fields)

    def test_corpus_mode_routes_to_run_corpus_export(self):
        out = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, out)
        cfg = ee.Config(url="https://x.example", email="m", password="p", company_id="1")
        fake = _CorpusClient(companies=[{"id": 2, "name": "Arpit Goyal"}],
                             expenses_by_company={2: [_cexp(1, "A-1", "A")]})
        fake.login = lambda: "tok"
        with mock.patch.object(ee, "load_config", lambda path: cfg), \
             mock.patch.object(ee, "InvoiceShelfClient", lambda config: fake):
            rc = ee.main(["--company", "all", "--client", "all",
                          "--export-all-fields", "--out", out])
        self.assertEqual(rc, 0)
        files = os.listdir(out)
        self.assertTrue(any(n.startswith("all-expenses_all-time") and n.endswith(".csv")
                            for n in files), files)

    def test_non_corpus_requires_dates(self):
        cfg = ee.Config(url="https://x.example", email="m", password="p", company_id="1")
        fake = _ExportClient([])
        fake.login = lambda: "tok"
        fake.find_customers = lambda name: [{"id": 5, "name": "Acme"}]
        with mock.patch.object(ee, "load_config", lambda path: cfg), \
             mock.patch.object(ee, "InvoiceShelfClient", lambda config: fake):
            rc = ee.main(["--client", "Acme"])  # no dates, not corpus
        self.assertEqual(rc, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_export_expenses.py::CliTests -q`
Expected: FAIL (`unrecognized arguments: --export-all-fields`, then routing/date assertions).

- [ ] **Step 3: Implement the CLI changes**

In `build_arg_parser`, change the `--start`/`--end` lines to be optional and add the flag. Replace:

```python
    parser.add_argument("--start", required=True, help="Start date, inclusive, DDMMYYYY.")
    parser.add_argument("--end", required=True, help="End date, inclusive, DDMMYYYY.")
```

with:

```python
    parser.add_argument("--start", default=None,
                        help="Start date, inclusive, DDMMYYYY. Required unless --company all / --client all.")
    parser.add_argument("--end", default=None,
                        help="End date, inclusive, DDMMYYYY. Required unless --company all / --client all.")
    parser.add_argument("--export-all-fields", action="store_true",
                        help="Export the full field set (company, client, category, exchange_rate, "
                             "expense_id, created_at) instead of the default six columns.")
```

Replace the whole body of `run_export_cli` with:

```python
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
            if not args.client and args.customer_id is None:
                raise ValueError("Provide --client NAME or --customer-id ID.")
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
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS — all new CLI tests plus every pre-existing test across all modules (routing, create, export). The pre-existing `CliTests.test_parser_reads_args`, `test_main_dry_run_end_to_end`, `test_main_reports_error_nonzero`, `test_company_flag_resolves_and_scopes`, `test_customer_id_skips_name_resolution` all still pass (they supply dates and named clients, i.e. non-corpus paths unchanged).

- [ ] **Step 5: Commit**

```bash
git add ishelf_ee/__init__.py tests/test_export_expenses.py
git commit -m "feat(cli): --export-all-fields flag + all-company/all-client corpus mode"
```

---

### Task 6: Docs — README + pinned list-expenses shape note

**Files:**
- Create: `docs/superpowers/notes/invoiceshelf-list-expenses.md`
- Modify: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Write the pinned-shape note**

Create `docs/superpowers/notes/invoiceshelf-list-expenses.md`:

```markdown
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
```

- [ ] **Step 2: Update the README**

In `README.md`, add a section documenting the new options (place it after the existing export usage):

```markdown
### Full-corpus export

Export every field (not just the default six) and/or every company/client in one run:

- `--export-all-fields` — widen the CSV to include `expense_id, company, client, category,
  exchange_rate, created_at` (category/customer ids resolved to names). Without it, the export
  is unchanged (six columns).
- `--company all` — iterate every company.
- `--client all` — include every client (no customer filter).

In `all` mode, `--start`/`--end` are optional (omit for all-time) and the output is a single
combined `all-expenses_<range>.csv` plus one combined receipts zip.

```bash
# Whole history, all companies/clients, all fields, with receipts:
ee --company all --client all --export-all-fields --config ~/.personal/configs/ishelf-config.env
```

The default per-client export (`--client NAME --start … --end …`) is unchanged.
```

- [ ] **Step 3: Verify docs render and suite still green**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (docs-only change; suite unaffected). Visually confirm both files read correctly.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/notes/invoiceshelf-list-expenses.md
git commit -m "docs: full-corpus export usage + pinned list-expenses shape"
```

---

## Self-Review

**1. Spec coverage:**
- `--export-all-fields` widens columns → Tasks 1, 3, 5. ✓
- Exact appended column list/order → Task 1 (`CSV_COLUMNS_ALL`) + Global Constraints. ✓
- Backward compat (default = 6 columns) → Task 1 (`test_default_row_unchanged`), Task 3 (`test_default_still_six_columns`), Task 5 (pre-existing CLI tests). ✓
- `--company all` / `--client all` sentinels → Task 5; underlying iteration/unfiltered-list → Tasks 2, 4. ✓
- Dates optional in `all` mode → Task 4 (stem `all-time`), Task 5 (`test_non_corpus_requires_dates` guards the inverse). ✓
- One combined CSV + zip in `all` scope; receipt de-dup across companies → Task 4 (shared `ZipNamer`). ✓
- Category/customer id→name resolution, shape-tolerant → Task 1 (+ fallback tests), Task 6 (pinned note). ✓
- CSV (no `.xlsx`), stdlib-only → honored throughout (no new imports). ✓
- First step = pin the live shape → Task 6 note + shape-tolerant builder makes it non-blocking. ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code and test block is literal. ✓

**3. Type consistency:** `expense_to_row(expense, receipt_file, *, all_fields, category_map, company_name)` is defined in Task 1 and called identically in `_collect_rows` (Task 3). `_collect_rows` / `_write_export` / `_category_map` signatures defined in Task 3 and consumed unchanged in Task 4. `run_export(..., all_fields=False)` (Task 3) and `run_corpus_export(..., all_companies=False, customer_id=None)` (Task 4) match their CLI call sites (Task 5). `list_expenses(customer_id=None, from_date=None, to_date=None)` (Task 2) matches all call sites. `CSV_COLUMNS_ALL` name consistent across Tasks 1/3/4. ✓
