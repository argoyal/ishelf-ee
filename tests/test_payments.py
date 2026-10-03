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


USD_CUSTOMER = {"id": 7, "name": "Aster AI Inc", "currency_id": 2}
INVOICE = {"id": 55, "invoice_number": "INV-000004", "customer_id": 7, "currency_id": 2,
           "total": 378100, "due_amount": 378100, "customer": USD_CUSTOMER}


class TestBuildPaymentBody(unittest.TestCase):
    def test_body(self):
        body = ee.build_payment_body(
            payment_date="2026-10-01", customer_id=7, payment_number="PAY-000001",
            amount_minor=378100, currency_id=2, invoice_id=55, exchange_rate="94.059717",
            payment_method_id=3, notes="eFIRC CITIN26740027028")
        self.assertEqual(body, {
            "payment_date": "2026-10-01", "customer_id": 7, "payment_number": "PAY-000001",
            "amount": 378100, "currency_id": 2, "invoice_id": 55, "exchange_rate": "94.059717",
            "payment_method_id": 3, "notes": "eFIRC CITIN26740027028"})

    def test_omits_optional(self):
        body = ee.build_payment_body(
            payment_date="2026-10-01", customer_id=8, payment_number="P1", amount_minor=100,
            currency_id=1, invoice_id=None, exchange_rate=None, payment_method_id=None, notes="")
        for key in ("invoice_id", "exchange_rate", "payment_method_id"):
            self.assertNotIn(key, body)


class TestPaymentClient(unittest.TestCase):
    def test_endpoints(self):
        c = _client()
        calls = []

        def fake(method, path, query=None, body=None, auth=True, raw=False):
            calls.append((method, path, query, body))
            return {"data": []}

        with mock.patch.object(c, "_request", side_effect=fake):
            c.list_payments(customer_id=7)
            c.create_payment({"a": 1})
            c.delete_payments([9])
        self.assertEqual(calls[0][:3], ("GET", "/payments", {"limit": "all", "customer_id": 7}))
        self.assertEqual(calls[1], ("POST", "/payments", None, {"a": 1}))
        self.assertEqual(calls[2], ("POST", "/payments/delete", None, {"ids": [9]}))

    def test_resolve_payment_by_number(self):
        c = _client()
        with mock.patch.object(c, "list_payments",
                               return_value=[{"id": 9, "payment_number": "PAY-1"}]):
            self.assertEqual(ee.resolve_payment_by_number(c, "PAY-1")["id"], 9)
            with self.assertRaises(LookupError):
                ee.resolve_payment_by_number(c, "PAY-2")


def _run(args, *, invoice=INVOICE, customer=USD_CUSTOMER, payments=None):
    out, err = io.StringIO(), io.StringIO()
    with ExitStack() as es:
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"))
        es.enter_context(mock.patch.object(ee, "load_config",
                                           return_value=ee.Config("https://x", "e", "sekret-pw", "1")))
        es.enter_context(mock.patch.object(ee, "resolve_company_id", return_value=2))
        es.enter_context(mock.patch.object(ee, "resolve_customer", return_value=customer))
        es.enter_context(mock.patch.object(ee, "resolve_payment_method_id", return_value=3))
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "list_invoices",
                                           return_value=[invoice] if invoice else []))
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "get_company_currency_id",
                                           return_value=1))
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "next_number",
                                           return_value="PAY-000001"))
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "list_payments",
                                           return_value=payments or []))
        create = es.enter_context(mock.patch.object(
            ee.InvoiceShelfClient, "create_payment",
            return_value={"data": {"id": 91, "payment_number": "PAY-000001"}}))
        dele = es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "delete_payments",
                                                  return_value={"success": True}))
        with redirect_stdout(out), redirect_stderr(err):
            rc = ee.main(args)
    return rc, out.getvalue(), err.getvalue(), create, dele


AGAINST_INVOICE = ["payment", "create", "--company", "Ascendra Ventures",
                   "--invoice", "INV-000004", "--amount", "3781", "--date", "01102026",
                   "--inr-amount", "355639.79", "--payment-method", "Bank Transfer",
                   "--notes", "eFIRC CITIN26740027028"]


def _body(out):
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


