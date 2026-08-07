import unittest
from unittest import mock
import ishelf_ee as ee


class TestNextExpenseNumber(unittest.TestCase):
    def test_first_of_day(self):
        self.assertEqual(ee.next_expense_number("07082026", 0), "0708202601")

    def test_second_of_day(self):
        self.assertEqual(ee.next_expense_number("07082026", 1), "0708202602")

    def test_twelfth(self):
        self.assertEqual(ee.next_expense_number("07082026", 11), "0708202612")


class TestCountExpensesOnDate(unittest.TestCase):
    def test_counts_and_scopes_to_date(self):
        c = ee.InvoiceShelfClient(ee.Config("https://x", "e", "p", "1"))
        c.token = "tok"
        captured = {}

        def fake(method, path, query=None, body=None, auth=True, raw=False):
            captured.update(path=path, query=query)
            return {"data": [{"id": 1}, {"id": 2}]}

        with mock.patch.object(c, "_request", side_effect=fake):
            n = c.count_expenses_on_date("2026-08-07")
        self.assertEqual(n, 2)
        self.assertEqual(captured["path"], "/expenses")
        self.assertEqual(captured["query"]["from_date"], "2026-08-07")
        self.assertEqual(captured["query"]["to_date"], "2026-08-07")


if __name__ == "__main__":
    unittest.main()
