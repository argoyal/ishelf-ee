import io
import json
import os
import tempfile
import unittest
from unittest import mock
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import ishelf_ee as ee


def _client():
    c = ee.InvoiceShelfClient(ee.Config(url="https://x", email="e", password="p", company_id="1"))
    c.token = "tok"
    return c


USD_CUSTOMER = {"id": 7, "name": "Aster AI Inc", "currency_id": 2,
                "currency": {"id": 2, "code": "USD", "precision": 2}}
INR_CUSTOMER = {"id": 8, "name": "PeopleEquation Private Ltd", "currency_id": 1,
                "currency": {"id": 1, "code": "INR", "precision": 2}}


class TestParseItem(unittest.TestCase):
    def test_name_and_price(self):
        self.assertEqual(ee.parse_item("Consulting Sep 2026=3781"),
                         {"name": "Consulting Sep 2026", "price_minor": 378100, "quantity": "1"})

    def test_quantity(self):
        self.assertEqual(ee.parse_item("Hours=50@40"),
                         {"name": "Hours", "price_minor": 5000, "quantity": "40"})

    def test_name_may_contain_equals(self):
        self.assertEqual(ee.parse_item("a=b=10")["name"], "a=b")

    def test_bad_inputs(self):
        for bad in ("NoPrice", "=10", "X=abc", "X=10@0", "X=-5"):
            with self.assertRaises(ValueError, msg=bad):
                ee.parse_item(bad)


class TestExchangeRate(unittest.TestCase):
    def test_same_currency_no_rate(self):
        self.assertIsNone(ee.resolve_exchange_rate(foreign=False, amount_minor=100))

    def test_same_currency_rejects_rate(self):
        with self.assertRaises(ValueError):
            ee.resolve_exchange_rate(foreign=False, amount_minor=100, exchange_rate="2")
        with self.assertRaises(ValueError):
            ee.resolve_exchange_rate(foreign=False, amount_minor=100, inr_amount="2")

    def test_foreign_requires_one(self):
        with self.assertRaises(ValueError):
            ee.resolve_exchange_rate(foreign=True, amount_minor=100)
        with self.assertRaises(ValueError):
            ee.resolve_exchange_rate(foreign=True, amount_minor=100,
                                     exchange_rate="2", inr_amount="2")

    def test_explicit_rate(self):
        self.assertEqual(ee.resolve_exchange_rate(foreign=True, amount_minor=100,
                                                  exchange_rate="94.05"), "94.05")

    def test_rate_from_inr_amount_six_places(self):
        # USD 3,781 credited as INR 3,55,639.79 (eFIRC CITIN26740027028).
        rate = ee.resolve_exchange_rate(foreign=True, amount_minor=378100,
                                        inr_amount="355639.79")
        self.assertEqual(rate, "94.059717")
        self.assertEqual(ee.base_minor(378100, rate), 35563979)

    def test_rejects_non_positive_rate(self):
        with self.assertRaises(ValueError):
            ee.resolve_exchange_rate(foreign=True, amount_minor=100, exchange_rate="0")


class TestBuildInvoiceBody(unittest.TestCase):
    def test_body_shape(self):
        items = [ee.parse_item("Consulting=3781"), ee.parse_item("Hours=10@3")]
        body = ee.build_invoice_body(
            invoice_date="2026-09-30", due_date="2026-10-15", customer_id=7,
            invoice_number="INV-000003", currency_id=2, exchange_rate="94.059717",
            items=items, notes="Sept work", template_name="invoice1")
        self.assertEqual(body["sub_total"], 378100 + 3000)
        self.assertEqual(body["total"], 381100)
        self.assertEqual(body["tax"], 0)
        self.assertEqual(body["discount"], 0)
        self.assertEqual(body["discount_val"], 0)
        self.assertEqual(body["currency_id"], 2)
        self.assertEqual(body["exchange_rate"], "94.059717")
        self.assertEqual(body["due_date"], "2026-10-15")
        self.assertEqual(body["taxes"], [])
        first = body["items"][0]
        self.assertEqual(first["name"], "Consulting")
        self.assertEqual(first["price"], 378100)
        self.assertEqual(first["total"], 378100)
        # createItems reads these keys directly, so they must be present.
        for key in ("discount_type", "discount_val", "tax"):
            self.assertIn(key, first)
        self.assertEqual(body["items"][1]["total"], 3000)

    def test_omits_optional(self):
        body = ee.build_invoice_body(
            invoice_date="2026-09-30", due_date=None, customer_id=8, invoice_number="I1",
            currency_id=1, exchange_rate=None, items=[ee.parse_item("X=1")], notes="",
            template_name="invoice1")
        self.assertNotIn("due_date", body)
        self.assertNotIn("exchange_rate", body)


