import io
import unittest
from unittest import mock
from contextlib import ExitStack, redirect_stdout
import ishelf_ee as ee


def _run(args, count=0, customer=5):
    """Run ee.main(args) fully mocked (offline); return (rc, stdout)."""
    with ExitStack() as es:
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"))
        es.enter_context(mock.patch.object(ee, "load_config",
                                           return_value=ee.Config("https://x", "e", "sekret-pw", "1")))
        es.enter_context(mock.patch.object(ee, "resolve_company_id", return_value=2))
        es.enter_context(mock.patch.object(ee, "resolve_category_id", return_value=3))
        es.enter_context(mock.patch.object(ee, "resolve_currency_id", return_value=1))
        es.enter_context(mock.patch.object(ee, "resolve_customer_id", return_value=customer))
        es.enter_context(mock.patch.object(ee.InvoiceShelfClient, "count_expenses_on_date",
                                           return_value=count))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ee.main(args)
    return rc, buf.getvalue()


class TestCreateDryRun(unittest.TestCase):
    def test_dry_run_offline_no_secret_autonumber(self):
        rc, out = _run(["create", "--company", "AsterHQ", "--amount", "12.34", "--currency", "USD",
                        "--date", "07082026", "--category", "Software", "--notes", "Anthropic", "--dry-run"],
                       count=0)
        self.assertEqual(rc, 0)
        self.assertIn("POST", out)
        self.assertIn("/expenses", out)
        self.assertIn('"amount": 1234', out)
        self.assertIn('"expense_date": "2026-08-07"', out)
        self.assertIn('"expense_number": "0708202601"', out)  # first of the day
        self.assertNotIn("sekret-pw", out)
        self.assertNotIn("password", out.lower())

    def test_autonumber_second_of_day(self):
        rc, out = _run(["create", "--company", "AsterHQ", "--amount", "1.00", "--currency", "USD",
                        "--date", "07082026", "--category", "Fuel", "--dry-run"], count=1)
        self.assertIn('"expense_number": "0708202602"', out)

    def test_explicit_expense_number_wins(self):
        rc, out = _run(["create", "--company", "AsterHQ", "--amount", "1.00", "--currency", "USD",
                        "--date", "07082026", "--category", "Fuel", "--expense-number", "CUSTOM-9",
                        "--dry-run"], count=5)
        self.assertIn('"expense_number": "CUSTOM-9"', out)

    def test_client_sets_customer_id(self):
        rc, out = _run(["create", "--company", "Arpit Goyal", "--client", "Ascendra Ventures",
                        "--amount", "30.00", "--currency", "INR", "--date", "07082026",
                        "--category", "Fuel", "--dry-run"], count=0, customer=5)
        self.assertEqual(rc, 0)
        self.assertIn('"customer_id": 5', out)
        self.assertIn('"amount": 3000', out)

    def test_payment_method_sets_id(self):
        with mock.patch.object(ee, "resolve_payment_method_id", return_value=4):
            rc, out = _run(["create", "--company", "Arpit Goyal", "--amount", "10.00",
                            "--currency", "INR", "--date", "07082026", "--category", "Fuel",
                            "--payment-method", "Credit Card", "--dry-run"], count=0)
        self.assertEqual(rc, 0)
        self.assertIn('"payment_method_id": 4', out)

    def test_no_payment_method_omits_id(self):
        rc, out = _run(["create", "--company", "Arpit Goyal", "--amount", "10.00",
                        "--currency", "INR", "--date", "07082026", "--category", "Fuel",
                        "--dry-run"], count=0)
        self.assertEqual(rc, 0)
        self.assertNotIn("payment_method_id", out)


if __name__ == "__main__":
    unittest.main()