class TestPaymentCreateCLI(unittest.TestCase):
    def test_dry_run_against_invoice(self):
        rc, out, err, create, _ = _run(AGAINST_INVOICE + ["--dry-run"])
        self.assertEqual(rc, 0, err)
        create.assert_not_called()
        self.assertIn("POST /payments", out)
        body = _body(out)
        self.assertEqual(body["invoice_id"], 55)
        self.assertEqual(body["customer_id"], 7)
        self.assertEqual(body["currency_id"], 2)
        self.assertEqual(body["amount"], 378100)
        self.assertEqual(body["exchange_rate"], "94.059717")
        self.assertEqual(body["payment_method_id"], 3)
        self.assertEqual(body["payment_number"], "PAY-000001")
        self.assertEqual(body["payment_date"], "2026-10-01")
        self.assertIn("355639.79", out)
        self.assertNotIn("sekret-pw", out)

    def test_real_create(self):
        rc, out, _, create, _ = _run(AGAINST_INVOICE)
        self.assertEqual(rc, 0)
        create.assert_called_once()
        self.assertIn("Recorded payment PAY-000001 (id 91)", out)

    def test_overpayment_rejected(self):
        inv = dict(INVOICE, due_amount=100000)
        rc, _, err, create, _ = _run(AGAINST_INVOICE, invoice=inv)
        self.assertEqual(rc, 1)
        self.assertIn("exceeds", err)
        create.assert_not_called()

    def test_client_must_match_invoice(self):
        other = {"id": 99, "name": "Someone Else", "currency_id": 2}
        rc, _, err, create, _ = _run(AGAINST_INVOICE + ["--client", "Someone Else"], customer=other)
        self.assertEqual(rc, 1)
        create.assert_not_called()

    def test_needs_invoice_or_client(self):
        rc, _, err, _, _ = _run(["payment", "create", "--company", "A", "--amount", "1",
                                 "--date", "01102026", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("--invoice", err)

    def test_without_invoice_uses_client(self):
        inr = {"id": 8, "name": "PeopleEquation Private Ltd", "currency_id": 1}
        rc, out, err, _, _ = _run(["payment", "create", "--company", "Arpit Goyal",
                                   "--client", "PeopleEquation Private Ltd", "--amount", "1500",
                                   "--date", "01102026", "--payment-number", "P-7", "--dry-run"],
                                  customer=inr)
        self.assertEqual(rc, 0, err)
        body = _body(out)
        self.assertEqual(body["customer_id"], 8)
        self.assertEqual(body["amount"], 150000)
        self.assertEqual(body["payment_number"], "P-7")
        self.assertNotIn("invoice_id", body)
        self.assertNotIn("exchange_rate", body)

    def test_foreign_without_rate_errors(self):
        args = [a for a in AGAINST_INVOICE if a not in ("--inr-amount", "355639.79")]
        rc, _, err, create, _ = _run(args)
        self.assertEqual(rc, 1)
        self.assertIn("--inr-amount", err)
        create.assert_not_called()


PAYMENTS = [{"id": 91, "payment_number": "PAY-000001", "payment_date": "2026-10-01",
             "amount": 378100, "base_amount": 35563979, "exchange_rate": "94.059717",
             "notes": "eFIRC CITIN26740027028", "customer": {"name": "Aster AI Inc"},
             "invoice": {"invoice_number": "INV-000004"}, "payment_method": {"name": "Bank Transfer"},
             "currency": {"code": "USD", "precision": 2}, "formatted_created_at": "03/10/2026"}]


class TestPaymentListExportDelete(unittest.TestCase):
    def test_payment_to_row(self):
        row = ee.payment_to_row(PAYMENTS[0])
        self.assertEqual(row["amount"], "3781.00")
        self.assertEqual(row["base_amount"], "355639.79")
        self.assertEqual(row["invoice_number"], "INV-000004")
        self.assertEqual(row["payment_method"], "Bank Transfer")
        self.assertEqual(row["customer"], "Aster AI Inc")

    def test_list(self):
        rc, out, _, _, _ = _run(["payment", "list", "--company", "A"], payments=PAYMENTS)
        self.assertEqual(rc, 0)
        self.assertIn("PAY-000001", out)
        self.assertIn("355639.79", out)

    def test_export(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _, _, _ = _run(["payment", "export", "--company", "A", "--out", d],
                                    payments=PAYMENTS)
            self.assertEqual(rc, 0)
            with open(os.path.join(d, "payments_A_all-time.csv")) as f:
                self.assertIn("CITIN26740027028", f.read())

    def test_delete(self):
        rc, out, _, _, dele = _run(["payment", "delete", "--company", "A",
                                    "--payment-number", "PAY-000001", "--dry-run"], payments=PAYMENTS)
        self.assertEqual(rc, 0)
        dele.assert_not_called()
        rc, out, _, _, dele = _run(["payment", "delete", "--company", "A",
                                    "--payment-number", "PAY-000001"], payments=PAYMENTS)
        self.assertEqual(rc, 0)
        dele.assert_called_once_with([91])


if __name__ == "__main__":
    unittest.main()