class TestInvoiceClient(unittest.TestCase):
    def _capture(self, call, ret=None):
        c = _client()
        captured = {}

        def fake(method, path, query=None, body=None, auth=True, raw=False):
            captured.update(method=method, path=path, query=query, body=body)
            return ret if ret is not None else {"data": []}

        with mock.patch.object(c, "_request", side_effect=fake):
            out = call(c)
        return captured, out

    def test_list_invoices(self):
        cap, _ = self._capture(lambda c: c.list_invoices(customer_id=7, from_date="2026-09-01",
                                                         to_date="2026-09-30"))
        self.assertEqual((cap["method"], cap["path"]), ("GET", "/invoices"))
        self.assertEqual(cap["query"], {"limit": "all", "customer_id": 7,
                                        "from_date": "2026-09-01", "to_date": "2026-09-30"})

    def test_create_and_delete(self):
        cap, _ = self._capture(lambda c: c.create_invoice({"a": 1}))
        self.assertEqual((cap["method"], cap["path"], cap["body"]), ("POST", "/invoices", {"a": 1}))
        cap, _ = self._capture(lambda c: c.delete_invoices([4]))
        self.assertEqual((cap["path"], cap["body"]), ("/invoices/delete", {"ids": [4]}))

    def test_next_number(self):
        cap, out = self._capture(lambda c: c.next_number("invoice"),
                                 ret={"success": True, "nextNumber": "INV-000004"})
        self.assertEqual((cap["path"], cap["query"]), ("/next-number", {"key": "invoice"}))
        self.assertEqual(out, "INV-000004")

    def test_next_number_failure_raises(self):
        with self.assertRaises(ee.ApiError):
            self._capture(lambda c: c.next_number("invoice"),
                          ret={"success": False, "message": "boom"})

    def test_company_currency(self):
        cap, out = self._capture(lambda c: c.get_company_currency_id(), ret={"currency": "1"})
        self.assertEqual(cap["path"], "/company/settings")
        self.assertEqual(cap["query"], [("settings[]", "currency")])
        self.assertEqual(out, 1)


class TestResolveByNumber(unittest.TestCase):
    def test_invoice_unique_absent_ambiguous(self):
        c = _client()
        rows = [{"id": 1, "invoice_number": "INV-1"}, {"id": 2, "invoice_number": "INV-2"},
                {"id": 3, "invoice_number": "INV-2"}]
        with mock.patch.object(c, "list_invoices", return_value=rows):
            self.assertEqual(ee.resolve_invoice_by_number(c, "INV-1")["id"], 1)
            with self.assertRaises(LookupError):
                ee.resolve_invoice_by_number(c, "INV-9")
            with self.assertRaises(LookupError):
                ee.resolve_invoice_by_number(c, "INV-2")


def _patches(es, *, customer=USD_CUSTOMER, company_currency=1, next_number="INV-000004"):
    es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"))
    es.enter_context(mock.patch.object(ee, "load_config",
                                       return_value=ee.Config("https://x", "e", "sekret-pw", "1")))
    es.enter_context(mock.patch.object(ee, "resolve_company_id", return_value=2))
    es.enter_context(mock.patch.object(ee, "resolve_customer", return_value=customer))
    es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "get_company_currency_id",
                                       return_value=company_currency))
    es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "next_number",
                                       return_value=next_number))


def _main(args, extra=None):
    out, err = io.StringIO(), io.StringIO()
    with ExitStack() as es:
        _patches(es, **(extra or {}))
        create = es.enter_context(mock.patch.object(
            ee.InvoiceShelfClient, "create_invoice",
            return_value={"data": {"id": 55, "invoice_number": "INV-000004"}}))
        with redirect_stdout(out), redirect_stderr(err):
            rc = ee.main(args)
    return rc, out.getvalue(), err.getvalue(), create


BASE = ["invoice", "create", "--company", "Ascendra Ventures", "--client", "Aster AI Inc",
        "--date", "30092026", "--item", "Consulting Sep 2026=3781"]


