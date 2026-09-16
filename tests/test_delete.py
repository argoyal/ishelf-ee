import io
import unittest
from unittest import mock
from contextlib import ExitStack, redirect_stdout
import ishelf_ee as ee


def _client():
    c = ee.InvoiceShelfClient(ee.Config(url="https://x", email="e", password="p", company_id="1"))
    c.token = "tok"
    return c


class TestDeleteClient(unittest.TestCase):
    def test_delete_posts_ids_to_delete_endpoint(self):
        c = _client()
        captured = {}

        def fake_request(method, path, query=None, body=None, auth=True, raw=False):
            captured.update(method=method, path=path, body=body)
            return {"success": True}

        with mock.patch.object(c, "_request", side_effect=fake_request):
            c.delete_expenses([9])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/expenses/delete")
        self.assertEqual(captured["body"], {"ids": [9]})


class TestResolveByNumber(unittest.TestCase):
    def test_finds_unique(self):
        c = _client()
        exps = [{"id": 9, "expense_number": "1509202603"},
                {"id": 10, "expense_number": "1509202601"}]
        with mock.patch.object(c, "list_expenses", return_value=exps):
            got = ee.resolve_expense_by_number(c, "1509202603")
        self.assertEqual(got["id"], 9)

    def test_absent_raises(self):
        c = _client()
        with mock.patch.object(c, "list_expenses", return_value=[{"id": 1, "expense_number": "X"}]):
            with self.assertRaises(LookupError):
                ee.resolve_expense_by_number(c, "NOPE")

    def test_ambiguous_raises(self):
        c = _client()
        dupes = [{"id": 1, "expense_number": "DUP"}, {"id": 2, "expense_number": "DUP"}]
        with mock.patch.object(c, "list_expenses", return_value=dupes):
            with self.assertRaises(LookupError):
                ee.resolve_expense_by_number(c, "DUP")


class TestDeleteCLI(unittest.TestCase):
    def _run(self, args, expenses, delete_ret=None):
        with ExitStack() as es:
            es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"))
            es.enter_context(mock.patch.object(
                ee, "load_config", return_value=ee.Config("https://x", "e", "pw", "1")))
            es.enter_context(mock.patch.object(ee, "resolve_company_id", return_value=2))
            es.enter_context(mock.patch.object(
                ee.InvoiceShelfClient, "list_expenses", return_value=expenses))
            dl = es.enter_context(mock.patch.object(
                ee.InvoiceShelfClient, "delete_expenses",
                return_value=delete_ret or {"success": True}))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ee.main(args)
        return rc, buf.getvalue(), dl

    def test_delete_by_number_calls_delete(self):
        exps = [{"id": 9, "expense_number": "1509202603",
                 "expense_date": "2026-09-15", "amount": 1120821}]
        rc, out, dl = self._run(
            ["delete", "--company", "Ascendra Ventures", "--expense-number", "1509202603"], exps)
        self.assertEqual(rc, 0)
        dl.assert_called_once_with([9])
        self.assertIn("9", out)

    def test_dry_run_does_not_delete(self):
        exps = [{"id": 9, "expense_number": "1509202603",
                 "expense_date": "2026-09-15", "amount": 1120821}]
        rc, out, dl = self._run(
            ["delete", "--company", "Ascendra Ventures", "--expense-number", "1509202603",
             "--dry-run"], exps)
        self.assertEqual(rc, 0)
        dl.assert_not_called()
        self.assertIn("1509202603", out)

    def test_not_found_returns_error_without_deleting(self):
        rc, out, dl = self._run(["delete", "--company", "X", "--expense-number", "NOPE"], [])
        self.assertEqual(rc, 1)
        dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
