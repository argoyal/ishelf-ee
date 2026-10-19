import io
import unittest
from unittest import mock
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import ishelf_ee as ee

EXPENSE = {
    "id": 9, "expense_number": "1509202603", "expense_date": "2026-09-15",
    "amount": 100000, "notes": "Old note", "expense_category_id": 3,
    "currency_id": 1, "customer_id": 5, "payment_method_id": 4, "exchange_rate": None,
    "currency": {"id": 1, "code": "INR", "precision": 2},
}


class FakeUpdate:
    """Records what update_expense was asked to send."""
    def __init__(self):
        self.calls = []

    def __call__(self, expense_id, body, receipt=None):
        self.calls.append((expense_id, body, receipt))
        return {"data": {"id": expense_id}}


def _run(args, expenses=None):
    expenses = [EXPENSE] if expenses is None else expenses
    fake = FakeUpdate()
    with ExitStack() as es:
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"))
        es.enter_context(mock.patch.object(
            ee, "load_config", return_value=ee.Config("https://x", "e", "pw", "1")))
        es.enter_context(mock.patch.object(ee, "resolve_company_id", return_value=2))
        es.enter_context(mock.patch.object(ee, "resolve_category_id", return_value=7))
        es.enter_context(mock.patch.object(ee, "resolve_customer_id", return_value=8))
        es.enter_context(mock.patch.object(ee, "resolve_payment_method_id", return_value=6))
        es.enter_context(mock.patch.object(
            ee.InvoiceShelfClient, "list_expenses", return_value=expenses))
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "update_expense", fake))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = ee.main(args)
            except SystemExit as e:
                rc = e.code
    return rc, out.getvalue(), err.getvalue(), fake


BASE = ["update", "--company", "Arpit Goyal", "--expense-number", "1509202603"]


class TestUpdate(unittest.TestCase):
    def test_update_changes_only_the_given_fields(self):
        rc, out, _, fake = _run(BASE + ["--amount", "2400.52"])
        self.assertEqual(rc, 0)
        eid, body, receipt = fake.calls[0]
        self.assertEqual(eid, 9)
        self.assertEqual(body["amount"], 240052)
        self.assertEqual(body["expense_date"], "2026-09-15")
        self.assertEqual(body["notes"], "Old note")
        self.assertEqual(body["expense_category_id"], 3)
        self.assertEqual(body["currency_id"], 1)
        self.assertEqual(body["customer_id"], 5)
        self.assertEqual(body["payment_method_id"], 4)
        self.assertEqual(body["expense_number"], "1509202603")
        self.assertIsNone(receipt)
        self.assertIn("updated 1509202603: amount=2400.52", out)

    def test_update_can_change_every_field(self):
        rc, out, _, fake = _run(BASE + [
            "--date", "01102026", "--category", "Fuel", "--notes", "New",
            "--client", "Acme", "--payment-method", "Card"])
        self.assertEqual(rc, 0)
        _, body, _ = fake.calls[0]
        self.assertEqual(body["expense_date"], "2026-10-01")
        self.assertEqual(body["expense_category_id"], 7)
        self.assertEqual(body["notes"], "New")
        self.assertEqual(body["customer_id"], 8)
        self.assertEqual(body["payment_method_id"], 6)
        self.assertEqual(body["amount"], 100000)

    def test_update_with_receipt_sends_multipart_put(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-fake")
        try:
            rc, out, _, fake = _run(BASE + ["--receipt", f.name])
        finally:
            os.unlink(f.name)
        self.assertEqual(rc, 0)
        _, _, receipt = fake.calls[0]
        self.assertEqual(receipt[0], os.path.basename(f.name))
        self.assertEqual(receipt[1], b"%PDF-fake")
        self.assertIn("receipt=", out)

    def test_client_sends_put_json_or_multipart_put(self):
        c = ee.InvoiceShelfClient(ee.Config("https://x", "e", "p", "1"))
        c.token = "tok"
        with mock.patch.object(c, "_request", return_value={}) as req:
            c.update_expense(9, {"amount": 1})
        req.assert_called_once_with("PUT", "/expenses/9", body={"amount": 1})
        with mock.patch.object(c, "_request_multipart", return_value={}) as mp:
            c.update_expense(9, {"amount": 1}, receipt=("r.pdf", b"x", "application/pdf"))
        method, path, data, ctype = mp.call_args[0]
        self.assertEqual((method, path), ("POST", "/expenses/9"))
        self.assertIn(b'name="_method"\r\n\r\nPUT', data)
        self.assertIn(b'name="attachment_receipt"; filename="r.pdf"', data)

    def test_update_unknown_number_exits_2(self):
        rc, out, err, fake = _run(BASE + ["--amount", "1"], expenses=[])
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])
        self.assertIn("No expense", err)

    def test_update_needs_at_least_one_change(self):
        rc, out, err, fake = _run(BASE)
        self.assertEqual(rc, 2)  # argparse error
        self.assertEqual(fake.calls, [])

    def test_update_dry_run_sends_nothing(self):
        rc, out, _, fake = _run(BASE + ["--amount", "5", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("would update 1509202603: amount=5", out)

    def test_api_error_exits_1(self):
        def boom(self, *a, **k):
            raise ee.ApiError("HTTP 422")
        with mock.patch.object(ee.InvoiceShelfClient, "update_expense", boom):
            with ExitStack() as es:
                es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "login", return_value="t"))
                es.enter_context(mock.patch.object(
                    ee, "load_config", return_value=ee.Config("https://x", "e", "pw", "1")))
                es.enter_context(mock.patch.object(ee, "resolve_company_id", return_value=2))
                es.enter_context(mock.patch.object(
                    ee.InvoiceShelfClient, "list_expenses", return_value=[EXPENSE]))
                with redirect_stderr(io.StringIO()):
                    rc = ee.main(BASE + ["--amount", "1"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
