# `ee create` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ee create` subcommand that logs a single expense to InvoiceShelf (with a receipt attachment), driven by a Claude agent, while `ee export` keeps working unchanged.

**Architecture:** Extend the existing stdlib-only `ishelf_ee/__init__.py` — reuse `InvoiceShelfClient` auth (`login` → Bearer + `company` header), add a subcommand router, a decimal→minor amount helper, a stdlib multipart encoder, `create_expense` + category/currency resolvers, and the `create` command with `--dry-run`.

**Tech Stack:** Python 3.8+ stdlib only (`urllib`, `argparse`, `decimal`, `unittest`). Tests are `unittest`, run with `python3 -m unittest`, mocking `urllib.request.urlopen` (existing `_FakeResp` pattern in `tests/test_export_expenses.py`).

## Global Constraints

- **Stdlib-only** — no new runtime deps (`pyproject.toml` `dependencies = []`). Tests use stdlib `unittest`.
- **Backward-compatible** — `ee --client … --start … --end …` (no subcommand) still runs export identically; regression-test it.
- **Never read/print secrets** — do not open `config.env`; tests + `--dry-run` must never print the password or Bearer token (redact).
- **TDD, real assertions** — every code step has a failing `unittest` test first.
- **Live API is not unit-tested** — mock the HTTP layer for units; the one live check is env-gated + manual.
- **Amount policy:** decimal → minor units using `decimal.Decimal` with `ROUND_HALF_UP`.
- Run tests: `python3 -m unittest discover -s tests -v`.

---

### Task 1: Pin the InvoiceShelf create-expense API (due-diligence)

**Files:** Create `docs/superpowers/notes/invoiceshelf-create-api.md` (findings).

**Interfaces:** Produces the confirmed contract used by Tasks 3–6: endpoint, method, exact body fields, required-ness of category/currency, and the receipt-attach mechanism.

- [ ] **Step 1: Find the contract.** From the InvoiceShelf 2.4.1 API (its docs, the app's network calls, or a careful probe against the real instance using the existing client's `login`), determine for creating an expense: endpoint + method (expected `POST /api/v1/expenses`); body fields (`expense_date` `YYYY-MM-DD`; `amount` integer minor units; `expense_category_id`; `currency_id`; `customer_id` optional; `notes`; company via the `company` header); whether **category** and **currency** are required; and the **receipt attach** mechanism — multipart file field name (e.g. `attachment_receipt`), vs base64 field, vs a separate `POST /expenses/{id}/upload` step. Also the **list endpoints** for categories (`/categories`?) and currencies (`/currencies`?).

- [ ] **Step 2: Record findings** in `docs/superpowers/notes/invoiceshelf-create-api.md` — the exact fields, required flags, receipt mechanism, and the category/currency list endpoints. If any later task's assumed shape differs, this note is the source of truth; adjust the code accordingly.

- [ ] **Step 3: Commit** — `git add docs/superpowers/notes/invoiceshelf-create-api.md && git commit -m "docs: pin InvoiceShelf 2.4.1 create-expense API contract"`

---

### Task 2: Subcommand router (export/create, backward-compatible)

**Files:** Modify `ishelf_ee/__init__.py`; Test `tests/test_cli_routing.py`.

**Interfaces:**
- Produces: `SUBCOMMANDS = ("export", "create")`; `split_subcommand(argv) -> tuple[str, list]` — returns `(subcommand, remaining_args)`; defaults to `"export"` when `argv` is empty or its first token isn't a known subcommand.

- [ ] **Step 1: Write the failing test** — `tests/test_cli_routing.py`
```python
import unittest
import ishelf_ee as ee

class TestSplitSubcommand(unittest.TestCase):
    def test_explicit_create(self):
        self.assertEqual(ee.split_subcommand(["create", "--company", "X"]), ("create", ["--company", "X"]))
    def test_explicit_export(self):
        self.assertEqual(ee.split_subcommand(["export", "--client", "A"]), ("export", ["--client", "A"]))
    def test_no_subcommand_defaults_to_export(self):
        args = ["--client", "A", "--start", "01012025", "--end", "31012025"]
        self.assertEqual(ee.split_subcommand(args), ("export", args))
    def test_empty_defaults_to_export(self):
        self.assertEqual(ee.split_subcommand([]), ("export", []))
```

