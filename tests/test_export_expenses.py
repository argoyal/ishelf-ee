# tests/test_export_expenses.py
import csv as _csv
import datetime
import io
import json as _json
import os
import tempfile
import unittest
import zipfile
from unittest import mock
import ishelf_ee as ee


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _fake_json_resp(obj):
    return _FakeResp(_json.dumps(obj).encode("utf-8"))


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


class ConfigDiscoveryTests(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(ee.find_config_file("/tmp/whatever.env"), "/tmp/whatever.env")

    def test_env_var_candidate_used_when_exists(self):
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        self.addCleanup(os.remove, path)
        os.environ["INVOICESHELF_CONFIG"] = path
        self.addCleanup(os.environ.pop, "INVOICESHELF_CONFIG", None)
        self.assertEqual(ee.find_config_file(), path)

    def test_returns_none_when_no_candidate_exists(self):
        os.environ.pop("INVOICESHELF_CONFIG", None)
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        with mock.patch.object(ee, "_config_candidates", lambda: [os.path.join(d, "nope.env")]):
            self.assertIsNone(ee.find_config_file())


class RowCsvTests(unittest.TestCase):
    def _expense(self):
        return {
            "expense_number": "EXP-000001",
            "expense_date": "2025-04-15",
            "amount": 123456,
            "notes": "Taxi, airport",
            "currency": {"code": "USD", "precision": 2},
        }

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

    def test_list_companies_sends_company_header(self):
        client = self._client()
        client.token = "tok123"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _fake_json_resp({"data": [{"id": 2, "name": "Acme"}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = client.list_companies()

        self.assertEqual(data, [{"id": 2, "name": "Acme"}])
        self.assertTrue(captured["url"].endswith("/api/v1/companies"))
        self.assertEqual(captured["headers"]["company"], "1")
        self.assertEqual(captured["headers"]["authorization"], "Bearer tok123")

    def test_company_id_is_mutable_and_used_in_header(self):
        client = self._client()
        client.token = "tok123"
        client.company_id = 9  # simulate resolution overriding the config default
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _fake_json_resp({"data": []})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client.list_expenses(5, "2025-04-01", "2025-06-30")

        self.assertEqual(captured["headers"]["company"], "9")

    def test_request_sends_browser_user_agent(self):
        client = self._client()
        client.token = "tok123"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _fake_json_resp({"data": []})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client.list_expenses(5, "2025-04-01", "2025-06-30")

        ua = captured["headers"].get("user-agent", "")
        self.assertIn("Mozilla", ua)
        self.assertNotIn("Python-urllib", ua)

    def test_login_sends_browser_user_agent(self):
        # Login is unauthenticated but Cloudflare still filters on User-Agent,
        # so the browser UA must be present here too.
        client = self._client()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _fake_json_resp({"type": "Bearer", "token": "t"})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client.login()

        self.assertIn("Mozilla", captured["headers"].get("user-agent", ""))

    def test_user_agent_env_override(self):
        os.environ["INVOICESHELF_USER_AGENT"] = "CustomAgent/9.9"
        self.addCleanup(os.environ.pop, "INVOICESHELF_USER_AGENT", None)
        client = self._client()
        client.token = "tok123"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _fake_json_resp({"data": []})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client.list_expenses(5, "2025-04-01", "2025-06-30")

        self.assertEqual(captured["headers"].get("user-agent"), "CustomAgent/9.9")

    def test_http_error_becomes_apierror(self):
        import urllib.error
        client = self._client()

        def raise_401(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message":"bad"}'))

        with mock.patch("urllib.request.urlopen", raise_401):
            with self.assertRaises(ee.ApiError) as ctx:
                client.login()
        self.assertEqual(ctx.exception.status, 401)


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


class _CompanyStubClient:
    def __init__(self, companies):
        self._companies = companies

    def list_companies(self):
        return self._companies


class CompanyResolveTests(unittest.TestCase):
    def test_single_match(self):
        client = _CompanyStubClient([{"id": 3, "name": "My Co"}])
        self.assertEqual(ee.resolve_company_id(client, "my co"), 3)

    def test_substring_single_match(self):
        client = _CompanyStubClient([
            {"id": 3, "name": "My Company Ltd"},
            {"id": 4, "name": "Other Inc"},
        ])
        self.assertEqual(ee.resolve_company_id(client, "my company"), 3)

    def test_exact_match_wins_over_substring(self):
        client = _CompanyStubClient([
            {"id": 1, "name": "Acme"},
            {"id": 2, "name": "Acme Corp"},
        ])
        self.assertEqual(ee.resolve_company_id(client, "Acme"), 1)

    def test_zero_matches_raises(self):
        with self.assertRaises(LookupError):
            ee.resolve_company_id(_CompanyStubClient([{"id": 1, "name": "Acme"}]), "Nobody")

    def test_ambiguous_raises_with_ids(self):
        client = _CompanyStubClient([
            {"id": 1, "name": "Acme One"},
            {"id": 2, "name": "Acme Two"},
        ])
        with self.assertRaises(LookupError) as ctx:
            ee.resolve_company_id(client, "Acme")
        self.assertIn("INVOICESHELF_COMPANY_ID", str(ctx.exception))


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

    def test_zero_expenses_writes_header_only_csv_no_zip(self):
        client = _ExportClient([])
        out = self._out()
        summary = ee.run_export(
            client, customer_id=5, client_label="Acme",
            start_date=datetime.date(2025, 4, 1), end_date=datetime.date(2025, 4, 2),
            out_dir=out)
        self.assertEqual(summary.expense_count, 0)
        self.assertIsNone(summary.zip_path)
        self.assertTrue(os.path.exists(summary.csv_path))
        with open(summary.csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        self.assertEqual(rows, [])  # header only, no data rows
        with open(summary.csv_path, newline="", encoding="utf-8") as f:
            header = f.readline().strip()
        self.assertEqual(header, ",".join(ee.CSV_COLUMNS))


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

    def test_parser_accepts_company(self):
        args = ee.build_arg_parser().parse_args(
            ["--company", "My Co", "--client", "Acme", "--start", "01042025", "--end", "30062025"])
        self.assertEqual(args.company, "My Co")

    def test_company_flag_resolves_and_scopes(self):
        out = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, out)
        cfg = ee.Config(url="https://x.example", email="m", password="p", company_id="1")
        fake = _ExportClient([_exp(1, "EXP-1", False)])
        fake.company_id = "1"
        fake.login = lambda: "tok"
        fake.list_companies = lambda: [{"id": 42, "name": "My Company"}]
        fake.find_customers = lambda name: [{"id": 5, "name": "Acme"}]
        with mock.patch.object(ee, "load_config", lambda path: cfg), \
             mock.patch.object(ee, "InvoiceShelfClient", lambda config: fake):
            rc = ee.main(["--company", "My Company", "--client", "Acme",
                          "--start", "01042025", "--end", "30062025",
                          "--out", out, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.company_id, 42)  # resolved id applied to the client

    def test_customer_id_skips_name_resolution(self):
        out = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, out)
        cfg = ee.Config(url="https://x.example", email="m", password="p", company_id="1")
        fake = _ExportClient([_exp(1, "EXP-1", False)])
        fake.login = lambda: "tok"
        def _boom(name):
            raise AssertionError("find_customers must not be called when --customer-id is given")
        fake.find_customers = _boom
        with mock.patch.object(ee, "load_config", lambda path: cfg), \
             mock.patch.object(ee, "InvoiceShelfClient", lambda config: fake):
            rc = ee.main(["--customer-id", "7", "--start", "01042025",
                          "--end", "30062025", "--out", out])
        self.assertEqual(rc, 0)
