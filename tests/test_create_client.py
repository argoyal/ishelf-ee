import unittest
from unittest import mock
import ishelf_ee as ee


def _client():
    c = ee.InvoiceShelfClient(ee.Config(url="https://x", email="e", password="p", company_id="1"))
    c.token = "tok"
    return c


class TestResolvers(unittest.TestCase):
    def test_resolve_category_exact(self):
        c = _client()
        with mock.patch.object(c, "_request", return_value={"data": [{"id": 3, "name": "Software"}]}):
            self.assertEqual(ee.resolve_category_id(c, "Software"), 3)

    def test_resolve_currency_by_code_bare_list(self):
        c = _client()
        # /currencies may return a bare list — must still resolve
        with mock.patch.object(c, "_request", return_value=[{"id": 1, "code": "USD"}, {"id": 2, "code": "INR"}]):
            self.assertEqual(ee.resolve_currency_id(c, "INR"), 2)

    def test_resolve_absent_raises(self):
        c = _client()
        with mock.patch.object(c, "_request", return_value={"data": []}):
            with self.assertRaises(LookupError):
                ee.resolve_category_id(c, "Nope")

    def test_resolve_payment_method_hits_endpoint(self):
        c = _client()
        with mock.patch.object(
                c, "_request",
                return_value={"data": [{"id": 4, "name": "Credit Card"},
                                       {"id": 5, "name": "Cash"}]}) as m:
            self.assertEqual(ee.resolve_payment_method_id(c, "Credit Card"), 4)
        m.assert_called_once_with("GET", "/payment-methods", query={"limit": "all"})


class TestCreateExpense(unittest.TestCase):
    def test_create_posts_json_when_no_receipt(self):
        c = _client()
        captured = {}

        def fake_request(method, path, query=None, body=None, auth=True, raw=False):
            captured.update(method=method, path=path, body=body)
            return {"data": {"id": 9}}

        with mock.patch.object(c, "_request", side_effect=fake_request):
            out = c.create_expense({"amount": 1234, "expense_date": "2026-08-07"})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/expenses")
        self.assertEqual(captured["body"]["amount"], 1234)
        self.assertEqual(out["data"]["id"], 9)

    def test_create_uses_multipart_when_receipt(self):
        c = _client()
        captured = {}

        def fake_mp(method, path, data, content_type):
            captured.update(method=method, path=path, ctype=content_type, data=data)
            return {"data": {"id": 11}}

        with mock.patch.object(c, "_request_multipart", side_effect=fake_mp):
            out = c.create_expense({"amount": 500}, receipt=("r.png", b"PNGDATA", "image/png"))
        self.assertEqual(captured["path"], "/expenses")
        self.assertIn("multipart/form-data", captured["ctype"])
        self.assertIn(b"PNGDATA", captured["data"])
        self.assertIn(b'name="attachment_receipt"', captured["data"])
        self.assertEqual(out["data"]["id"], 11)


if __name__ == "__main__":
    unittest.main()
