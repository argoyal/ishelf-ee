import io
import unittest
from unittest import mock
from contextlib import redirect_stdout
import ishelf_ee as ee


class TestCreateDryRun(unittest.TestCase):
    def test_dry_run_offline_no_secret(self):
        args = ["create", "--company", "AsterHQ", "--amount", "12.34", "--currency", "USD",
                "--date", "07082026", "--category", "Software", "--notes", "Anthropic", "--dry-run"]
        with mock.patch.object(ee.InvoiceShelfClient, "login", return_value="tok"), \
             mock.patch.object(ee, "load_config",
                               return_value=ee.Config("https://x", "e", "sekret-pw", "1")), \
             mock.patch.object(ee, "resolve_company_id", return_value=2), \
             mock.patch.object(ee, "resolve_category_id", return_value=3), \
             mock.patch.object(ee, "resolve_currency_id", return_value=1):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ee.main(args)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("POST", out)
        self.assertIn("/expenses", out)
        self.assertIn('"amount": 1234', out)
        self.assertIn('"expense_date": "2026-08-07"', out)
        # secrets never printed
        self.assertNotIn("sekret-pw", out)
        self.assertNotIn("password", out.lower())


if __name__ == "__main__":
    unittest.main()