class TestInvoiceCreateCLI(unittest.TestCase):
    def test_dry_run_foreign_with_inr_amount(self):
        rc, out, _, create = _main(BASE + ["--inr-amount", "355639.79", "--dry-run"])
        self.assertEqual(rc, 0)
        create.assert_not_called()
        self.assertIn("POST /invoices", out)
        body = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertEqual(body["invoice_number"], "INV-000004")
        self.assertEqual(body["invoice_date"], "2026-09-30")
        self.assertEqual(body["customer_id"], 7)
        self.assertEqual(body["currency_id"], 2)
        self.assertEqual(body["exchange_rate"], "94.059717")
        self.assertEqual(body["total"], 378100)
        self.assertIn("355639.79", out)  # base (company-currency) total shown
        self.assertNotIn("sekret-pw", out)

    def test_foreign_without_rate_errors(self):
        rc, _, err, create = _main(BASE + ["--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("--inr-amount", err)
        create.assert_not_called()

    def test_currency_mismatch_errors(self):
        rc, _, err, _ = _main(BASE + ["--currency", "INR", "--exchange-rate", "94", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("USD", err)

    def test_same_currency_customer(self):
        args = ["invoice", "create", "--company", "Arpit Goyal",
                "--client", "PeopleEquation Private Ltd", "--date", "01102026",
                "--due-date", "15102026", "--item", "Retainer=150000",
                "--invoice-number", "PE-01", "--currency", "inr", "--dry-run"]
        rc, out, err, _ = _main(args, extra={"customer": INR_CUSTOMER})
        self.assertEqual(rc, 0, err)
        body = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertEqual(body["invoice_number"], "PE-01")
        self.assertEqual(body["due_date"], "2026-10-15")
        self.assertNotIn("exchange_rate", body)

    def test_real_create(self):
        rc, out, _, create = _main(BASE + ["--exchange-rate", "94.059717"])
        self.assertEqual(rc, 0)
        create.assert_called_once()
        self.assertIn("Created invoice INV-000004 (id 55)", out)


class TestInvoiceListExportDelete(unittest.TestCase):
    INVOICES = [{"id": 55, "invoice_number": "INV-000004", "invoice_date": "2026-09-30",
                 "due_date": None, "total": 378100, "due_amount": 0, "base_total": 35563979,
                 "exchange_rate": "94.059717", "status": "DRAFT", "paid_status": "PAID",
                 "customer": {"name": "Aster AI Inc"},
                 "currency": {"code": "USD", "precision": 2}, "notes": "",
                 "formatted_created_at": "30/09/2026"}]

    def _run(self, args, invoices=None, delete_ret=None):
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as es:
            _patches(es)
            lst = es.enter_context(mock.patch.object(
                ee.InvoiceShelfClient, "list_invoices",
                return_value=self.INVOICES if invoices is None else invoices))
            dele = es.enter_context(mock.patch.object(
                ee.InvoiceShelfClient, "delete_invoices", return_value=delete_ret or {"success": True}))
            with redirect_stdout(out), redirect_stderr(err):
                rc = ee.main(args)
        return rc, out.getvalue(), err.getvalue(), lst, dele

    def test_invoice_to_row(self):
        row = ee.invoice_to_row(self.INVOICES[0])
        self.assertEqual(row["total"], "3781.00")
        self.assertEqual(row["due_amount"], "0.00")
        self.assertEqual(row["base_total"], "355639.79")
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["customer"], "Aster AI Inc")
        self.assertEqual(row["paid_status"], "PAID")

    def test_list_prints_rows_and_filters(self):
        rc, out, _, lst, _ = self._run(["invoice", "list", "--company", "A", "--client", "Aster AI Inc",
                                        "--start", "01092026", "--end", "30092026"])
        self.assertEqual(rc, 0)
        self.assertIn("INV-000004", out)
        self.assertIn("3781.00", out)
        lst.assert_called_once_with(customer_id=7, from_date="2026-09-01", to_date="2026-09-30")

    def test_export_writes_csv(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _, _, _ = self._run(["invoice", "export", "--company", "Ascendra Ventures",
                                          "--out", d])
            self.assertEqual(rc, 0)
            path = os.path.join(d, "invoices_Ascendra Ventures_all-time.csv")
            self.assertTrue(os.path.exists(path), out)
            with open(path) as f:
                text = f.read()
            self.assertIn("invoice_number", text.splitlines()[0])
            self.assertIn("INV-000004", text)

    def test_export_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _, _, _ = self._run(["invoice", "export", "--company", "A", "--out", d,
                                          "--start", "01092026", "--end", "30092026", "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertEqual(os.listdir(d), [])
            self.assertIn("invoices_A_01092026-30092026.csv", out)

    def test_start_without_end_errors(self):
        rc, _, err, _, _ = self._run(["invoice", "list", "--company", "A", "--start", "01092026"])
        self.assertEqual(rc, 1)

    def test_delete_dry_run_and_real(self):
        rc, out, _, _, dele = self._run(["invoice", "delete", "--company", "A",
                                         "--invoice-number", "INV-000004", "--dry-run"])
        self.assertEqual(rc, 0)
        dele.assert_not_called()
        self.assertIn("would delete invoice INV-000004", out)
        rc, out, _, _, dele = self._run(["invoice", "delete", "--company", "A",
                                         "--invoice-number", "INV-000004"])
        self.assertEqual(rc, 0)
        dele.assert_called_once_with([55])

    def test_delete_absent_errors(self):
        rc, _, err, _, dele = self._run(["invoice", "delete", "--company", "A",
                                         "--invoice-number", "NOPE"])
        self.assertEqual(rc, 1)
        dele.assert_not_called()

    def test_unknown_verb(self):
        rc, _, err, _, _ = self._run(["invoice", "frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("create", err)


if __name__ == "__main__":
    unittest.main()