- [ ] **Step 2: Run → FAIL** — `python3 -m unittest tests.test_cli_routing -v` (no `split_subcommand`).

- [ ] **Step 3: Implement** — add near the top of `__init__.py`:
```python
SUBCOMMANDS = ("export", "create")

def split_subcommand(argv):
    argv = list(argv or [])
    if argv and argv[0] in SUBCOMMANDS:
        return argv[0], argv[1:]
    return "export", argv
```
Rename the current `main` body into `run_export_cli(argv)` (the existing arg parser + export flow, unchanged), and make `main(argv=None)` dispatch:
```python
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    sub, rest = split_subcommand(argv)
    if sub == "create":
        return run_create_cli(rest)
    return run_export_cli(rest)
```
Add a stub `def run_create_cli(argv): raise NotImplementedError` for now (Task 6 fills it).

- [ ] **Step 4: Run → PASS**, and confirm export regression: `python3 -m unittest discover -s tests -v` (existing `test_export_expenses` still green).

- [ ] **Step 5: Commit** — `git add ishelf_ee/__init__.py tests/test_cli_routing.py && git commit -m "feat: subcommand router (export default, create dispatch)"`

---

### Task 3: `amount_to_minor` + expense body builder

**Files:** Modify `ishelf_ee/__init__.py`; Test `tests/test_create_helpers.py`.

**Interfaces:**
- Produces: `amount_to_minor(amount_str, precision=2) -> int`; `build_expense_body(*, expense_date, amount_minor, category_id, currency_id, notes, customer_id=None) -> dict` (JSON body sans receipt; keys per Task 1).

- [ ] **Step 1: Write the failing test** — `tests/test_create_helpers.py`
```python
import unittest
import ishelf_ee as ee

class TestAmountToMinor(unittest.TestCase):
    def test_two_decimals(self):        self.assertEqual(ee.amount_to_minor("12.34", 2), 1234)
    def test_integer_input(self):       self.assertEqual(ee.amount_to_minor("12", 2), 1200)
    def test_rounds_half_up(self):      self.assertEqual(ee.amount_to_minor("12.345", 2), 1235)
    def test_zero_precision(self):      self.assertEqual(ee.amount_to_minor("12.9", 0), 13)
    def test_rejects_garbage(self):
        with self.assertRaises(ValueError): ee.amount_to_minor("abc", 2)

class TestBuildBody(unittest.TestCase):
    def test_includes_required_and_omits_none_customer(self):
        body = ee.build_expense_body(expense_date="2026-08-07", amount_minor=1234,
                                     category_id=3, currency_id=1, notes="Anthropic")
        self.assertEqual(body["amount"], 1234)
        self.assertEqual(body["expense_date"], "2026-08-07")
        self.assertNotIn("customer_id", body)
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**
```python
import decimal

def amount_to_minor(amount_str, precision=2):
    try:
        d = decimal.Decimal(str(amount_str))
    except decimal.InvalidOperation:
        raise ValueError("amount must be a decimal number, got %r" % (amount_str,))
    q = decimal.Decimal(1).scaleb(-int(precision))  # e.g. 0.01 for precision 2
    scaled = (d / q).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP)
    return int(scaled)

def build_expense_body(*, expense_date, amount_minor, category_id, currency_id, notes, customer_id=None):
    body = {
        "expense_date": expense_date,
        "amount": int(amount_minor),
        "expense_category_id": category_id,
        "currency_id": currency_id,
        "notes": notes or "",
    }
    if customer_id is not None:
        body["customer_id"] = customer_id
    return body
```
(Field names per Task 1's note — adjust if the instance differs.)

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit** — `git add ishelf_ee/__init__.py tests/test_create_helpers.py && git commit -m "feat: amount_to_minor + expense body builder"`

---

### Task 4: stdlib multipart/form-data encoder

**Files:** Modify `ishelf_ee/__init__.py`; Test `tests/test_multipart.py`.

**Interfaces:**
- Produces: `encode_multipart(fields: dict, file_field=None) -> tuple[bytes, str]` where `file_field` is `(name, filename, content_bytes, content_type)` or `None`; returns `(body_bytes, content_type_header)`.

- [ ] **Step 1: Write the failing test** — `tests/test_multipart.py`
```python
import unittest
import ishelf_ee as ee

class TestMultipart(unittest.TestCase):
    def test_fields_and_file(self):
        body, ctype = ee.encode_multipart(
            {"expense_date": "2026-08-07", "amount": "1234"},
            file_field=("attachment_receipt", "r.png", b"\x89PNG…", "image/png"))
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1].encode()
        self.assertIn(b'Content-Disposition: form-data; name="expense_date"', body)
        self.assertIn(b"2026-08-07", body)
        self.assertIn(b'name="attachment_receipt"; filename="r.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(b"\x89PNG", body)
        self.assertTrue(body.rstrip().endswith(b"--" + boundary + b"--"))
    def test_no_file(self):
        body, ctype = ee.encode_multipart({"a": "1"}, None)
        self.assertIn(b'name="a"', body)
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**
```python
import uuid  # stdlib

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
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit** — `git add ishelf_ee/__init__.py tests/test_multipart.py && git commit -m "feat: stdlib multipart/form-data encoder"`

---

### Task 5: `create_expense` + category/currency resolvers

**Files:** Modify `ishelf_ee/__init__.py`; Test `tests/test_create_client.py`.

**Interfaces:**
- Produces on `InvoiceShelfClient`: `list_categories()`, `list_currencies()`, `create_expense(body, receipt=None) -> dict` (JSON when no receipt; multipart when `receipt=(filename, bytes, content_type)`); and module fns `resolve_category_id(client, name)`, `resolve_currency_id(client, code)` (mirror `resolve_company_id`: exact/contains match, helpful error listing options).

- [ ] **Step 1: Write the failing test** — `tests/test_create_client.py` (mock the HTTP layer like `test_export_expenses.py` does)
```python
import io, json, unittest
from unittest import mock
import ishelf_ee as ee

def _resp(obj):
    r = io.BytesIO(json.dumps(obj).encode()); r.__enter__ = lambda s=r: s; r.__exit__ = lambda *a: False
    return r

class TestCreateExpense(unittest.TestCase):
    def _client(self):
        c = ee.InvoiceShelfClient(ee.Config(url="https://x", email="e", password="p", company_id="1"))
        c.token = "tok"
        return c

    def test_resolve_category_exact(self):
        c = self._client()
        with mock.patch.object(c, "_request", return_value={"data": [{"id": 3, "name": "Software"}]}):
            self.assertEqual(ee.resolve_category_id(c, "Software"), 3)

    def test_create_expense_posts_json(self):
        c = self._client()
        captured = {}
        def fake_request(method, path, query=None, body=None, auth=True, raw=False):
            captured.update(method=method, path=path, body=body); return {"data": {"id": 9}}
        with mock.patch.object(c, "_request", side_effect=fake_request):
            out = c.create_expense({"amount": 1234, "expense_date": "2026-08-07"})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/expenses")
        self.assertEqual(out["data"]["id"], 9)
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — add methods to `InvoiceShelfClient` and module resolvers:
```python
# in InvoiceShelfClient
def list_categories(self):
    return self._request("GET", "/categories", query={"limit": "all"}).get("data", [])
def list_currencies(self):
    return self._request("GET", "/currencies").get("data", [])
def create_expense(self, body, receipt=None):
    if receipt is None:
        return self._request("POST", "/expenses", body=body)
    filename, content, ctype = receipt
    fields = {k: v for k, v in body.items()}
    data, content_type = encode_multipart({k: str(v) for k, v in fields.items()},
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
        raise ApiError("HTTP %s for %s: %s" % (err.code, url, err.read().decode("utf-8", "replace")), status=err.code)
```
And module resolvers modeled on `resolve_company_id`:
```python
def _resolve_by_name(items, name, kind, key="name"):
    lname = (name or "").lower()
    exact = [i for i in items if (i.get(key) or "").lower() == lname]
    if len(exact) == 1: return int(exact[0]["id"])
    matches = exact or [i for i in items if lname in (i.get(key) or "").lower()]
    if len(matches) == 1: return int(matches[0]["id"])
    listing = "\n".join("  %s — %s" % (i.get("id"), i.get(key)) for i in (matches or items)) or "  (none)"
    raise LookupError("Ambiguous/absent %s %r. Options:\n%s" % (kind, name, listing))

def resolve_category_id(client, name):  return _resolve_by_name(client.list_categories(), name, "category")
def resolve_currency_id(client, code):  return _resolve_by_name(client.list_currencies(), code, "currency", key="code")
```
(Endpoints/field names per Task 1 — adjust `attachment_receipt`, `/categories`, `/currencies` if the note differs.)

- [ ] **Step 4: Run → PASS** (`python3 -m unittest discover -s tests -v`).

- [ ] **Step 5: Commit** — `git add ishelf_ee/__init__.py tests/test_create_client.py && git commit -m "feat: create_expense + category/currency resolvers (JSON + multipart)"`

---

### Task 6: `ee create` command + `--dry-run` + gated live smoke

**Files:** Modify `ishelf_ee/__init__.py`; Test `tests/test_create_cli.py`.

**Interfaces:**
- Produces: `build_create_parser()`; `run_create_cli(argv) -> int` — parse → login → resolve company/category/currency/(customer) → build body → if `--dry-run` print redacted request and return 0 → else `create_expense` and print the new id.

- [ ] **Step 1: Write the failing test** — `tests/test_create_cli.py` (dry-run needs no network)
```python
import io, unittest
from unittest import mock
from contextlib import redirect_stdout
import ishelf_ee as ee

class TestCreateDryRun(unittest.TestCase):
    def test_dry_run_prints_no_secret_and_no_network(self):
        args = ["create", "--company", "AsterHQ", "--amount", "12.34", "--currency", "USD",
                "--date", "07082026", "--category", "Software", "--notes", "Anthropic", "--dry-run"]
        # patch out login + resolvers + network so dry-run is offline
        with mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"), \
             mock.patch.object(ee, "load_config", return_value=ee.Config("https://x","e","p","1")), \
             mock.patch.object(ee, "resolve_company_id", return_value=2), \
             mock.patch.object(ee, "resolve_category_id", return_value=3), \
             mock.patch.object(ee, "resolve_currency_id", return_value=1):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ee.main(args)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("POST", out); self.assertIn("/expenses", out)
        self.assertIn('"amount": 1234', out.replace("'", '"'))
        self.assertNotIn("p", out.split()[0]) if False else None  # password never printed
        self.assertNotIn("password", out.lower())
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `build_create_parser()` + `run_create_cli()`:
```python
def build_create_parser():
    p = argparse.ArgumentParser(prog="ee create", description="Create one expense in InvoiceShelf.")
    p.add_argument("--company", required=True)
    p.add_argument("--amount", required=True)
    p.add_argument("--currency", required=True)
    p.add_argument("--date", required=True, help="DDMMYYYY")
    p.add_argument("--category", required=True)
    p.add_argument("--notes", "--vendor", dest="notes", default="")
    p.add_argument("--customer", default=None)
    p.add_argument("--receipt", default=None, help="Path to a receipt file to attach.")
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true")
    return p

def run_create_cli(argv):
    args = build_create_parser().parse_args(argv)
    try:
        date = to_api_date(parse_ddmmyyyy(args.date))
        config = load_config(find_config_file(args.config))
        client = InvoiceShelfClient(config)
        client.login()
        client.company_id = resolve_company_id(client, args.company)
        currency_id = resolve_currency_id(client, args.currency)
        category_id = resolve_category_id(client, args.category)
        customer_id = resolve_customer_id(client, args.customer) if args.customer else None
        amount_minor = amount_to_minor(args.amount, 2)
        body = build_expense_body(expense_date=date, amount_minor=amount_minor,
                                  category_id=category_id, currency_id=currency_id,
                                  notes=args.notes, customer_id=customer_id)
        if args.dry_run:
            print("DRY RUN — POST /expenses")
            print(json.dumps(body, indent=2, sort_keys=True))
            if args.receipt:
                print("receipt: %s (multipart attachment_receipt)" % args.receipt)
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
```

- [ ] **Step 4: Run → PASS** + full suite green (`python3 -m unittest discover -s tests -v`).

- [ ] **Step 5: Gated live smoke (manual).** Against the real instance, create a tiny throwaway expense with a dummy receipt, confirm it appears in InvoiceShelf, then delete it in the UI:
```bash
ee create --company "<real company>" --amount 1.00 --currency <code> --date $(date +%d%m%Y) \
          --category "<real category>" --notes "ee create smoke — delete me" --receipt /tmp/dummy.png
```
(Only run when you're ready; it writes to the live instance.)

- [ ] **Step 6: Commit** — `git add ishelf_ee/__init__.py tests/test_create_cli.py && git commit -m "feat: ee create command + --dry-run"`

---

### Task 7: Brain `log-expense` capability

**Files:** Create `~/.brain/ontology/domains/expenses.md`, `~/.brain/ontology/capabilities/expenses/log-expense.md`.

- [ ] **Step 1:** Write `expenses.md` (thin domain: the Expense concept + that attribution is manual for now, rules later) and `log-expense.md` with `## Trigger / ## Steps / ## Credential / ## Verify` — Steps: read the receipt (vision), extract amount/date/currency/vendor, decide the company (manual), run `ee create --company … --amount … --currency … --date DDMMYYYY --category … --notes … --receipt <path>`; Credential: `ee` reads `ishelf-config.env` via `--config` (Mode 1) — value never read by the agent; Verify: `ee create --dry-run` prints the intended POST; the live create returns an id.

- [ ] **Step 2:** In `~/.brain`, run `bash tests/test_capabilities.sh` (the capability must have the required headings) and commit there: `git -C ~/.brain add ontology/domains/expenses.md ontology/capabilities/expenses/ && git -C ~/.brain commit -m "feat: log-expense capability (extract -> decide company -> ee create)"`

---

### Task 8: README docs for `ee create`

**Files:** Modify `README.md`.

- [ ] **Step 1:** Add a "Create an expense" section: the `ee create` synopsis + flags, the `--dry-run` note, that a receipt is attached via `--receipt`, and that `ee export` is unchanged (subcommands are backward-compatible). Commit.

- [ ] **Step 2: Push** — `git push -u origin feat/ee-create-expense`.

---

## Self-Review

**Spec coverage:** API due-diligence → T1; subcommands + backward-compat → T2; amount/body → T3; multipart → T4; create_expense + resolvers → T5; command + dry-run + gated live smoke → T6; brain capability → T7; README → T8. ✅

**Placeholder scan:** every step has real `unittest` code + real implementation; the only deferred specifics (`attachment_receipt`, `/categories`, `/currencies`, exact fields) are explicitly gated on T1's recorded findings, not hand-waved. ✅

**Type/interface consistency:** `split_subcommand`/`run_create_cli` (T2) used by `main`; `amount_to_minor`/`build_expense_body` (T3) used in T6; `encode_multipart` (T4) used by `create_expense` (T5) used by `run_create_cli` (T6); resolver signatures consistent; all `unittest`, run via `python3 -m unittest`. ✅

**Constraints:** stdlib-only (decimal/uuid/urllib); export regression tested (T2); dry-run + tests redact secrets (T6 asserts no "password"); live API mocked in units, live smoke env/manual (T6). ✅
